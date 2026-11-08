"""Exercise diagnostic publication with installed Home Assistant, without BLE.

Run separately from the standard-library suite so its HA stubs cannot replace
the real framework: python -B -m unittest discover -s tests/ha -v
"""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.yutai_ble.bridge import BridgeError
from custom_components.yutai_ble.coordinator import YutaiCoordinator
from custom_components.yutai_ble.sensor import YutaiDiagnostic
from custom_components.yutai_ble.switch import YutaiRelaySwitch


ADDRESS = "02:00:00:00:00:01"
MODEL = "yutai.plug.fsov8m"


def read_report():
    return {
        "address": ADDRESS, "model": MODEL, "status": "read_verified",
        "authentication_verified": True, "f0_verified": True,
        "relay_writes": 0, "connected_after_cleanup": False,
        "settings": {}, "capabilities": {},
        "properties": [{
            "siid": 2, "piid": 1, "status": 0, "value": False,
            "type_name": "bool", "typelen": 1, "raw_hex": "00",
        }],
    }


class FrameworkDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        entry = ConfigEntry(
            domain="yutai_ble", title="Test plug", source="user",
            unique_id=ADDRESS, version=1, minor_version=1,
            data={"address": ADDRESS, "model": MODEL, "gatt_ltmk": "ab" * 32},
            options={}, discovery_keys=MappingProxyType({}), subentries_data=(),
            pref_disable_polling=True,
        )
        self.coordinator = YutaiCoordinator(self.hass, entry)
        self.status = YutaiDiagnostic(self.coordinator, "connection_status")
        self.failures = YutaiDiagnostic(self.coordinator, "consecutive_failures")
        self.relay = YutaiRelaySwitch(self.coordinator)
        self.notifications = []
        self.unsubscribe = self.coordinator.async_add_listener(self.record_state)
        self.coordinator.bridge.read = AsyncMock(return_value=read_report())
        await self.coordinator.async_refresh()
        self.assertEqual(len(self.notifications), 1)
        self.assertTrue(self.relay.available)
        self.last_success = self.coordinator.last_success_at
        self.notifications.clear()

    async def asyncTearDown(self):
        self.unsubscribe()
        await self.coordinator.async_shutdown()
        await self.hass.async_stop(force=True)

    def record_state(self):
        self.notifications.append({
            "status": self.status.native_value,
            "failures": self.failures.native_value,
            "available": self.relay.available,
            **self.status.extra_state_attributes,
        })

    async def test_every_failed_refresh_notifies_once_and_recovery_restores_state(self):
        self.coordinator.bridge.read = AsyncMock(
            side_effect=BridgeError("ble_connect_failed")
        )
        for _ in range(4):
            await self.coordinator.async_refresh()
        self.assertEqual([item["failures"] for item in self.notifications], [1, 2, 3, 4])
        self.assertEqual(
            [item["poll_interval_seconds"] for item in self.notifications],
            [120, 240, 300, 300],
        )
        self.assertTrue(all(item["status"] == "retrying" for item in self.notifications))
        self.assertTrue(all(not item["available"] for item in self.notifications))
        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.last_success_at, self.last_success)

        self.coordinator.bridge.read = AsyncMock(return_value=read_report())
        await self.coordinator.async_refresh()
        self.assertEqual(len(self.notifications), 5)
        self.assertEqual(self.notifications[-1], {
            "status": "verified", "failures": 0, "available": True,
            "last_error": None, "poll_interval_seconds": 60,
        })

    async def test_failed_commands_and_first_failed_poll_publish_changed_diagnostics(self):
        with patch.object(self.coordinator, "_schedule_recovery"):
            for error in (
                BridgeError("worker_timeout", may_have_written=True),
                BridgeError("ble_connect_failed"),
            ):
                self.coordinator.bridge.relay = AsyncMock(side_effect=error)
                with self.assertRaises(HomeAssistantError):
                    await self.coordinator.async_set_relay(True)
        self.assertEqual(len(self.notifications), 2)
        self.assertEqual(
            [item["last_error"] for item in self.notifications],
            ["write_result_uncertain", "ble_connect_failed"],
        )

        self.coordinator.bridge.read = AsyncMock(
            side_effect=BridgeError("ble_connect_timeout")
        )
        await self.coordinator.async_refresh()
        self.assertEqual(len(self.notifications), 3)
        self.assertEqual(self.notifications[-1], {
            "status": "retrying", "failures": 1, "available": False,
            "last_error": "ble_connect_timeout", "poll_interval_seconds": 120,
        })
        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.last_success_at, self.last_success)


if __name__ == "__main__":
    unittest.main()

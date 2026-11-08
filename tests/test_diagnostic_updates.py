"""Listeners must see changing diagnostics while HA suppresses repeat failures."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from test_coordinator import HAError, bridge, coordinator_module, models
from test_models import config_data, report
from test_sensor import sensor


class DiagnosticUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coordinator = coordinator_module.YutaiCoordinator(
            None, SimpleNamespace(data=config_data(), options={}))
        self.original = models.normalize_report(self.coordinator.device_config, report())
        self.coordinator.data = self.original
        self.prior_success = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.coordinator.last_success_at = self.prior_success
        self.entities = {field: sensor.YutaiDiagnostic(self.coordinator, field) for field in
                         ("connection_status", "last_success", "consecutive_failures")}
        self.events = []
        self.coordinator.async_add_listener(self.capture_state)

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()

    def capture_state(self):
        self.events.append({
            "status": self.entities["connection_status"].native_value,
            "failures": self.entities["consecutive_failures"].native_value,
            "last_success": self.entities["last_success"].native_value,
            "diagnostics_available": all(entity.available for entity in self.entities.values()),
            "last_update_success": self.coordinator.last_update_success,
            "snapshot": self.coordinator.data,
            **self.entities["connection_status"].extra_state_attributes,
        })

    def assert_failed_event(self, event, *, failures, error, interval):
        self.assertEqual(event["status"], "retrying")
        self.assertEqual(event["failures"], failures)
        self.assertEqual(event["last_error"], error)
        self.assertEqual(event["poll_interval_seconds"], interval)
        self.assertEqual(event["last_success"], self.prior_success)
        self.assertTrue(event["diagnostics_available"])
        self.assertFalse(event["last_update_success"])
        self.assertIs(event["snapshot"], self.original)

    async def test_repeated_read_failures_and_recovery_notify_once_each(self):
        codes = ("ble_connect_failed", "ble_connect_timeout", "authentication_failed", "worker_failed")
        self.coordinator.bridge.read = AsyncMock(side_effect=[bridge.BridgeError(code) for code in codes])
        for count, (code, interval) in enumerate(zip(codes, (120, 240, 300, 300)), 1):
            await self.coordinator.async_request_refresh()
            self.assertEqual(len(self.events), count)
            self.assert_failed_event(self.events[-1], failures=count, error=code, interval=interval)
        self.assertEqual(self.coordinator.published, [])

        self.coordinator.bridge.read = AsyncMock(return_value=report())
        await self.coordinator.async_request_refresh()
        self.assertEqual(len(self.events), 5)
        event = self.events[-1]
        self.assertEqual(event["status"], "verified")
        self.assertEqual(event["failures"], 0)
        self.assertIsNone(event["last_error"])
        self.assertEqual(event["poll_interval_seconds"], 60)
        self.assertTrue(event["last_update_success"])
        self.assertGreater(event["last_success"], self.prior_success)
        self.assertEqual(len(self.coordinator.published), 1)

    async def test_repeated_write_failures_then_first_read_failure_notify(self):
        self.coordinator.bridge.setting = AsyncMock(side_effect=bridge.BridgeError("authentication_failed"))
        with self.assertRaises(HAError):
            await self.coordinator.async_set_setting("indicator_mode", "follow")
        self.assertEqual(len(self.events), 1)
        self.assert_failed_event(self.events[-1], failures=0, error="authentication_failed", interval=60)

        self.coordinator.bridge.relay = AsyncMock(
            side_effect=bridge.BridgeError("worker_timeout", may_have_written=True))
        with self.assertRaises(HAError):
            await self.coordinator.async_set_relay(True)
        self.assertEqual(len(self.events), 2)
        self.assert_failed_event(self.events[-1], failures=0, error="write_result_uncertain", interval=60)

        self.coordinator.bridge.read = AsyncMock(side_effect=bridge.BridgeError("ble_connect_failed"))
        await self.coordinator.async_request_refresh()
        self.assertEqual(len(self.events), 3)
        self.assert_failed_event(self.events[-1], failures=1, error="ble_connect_failed", interval=120)
        self.assertEqual(self.coordinator.published, [])

    async def test_queued_refresh_does_not_double_notify_after_write_failure(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def fail_write(*_args):
            started.set()
            await release.wait()
            raise bridge.BridgeError("worker_timeout", may_have_written=True)

        self.coordinator.bridge.relay = AsyncMock(side_effect=fail_write)
        self.coordinator.bridge.read = AsyncMock(side_effect=bridge.BridgeError("ble_connect_failed"))
        write = asyncio.create_task(self.coordinator.async_set_relay(True))
        await started.wait()
        refresh = asyncio.create_task(self.coordinator.async_request_refresh())
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(write, refresh, return_exceptions=True)
        self.assertIsInstance(outcomes[0], HAError)
        self.assertIsNone(outcomes[1])
        self.assertEqual(len(self.events), 2)
        self.assert_failed_event(self.events[0], failures=0, error="write_result_uncertain", interval=60)
        self.assert_failed_event(self.events[1], failures=1, error="ble_connect_failed", interval=120)

    async def test_verified_manual_recovery_notifies_once(self):
        self.coordinator.bridge.read = AsyncMock(side_effect=bridge.BridgeError("ble_connect_failed"))
        await self.coordinator.async_request_refresh()
        readback = report()
        readback.update(status="setting_verified", verified=True)
        readback["settings"]["indicator_mode"] = "follow"
        self.coordinator.bridge.setting = AsyncMock(return_value=readback)
        await self.coordinator.async_set_setting("indicator_mode", "follow")
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[-1]["status"], "verified")
        self.assertTrue(self.events[-1]["last_update_success"])
        self.assertIsNone(self.events[-1]["last_error"])
        self.assertEqual(self.events[-1]["failures"], 0)
        self.assertEqual(self.events[-1]["poll_interval_seconds"], 60)
        self.assertEqual(self.events[-1]["snapshot"].settings["indicator_mode"], "follow")
        self.assertEqual(len(self.coordinator.published), 1)

    async def test_cancelled_read_updates_error_while_already_failed(self):
        self.coordinator.bridge.read = AsyncMock(side_effect=bridge.BridgeError("ble_connect_failed"))
        await self.coordinator.async_request_refresh()
        started = asyncio.Event()

        async def blocked(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        self.coordinator.bridge.read = AsyncMock(side_effect=blocked)
        refresh = asyncio.create_task(self.coordinator.async_request_refresh())
        await started.wait()
        refresh.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await refresh
        self.assertEqual(len(self.events), 2)
        self.assert_failed_event(self.events[-1], failures=1, error="request_cancelled", interval=120)
        self.assertEqual(self.coordinator.published, [])


if __name__ == "__main__":
    unittest.main()

"""Entry setup and unloading clean up only their own coordinator."""

import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from _load import COMPONENT
from test_coordinator import coordinator_module  # Preload the coordinator with HA stubs.


def load_entrypoint():
    names = ("homeassistant", "homeassistant.config_entries", "homeassistant.const", "homeassistant.core")
    stubs = {name: types.ModuleType(name) for name in names}
    stubs["custom_components.yutai_ble.coordinator"] = coordinator_module
    stubs["homeassistant.config_entries"].ConfigEntry = object
    stubs["homeassistant.core"].HomeAssistant = object
    stubs["homeassistant.const"].Platform = SimpleNamespace(
        SELECT="select", SWITCH="switch", TIME="time", NUMBER="number",
        SENSOR="sensor", BINARY_SENSOR="binary_sensor")
    spec = importlib.util.spec_from_file_location(
        "custom_components.yutai_ble._entrypoint_test", COMPONENT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "custom_components.yutai_ble"
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


entrypoint = load_entrypoint()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self):
        coordinator = SimpleNamespace(async_config_entry_first_refresh=AsyncMock(), async_shutdown=AsyncMock())
        entry = SimpleNamespace(async_on_unload=Mock(), add_update_listener=Mock(return_value=lambda: None))
        hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(), async_unload_platforms=AsyncMock(return_value=True)))
        return coordinator, entry, hass

    async def test_failed_first_refresh_always_shuts_coordinator_down(self):
        coordinator, entry, hass = self.prepare()
        coordinator.async_config_entry_first_refresh.side_effect = RuntimeError("test-only")
        with patch.object(entrypoint, "YutaiCoordinator", return_value=coordinator):
            with self.assertRaises(RuntimeError):
                await entrypoint.async_setup_entry(hass, entry)
        coordinator.async_shutdown.assert_awaited_once()
        hass.config_entries.async_forward_entry_setups.assert_not_awaited()

    async def test_failed_platform_setup_always_shuts_coordinator_down(self):
        coordinator, entry, hass = self.prepare()
        hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("test-only")
        with patch.object(entrypoint, "YutaiCoordinator", return_value=coordinator):
            with self.assertRaises(RuntimeError):
                await entrypoint.async_setup_entry(hass, entry)
        coordinator.async_shutdown.assert_awaited_once()

    async def test_successful_unload_waits_for_coordinator_shutdown(self):
        coordinator, entry, hass = self.prepare()
        entry.runtime_data = coordinator
        self.assertTrue(await entrypoint.async_unload_entry(hass, entry))
        coordinator.async_shutdown.assert_awaited_once()

    async def test_unsuccessful_platform_unload_leaves_entry_running(self):
        coordinator, entry, hass = self.prepare()
        entry.runtime_data = coordinator
        hass.config_entries.async_unload_platforms.return_value = False
        self.assertFalse(await entrypoint.async_unload_entry(hass, entry))
        coordinator.async_shutdown.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

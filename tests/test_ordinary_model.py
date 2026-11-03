"""HA boundaries for the ordinary fsov7m, with no hardware connections."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_config_flow import flow_module
from test_coordinator import coordinator_module, HAError
from test_models import config_data, models, report
from test_sensor import sensor


def ordinary_data():
    return {**config_data(), "model": "yutai.plug.fsov7m"}


def ordinary_report():
    return {**report(), "model": "yutai.plug.fsov7m",
            "settings": {"indicator_mode": "follow", "power_on_state": "off"},
            "capabilities": {
                "indicator_mode": {"kind": "select", "options": ["follow", "off"]},
                "power_on_state": {"kind": "select", "options": ["off", "on", "memory"]}}}


class OrdinaryModelTests(unittest.IsolatedAsyncioTestCase):
    def test_configuration_and_normalization_hide_unverified_meters(self):
        config = models.validate_config(ordinary_data())
        snapshot = models.normalize_report(config, ordinary_report())
        self.assertEqual(snapshot.measurements, {"voltage": None, "current": None, "power": None, "relay_output": False})
        self.assertEqual(set(snapshot.capabilities), {"indicator_mode", "power_on_state"})

    def test_fsov8_capabilities_cannot_enter_fsov7_snapshot(self):
        config = models.validate_config(ordinary_data())
        for extra in ({"indicator_mode": {"kind": "select", "options": ["follow", "inverse"]}},
                      {"indicator_dnd_enabled": {"kind": "switch"}},
                      {"double_click_enabled": {"kind": "switch"}}):
            data = ordinary_report()
            data["capabilities"].update(extra)
            with self.assertRaises(models.ValidationError):
                models.normalize_report(config, data)

    async def test_user_and_import_flows_accept_ordinary_model_after_read(self):
        for method in ("async_step_user", "async_step_import"):
            flow = flow_module.YutaiConfigFlow()
            with patch.object(flow_module.BridgeClient, "read", AsyncMock(return_value=ordinary_report())) as read:
                result = await getattr(flow, method)(ordinary_data())
            self.assertEqual(result["type"], "create_entry")
            self.assertEqual(result["data"]["model"], "yutai.plug.fsov7m")
            self.assertEqual(read.call_args.args[0].model, "yutai.plug.fsov7m")

    async def test_form_lists_both_yutai_models_only(self):
        form = await flow_module.YutaiConfigFlow().async_step_user()
        self.assertEqual(set(form["data_schema"]["model"]), {"yutai.plug.fsov7m", "yutai.plug.fsov8m"})

    async def test_sensor_setup_does_not_create_ordinary_meter_entities(self):
        for model, count in (("yutai.plug.fsov7m", 3), ("yutai.plug.fsov8m", 6)):
            coordinator = SimpleNamespace(device_config=SimpleNamespace(model=model))
            entities = []
            await sensor.async_setup_entry(None, SimpleNamespace(runtime_data=coordinator), lambda items: entities.extend(items))
            self.assertEqual(len(entities), count)
            if model.endswith("fsov7m"):
                self.assertTrue(all(isinstance(entity, sensor.YutaiDiagnostic) for entity in entities))

    async def test_ordinary_coordinator_blocks_unverified_settings_before_dispatch(self):
        coordinator = coordinator_module.YutaiCoordinator(None, SimpleNamespace(data=ordinary_data(), options={}))
        coordinator.data = models.normalize_report(coordinator.device_config, ordinary_report())
        coordinator.bridge.setting = AsyncMock()
        try:
            for field, value in (("indicator_mode", "inverse"), ("indicator_dnd_enabled", True), ("double_click_enabled", True)):
                with self.assertRaises(HAError):
                    await coordinator.async_set_setting(field, value)
            coordinator.bridge.setting.assert_not_awaited()
            data = ordinary_report()
            data.update(status="setting_verified", verified=True)
            data["settings"]["indicator_mode"] = "off"
            coordinator.bridge.setting.return_value = data
            await coordinator.async_set_setting("indicator_mode", "off")
            self.assertEqual(coordinator.data.settings["indicator_mode"], "off")
            coordinator.bridge.setting.assert_awaited_once_with(coordinator.device_config, "indicator_mode", "off")
        finally:
            await coordinator.async_shutdown()

"""Offline number-service validation, with no HA or Bluetooth runtime."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from _load import integration
from test_models import config_data, report

models = integration("models")


class HAError(Exception):
    def __init__(self, message, **translations):
        super().__init__(message)
        self.translations = translations


class EntityStub:
    def __init__(self, coordinator, field):
        self.coordinator = coordinator
        self.field = field

    @property
    def verified_value(self):
        return self.coordinator.data.settings.get(self.field)


def load_number():
    stubs = {name: types.ModuleType(name) for name in (
        "homeassistant.components.number", "homeassistant.exceptions",
        "homeassistant.helpers.entity", "custom_components.yutai_ble.entity",
    )}
    stubs["homeassistant.components.number"].NumberEntity = type("NumberEntity", (), {})
    stubs["homeassistant.components.number"].NumberMode = SimpleNamespace(BOX="box")
    stubs["homeassistant.exceptions"].HomeAssistantError = HAError
    stubs["homeassistant.helpers.entity"].EntityCategory = SimpleNamespace(CONFIG="config")
    stubs["custom_components.yutai_ble.entity"].YutaiEntity = EntityStub
    source = Path(__file__).resolve().parents[1] / "custom_components" / "yutai_ble" / "number.py"
    spec = importlib.util.spec_from_file_location("custom_components.yutai_ble.number_service_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


number = load_number()


class NumberTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        data = report()
        data["capabilities"]["double_click_seconds"] = {"kind": "number", "min": 1, "max": 86340, "step": 1, "unit": "s"}
        data["settings"]["double_click_seconds"] = 120
        self.snapshot = models.normalize_report(models.validate_config(config_data()), data)
        self.coordinator = SimpleNamespace(
            data=self.snapshot,
            async_set_setting=AsyncMock(),
            async_set_update_error=Mock(),
            last_update_success=True,
        )
        self.entity = number.YutaiNumber(self.coordinator, "double_click_seconds")

    async def test_seconds_display_and_confirmed_range(self):
        self.assertEqual(self.entity._attr_native_unit_of_measurement, "s")
        self.assertEqual(self.entity._attr_native_min_value, 1)
        self.assertEqual(self.entity._attr_native_max_value, 86340)
        self.assertEqual(self.entity._attr_native_step, 1)
        self.assertEqual(self.entity.native_value, 120)

    async def test_representable_values_dispatch_once_as_integer_seconds(self):
        for value in (1, 17.0, 59, 60.0, 120, 3780, 3840, 7440, 86340):
            with self.subTest(value=value):
                self.coordinator.async_set_setting.reset_mock()
                await self.entity.async_set_native_value(value)
                self.coordinator.async_set_setting.assert_awaited_once_with("double_click_seconds", int(value))
                self.assertIs(type(self.coordinator.async_set_setting.call_args.args[1]), int)

    async def test_invalid_inputs_do_not_dispatch_or_change_other_entity_availability(self):
        for value in (0, -1, 61, 137, 3599, 86339, 86341, 86400, 10**1000, True, "120", 60.5, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(HAError) as caught:
                await self.entity.async_set_native_value(value)
            self.assertEqual(caught.exception.translations["translation_domain"], "yutai_ble")
            self.assertEqual(caught.exception.translations["translation_key"], "invalid_countdown_duration")
            self.coordinator.async_set_setting.assert_not_awaited()
            self.coordinator.async_set_update_error.assert_not_called()
            self.assertTrue(self.coordinator.last_update_success)
            self.assertIs(self.coordinator.data, self.snapshot)
            self.assertEqual(self.coordinator.data.settings["indicator_mode"], "inverse")
            self.assertEqual(self.entity.native_value, 120)

    async def test_backend_capability_narrowing_is_respected_before_dispatch(self):
        self.snapshot.capabilities["double_click_seconds"] = models.Capability("number", minimum=1, maximum=3600, step=1)
        with self.assertRaises(HAError):
            await self.entity.async_set_native_value(3780)
        self.coordinator.async_set_setting.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

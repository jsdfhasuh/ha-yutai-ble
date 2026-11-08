"""Time-service validation and error translations without HA or Bluetooth."""

from datetime import time, timedelta, timezone
import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _load import COMPONENT
from test_coordinator import HAError


class EntityStub:
    def __init__(self, coordinator, field):
        self.coordinator = coordinator
        self.field = field


def load_time():
    stubs = {name: types.ModuleType(name) for name in (
        "homeassistant.components.time", "homeassistant.exceptions",
        "homeassistant.helpers.entity", "custom_components.yutai_ble.entity",
    )}
    stubs["homeassistant.components.time"].TimeEntity = type("TimeEntity", (), {})
    stubs["homeassistant.exceptions"].HomeAssistantError = HAError
    stubs["homeassistant.helpers.entity"].EntityCategory = SimpleNamespace(CONFIG="config")
    stubs["custom_components.yutai_ble.entity"].YutaiEntity = EntityStub
    spec = importlib.util.spec_from_file_location(
        "custom_components.yutai_ble.time_service_test", COMPONENT / "time.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


time_platform = load_time()


class TimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coordinator = SimpleNamespace(async_set_setting=AsyncMock())
        self.entity = time_platform.YutaiTime(self.coordinator, "indicator_dnd_start")

    async def test_local_whole_minutes_dispatch_once_without_changing_value(self):
        for value in (time(0, 0), time(12, 30), time(23, 59)):
            with self.subTest(value=value):
                self.coordinator.async_set_setting.reset_mock()
                await self.entity.async_set_value(value)
                self.coordinator.async_set_setting.assert_awaited_once_with(
                    "indicator_dnd_start", value.strftime("%H:%M")
                )

    async def test_seconds_and_timezone_errors_are_translatable_without_dispatch(self):
        for value in (time(12, 30, 1), time(12, 30, microsecond=1),
                      time(12, 30, tzinfo=timezone.utc),
                      time(12, 30, tzinfo=timezone(timedelta(hours=8)))):
            with self.subTest(value=value), self.assertRaises(HAError) as caught:
                await self.entity.async_set_value(value)
            self.assertEqual(
                (caught.exception.translation_domain, caught.exception.translation_key),
                ("yutai_ble", "invalid_local_time"),
            )
            self.assertIsNone(caught.exception.translation_placeholders)
            self.coordinator.async_set_setting.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

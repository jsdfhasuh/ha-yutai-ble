"""Diagnostic visibility during repeated failures, without HA or BLE access."""

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class EntityStub:
    def __init__(self, coordinator, field, *, measurement=False):
        self.coordinator = coordinator
        self.field = field


def load_sensor():
    stubs = {name: types.ModuleType(name) for name in (
        "homeassistant.components.sensor", "homeassistant.const",
        "homeassistant.helpers.entity", "custom_components.yutai_ble.entity",
    )}
    platform = stubs["homeassistant.components.sensor"]
    platform.SensorEntity = type("SensorEntity", (), {})
    platform.SensorDeviceClass = SimpleNamespace(VOLTAGE="voltage", CURRENT="current", POWER="power", TIMESTAMP="timestamp", ENUM="enum")
    platform.SensorStateClass = SimpleNamespace(MEASUREMENT="measurement")
    constants = stubs["homeassistant.const"]
    constants.UnitOfElectricCurrent = SimpleNamespace(AMPERE="A")
    constants.UnitOfElectricPotential = SimpleNamespace(VOLT="V")
    constants.UnitOfPower = SimpleNamespace(WATT="W")
    stubs["homeassistant.helpers.entity"].EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
    stubs["custom_components.yutai_ble.entity"].YutaiEntity = EntityStub
    source = Path(__file__).resolve().parents[1] / "custom_components" / "yutai_ble" / "sensor.py"
    spec = importlib.util.spec_from_file_location("custom_components.yutai_ble.sensor_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


sensor = load_sensor()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = SimpleNamespace(last_success_at=None, last_error_code=None,
                                           consecutive_failures=0, last_update_success=False,
                                           update_interval=timedelta(seconds=60))
        self.entities = {field: sensor.YutaiDiagnostic(self.coordinator, field) for field in
                         ("connection_status", "last_success", "consecutive_failures")}

    def test_initialization_does_not_claim_success(self):
        self.assertEqual(self.entities["connection_status"].native_value, "initializing")
        self.assertIsNone(self.entities["last_success"].native_value)

    def test_failed_reads_keep_diagnostics_visible_and_timestamp_unchanged(self):
        prior = datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.coordinator.last_success_at = prior
        self.coordinator.last_error_code = "read_failed"
        self.coordinator.consecutive_failures = 4
        self.coordinator.update_interval = timedelta(seconds=300)
        self.assertTrue(all(entity.available for entity in self.entities.values()))
        self.assertEqual(self.entities["connection_status"].native_value, "retrying")
        self.assertEqual(self.entities["consecutive_failures"].native_value, 4)
        self.assertEqual(self.entities["last_success"].native_value, prior)
        self.assertEqual(self.entities["connection_status"].extra_state_attributes,
                         {"last_error": "read_failed", "poll_interval_seconds": 300})

    def test_success_requires_both_verification_and_timestamp(self):
        self.coordinator.last_update_success = True
        self.assertNotEqual(self.entities["connection_status"].native_value, "verified")
        self.coordinator.last_success_at = datetime.now(timezone.utc)
        self.assertEqual(self.entities["connection_status"].native_value, "verified")


if __name__ == "__main__":
    unittest.main()

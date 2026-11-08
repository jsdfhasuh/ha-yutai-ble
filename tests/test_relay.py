"""Independent relay boundary tests: no real HA, process, or BLE traffic."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _load import integration
from test_bridge import FakeProcess, connection_failure
from test_coordinator import HAError, coordinator_module
from test_models import config_data, prop, report

models = integration("models")
bridge = integration("bridge")


def relay_report(value=True, initial=False):
    writes = int(initial is not value)
    data = report()
    data.update(
        operation="relay_control", status="relay_verified", verified=True,
        relay_writes=writes, relay_write_attempts=writes, maybe_executed=bool(writes),
        requested_value=value, initial_relay_state=initial, observed_relay_state=value,
        initial_snapshot_complete=True, verification_snapshot_complete=True, settings_unchanged=True,
    )
    data["properties"][0] = prop(2, 1, value, True)
    return data


class RelayModelTests(unittest.TestCase):
    def test_verified_on_off_and_noop_use_observed_bool(self):
        for value, initial in ((True, False), (False, True), (True, True), (False, False)):
            with self.subTest(value=value, initial=initial):
                snapshot = models.normalize_report(models.validate_config(config_data()), relay_report(value, initial), operation="relay", expected_value=value)
                self.assertIs(snapshot.measurements["relay_output"], value)
                self.assertEqual(snapshot.measurements["power"], 7)
                self.assertEqual(snapshot.settings["indicator_mode"], "inverse")

    def test_old_read_and_setting_never_accept_relay_writes(self):
        for operation, status in (("read", "read_verified"), ("setting", "setting_verified")):
            data = relay_report()
            data["status"] = status
            with self.assertRaises(models.ValidationError):
                models.normalize_report(models.validate_config(config_data()), data, operation=operation, field_name="indicator_mode", expected_value="inverse")

    def test_relay_proof_flags_counters_and_bool_types_are_strict(self):
        for field, value in (
            ("operation", "set_property"), ("status", "setting_verified"), ("verified", 1),
            ("requested_value", 1), ("requested_value", False), ("initial_relay_state", 0),
            ("observed_relay_state", 1), ("observed_relay_state", False),
            ("relay_writes", True), ("relay_writes", 2), ("relay_write_attempts", True),
            ("relay_write_attempts", 0), ("maybe_executed", False),
            ("initial_snapshot_complete", False), ("verification_snapshot_complete", False),
            ("settings_unchanged", False), ("connected_after_cleanup", True),
            ("authentication_verified", False), ("f0_verified", False),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(models.ValidationError):
                models.normalize_report(models.validate_config(config_data()), {**relay_report(), field: value}, operation="relay", expected_value=True)

    def test_noop_requires_zero_writes_and_changed_state_requires_one(self):
        noop = relay_report(True, True)
        for changes in (
            {"relay_writes": 1, "relay_write_attempts": 1, "maybe_executed": True},
            {"maybe_executed": 0},
        ):
            with self.assertRaises(models.ValidationError):
                models.normalize_report(models.validate_config(config_data()), {**noop, **changes}, operation="relay", expected_value=True)
        changed = relay_report(True, False)
        changed.update(relay_writes=0, relay_write_attempts=0, maybe_executed=False)
        with self.assertRaises(models.ValidationError):
            models.normalize_report(models.validate_config(config_data()), changed, operation="relay", expected_value=True)

    def test_relay_requires_independent_property_readback_and_no_arbitrary_field(self):
        for properties in ([], [prop(2, 1, False, True)], [prop(2, 1, True, True)] * 2):
            data = {**relay_report(), "properties": properties}
            with self.assertRaises(models.ValidationError):
                models.normalize_report(models.validate_config(config_data()), data, operation="relay", expected_value=True)
        for kwargs in ({"expected_value": 1}, {"expected_value": True, "field_name": "relay_output"}):
            with self.assertRaises(models.ValidationError):
                models.normalize_report(models.validate_config(config_data()), relay_report(), operation="relay", **kwargs)


class RelayBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bridge._LOCK = bridge.RequestQueue()
        bridge._NEXT_REQUEST_AT = 0
        self.config = models.validate_config(config_data())

    async def test_relay_payload_has_only_bool_value_and_secret_stays_on_stdin(self):
        process = FakeProcess(json.dumps(relay_report()).encode())
        launch = AsyncMock(return_value=process)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            await bridge.BridgeClient(cooldown=0).relay(self.config, True)
        self.assertEqual(json.loads(process.inputs[0]), {**self.config.worker_payload(), "operation": "relay", "value": True})
        self.assertNotIn(self.config.gatt_ltmk, repr(launch.call_args))
        launch.assert_awaited_once()

    async def test_invalid_bool_never_spawns(self):
        launch = AsyncMock()
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            for value in (0, 1, "on", None):
                with self.assertRaises(bridge.BridgeError):
                    await bridge.BridgeClient(cooldown=0).relay(self.config, value)
        launch.assert_not_awaited()

    async def test_relay_errors_never_retry_even_when_read_would_retry(self):
        for attempts, maybe, uncertain in ((0, False, False), (1, True, True), (None, None, True)):
            data = json.loads(connection_failure())
            data.update(relay_write_attempts=attempts, maybe_executed=maybe)
            process = FakeProcess(json.dumps(data).encode(), code=1)
            launch = AsyncMock(return_value=process)
            with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
                with self.assertRaises(bridge.BridgeError) as caught:
                    await bridge.BridgeClient(cooldown=0).relay(self.config, True)
            launch.assert_awaited_once()
            self.assertFalse(caught.exception.retryable)
            self.assertIs(caught.exception.may_have_written, uncertain)

    async def test_unknown_error_or_timeout_is_uncertain_and_not_retried(self):
        for process in (FakeProcess(b'{"status":"error"}', code=1), FakeProcess(block=True)):
            launch = AsyncMock(return_value=process)
            with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
                with self.assertRaises(bridge.BridgeError) as caught:
                    await bridge.BridgeClient(timeout=0.005, cooldown=0).relay(self.config, True)
            launch.assert_awaited_once()
            self.assertTrue(caught.exception.may_have_written)


class RelayCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coordinator = coordinator_module.YutaiCoordinator(None, SimpleNamespace(data=config_data(), options={}))
        self.original = models.normalize_report(models.validate_config(config_data()), report())
        self.coordinator.data = self.original

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()

    async def test_requested_output_is_not_published_before_verified_readback(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*_):
            started.set()
            await release.wait()
            return relay_report()

        self.coordinator.bridge.relay = AsyncMock(side_effect=delayed)
        task = asyncio.create_task(self.coordinator.async_set_relay(True))
        await started.wait()
        self.assertIs(self.coordinator.data, self.original)
        self.assertEqual(self.coordinator.published, [])
        release.set()
        await task
        self.assertIs(self.coordinator.data.measurements["relay_output"], True)
        self.coordinator.bridge.relay.assert_awaited_once()

    async def test_uncertain_relay_failure_recovers_only_with_read(self):
        self.coordinator.bridge.relay = AsyncMock(side_effect=bridge.BridgeError("worker_timeout", may_have_written=True))
        self.coordinator.bridge.read = AsyncMock(return_value=report())
        with patch.object(coordinator_module.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(HAError) as caught:
                await self.coordinator.async_set_relay(True)
            self.assertEqual((caught.exception.translation_domain, caught.exception.translation_key),
                             ("yutai_ble", "relay_unverified"))
            self.assertIsNone(caught.exception.translation_placeholders)
            self.assertEqual(self.coordinator.error.translation_key, "relay_unverified")
            self.assertIs(self.coordinator.data, self.original)
            self.assertEqual(self.coordinator.published, [])
            self.assertFalse(self.coordinator.last_update_success)
            self.assertEqual(self.coordinator.last_error_code, "write_result_uncertain")
            await self.coordinator._recovery_task
        self.coordinator.bridge.relay.assert_awaited_once()
        self.coordinator.bridge.read.assert_awaited_once()
        self.assertIs(self.coordinator.data.measurements["relay_output"], False)

    async def test_invalid_bool_does_not_dispatch_or_invalidate_state(self):
        self.coordinator.bridge.relay = AsyncMock()
        with self.assertRaises(HAError) as caught:
            await self.coordinator.async_set_relay(1)
        self.assertEqual((caught.exception.translation_domain, caught.exception.translation_key),
                         ("yutai_ble", "invalid_relay_value"))
        self.coordinator.bridge.relay.assert_not_awaited()
        self.assertTrue(self.coordinator.last_update_success)

    async def test_bad_relay_readback_is_not_published(self):
        self.coordinator.bridge.relay = AsyncMock(return_value=relay_report(False, False))
        with self.assertRaises(HAError):
            await self.coordinator.async_set_relay(True)
        self.assertIs(self.coordinator.data, self.original)
        self.assertEqual(self.coordinator.published, [])
        self.assertEqual(self.coordinator.last_error_code, "write_result_uncertain")

    async def test_relay_and_setting_share_the_same_request_lock(self):
        started, release = asyncio.Event(), asyncio.Event()
        setting_report = report()
        setting_report.update(status="setting_verified", verified=True)
        setting_report["settings"]["indicator_mode"] = "follow"

        async def setting(*_):
            started.set()
            await release.wait()
            return setting_report

        self.coordinator.bridge.setting = AsyncMock(side_effect=setting)
        self.coordinator.bridge.relay = AsyncMock(return_value=relay_report())
        first = asyncio.create_task(self.coordinator.async_set_setting("indicator_mode", "follow"))
        await started.wait()
        second = asyncio.create_task(self.coordinator.async_set_relay(True))
        await asyncio.sleep(0)
        self.coordinator.bridge.relay.assert_not_awaited()
        release.set()
        await asyncio.gather(first, second)
        self.coordinator.bridge.relay.assert_awaited_once()

    async def test_cancelled_relay_is_uncertain_not_retried_and_closing_blocks_new_relay(self):
        started = asyncio.Event()

        async def blocked(*_):
            started.set()
            await asyncio.Event().wait()

        self.coordinator.bridge.relay = AsyncMock(side_effect=blocked)
        task = asyncio.create_task(self.coordinator.async_set_relay(True))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.coordinator.bridge.relay.assert_awaited_once()
        self.assertIs(self.coordinator.data, self.original)
        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.last_error_code, "write_result_uncertain")
        self.assertEqual((self.coordinator.error.translation_domain, self.coordinator.error.translation_key),
                         ("yutai_ble", "relay_cancelled"))
        await self.coordinator.async_shutdown()
        with self.assertRaises(HAError) as caught:
            await self.coordinator.async_set_relay(False)
        self.assertEqual((caught.exception.translation_domain, caught.exception.translation_key),
                         ("yutai_ble", "entry_closing"))
        self.coordinator.bridge.relay.assert_awaited_once()


class EntityStub:
    def __init__(self, coordinator, field, *, measurement=False):
        self.coordinator, self.field, self.measurement = coordinator, field, measurement

    @property
    def verified_value(self):
        return self.coordinator.data.measurements.get(self.field)

    @property
    def available(self):
        return self.coordinator.last_update_success and self.verified_value is not None


def load_switch():
    stubs = {name: types.ModuleType(name) for name in (
        "homeassistant.components.switch", "homeassistant.helpers.entity", "custom_components.yutai_ble.entity",
    )}
    stubs["homeassistant.components.switch"].SwitchEntity = type("SwitchEntity", (), {})
    stubs["homeassistant.helpers.entity"].EntityCategory = SimpleNamespace(CONFIG="config")
    stubs["custom_components.yutai_ble.entity"].YutaiEntity = EntityStub
    source = Path(__file__).resolve().parents[1] / "custom_components" / "yutai_ble" / "switch.py"
    spec = importlib.util.spec_from_file_location("custom_components.yutai_ble.switch_relay_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


switch = load_switch()


class RelaySwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coordinator = SimpleNamespace(
            device_config=models.validate_config(config_data()), data=models.normalize_report(models.validate_config(config_data()), report()),
            last_update_success=True, async_set_relay=AsyncMock(), async_set_setting=AsyncMock(),
        )
        self.entity = switch.YutaiRelaySwitch(self.coordinator)

    async def test_main_switch_has_independent_identity_and_uses_measured_output(self):
        self.assertEqual(self.entity._attr_unique_id, "02:00:00:00:00:01_relay_control")
        self.assertEqual(self.entity._attr_translation_key, "relay_control")
        self.assertIsNone(self.entity._attr_entity_category)
        self.assertEqual(self.entity.field, "relay_output")
        self.assertTrue(self.entity.measurement)
        self.assertIs(self.entity.is_on, False)
        self.assertTrue(self.entity.available)
        self.coordinator.last_update_success = False
        self.assertFalse(self.entity.available)
        self.coordinator.last_update_success = True
        self.coordinator.data.measurements["relay_output"] = None
        self.assertFalse(self.entity.available)

    async def test_commands_use_relay_path_once_without_optimistic_state(self):
        await self.entity.async_turn_on()
        self.coordinator.async_set_relay.assert_awaited_once_with(True)
        self.assertIs(self.entity.is_on, False)
        self.coordinator.async_set_relay.reset_mock()
        await self.entity.async_turn_off()
        self.coordinator.async_set_relay.assert_awaited_once_with(False)
        self.coordinator.async_set_setting.assert_not_awaited()

    async def test_platform_adds_main_switch_without_capability_guessing(self):
        collected = []
        await switch.async_setup_entry(None, SimpleNamespace(runtime_data=self.coordinator), collected.extend)
        self.assertEqual(len(collected), 1)
        self.assertIsInstance(collected[0], switch.YutaiRelaySwitch)


if __name__ == "__main__":
    unittest.main()

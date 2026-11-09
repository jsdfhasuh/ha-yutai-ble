"""Partial readback must not masquerade as fresh measurements or settings."""

import asyncio
import importlib.util
import sys
import types
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from _load import COMPONENT

from test_coordinator import coordinator_module, models, HAError
from test_models import config_data, report, prop
from test_sensor import sensor


class CoordinatorEntityStub:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def available(self):
        return self.coordinator.last_update_success


def load_entity():
    stubs = {name: types.ModuleType(name) for name in (
        "homeassistant.helpers.device_registry", "homeassistant.helpers.update_coordinator")}
    stubs["homeassistant.helpers.device_registry"].CONNECTION_BLUETOOTH = "bluetooth"
    stubs["homeassistant.helpers.device_registry"].DeviceInfo = dict
    stubs["homeassistant.helpers.update_coordinator"].CoordinatorEntity = CoordinatorEntityStub
    stubs["custom_components.yutai_ble.coordinator"] = coordinator_module
    spec = importlib.util.spec_from_file_location("custom_components.yutai_ble.entity_fast_test", COMPONENT / "entity.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module.YutaiEntity


YutaiEntity = load_entity()


def partial(value=True, initial=False):
    return {"address": config_data()["address"], "model": config_data()["model"],
            "transport": "retained_session", "status": "relay_session_verified",
            "operation": "relay_control", "verification_scope": "relay_only",
            "transaction_complete": True, "session_healthy": True, "session_connected": True,
            "authentication_verified": True, "f0_verified": True, "connection_reused": True,
            "verified": True, "requested_value": value, "initial_relay_state": initial,
            "observed_relay_state": value, "initial_relay_verified": True,
            "relay_writes": int(value is not initial), "relay_write_attempts": int(value is not initial),
            "maybe_executed": value is not initial, "settings_unchanged": None,
            "properties": [prop(2, 1, value, True)], "worker_elapsed_seconds": 0.5}


class SessionModelTests(unittest.TestCase):
    def test_partial_is_not_a_snapshot_and_all_proofs_are_required(self):
        config = models.validate_config(config_data())
        self.assertTrue(models.normalize_relay_report(config, partial(), True).value)
        with self.assertRaises(models.ValidationError):
            models.normalize_report(config, partial())
        for key in partial():
            if key in {"worker_elapsed_seconds", "settings_unchanged"}:
                continue
            bad = partial()
            del bad[key]
            with self.subTest(key=key), self.assertRaises(models.ValidationError):
                models.normalize_relay_report(config, bad, True)
        for change in ({"relay_writes": True}, {"connected_after_cleanup": False},
                       {"properties": [prop(2, 1, False, True)]},
                       {"address": "02:00:00:00:00:02"}, {"settings_unchanged": True}):
            with self.subTest(change=change), self.assertRaises(models.ValidationError):
                models.normalize_relay_report(config, {**partial(), **change}, True)

    def test_full_session_needs_transport_proof(self):
        data = {**report(), **{k: v for k, v in partial().items() if k in (
            "transport", "transaction_complete", "session_healthy", "session_connected", "connection_reused")},
            "status": "read_session_verified", "operation": "read", "verification_scope": "full"}
        del data["connected_after_cleanup"]
        data["properties"] = report()["properties"][:4] + [prop(5, index, 0) for index in range(1, 8)] + [
            prop(7, 1, False, True), prop(7, 2, False, True)]
        config = models.validate_config(config_data())
        self.assertEqual(models.normalize_report(config, data).measurements["power"], 7)
        with self.assertRaises(models.ValidationError):
            models.normalize_report(config, {**data, "properties": data["properties"][:-1]})
        for key in ("session_healthy", "transaction_complete", "connection_reused"):
            bad = deepcopy(data)
            del bad[key]
            with self.assertRaises(models.ValidationError):
                models.normalize_report(config, bad)


class FastCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = coordinator_module.YutaiCoordinator(None, SimpleNamespace(data=config_data(), options={"session_idle_timeout": 90}))
        self.c.data = models.normalize_report(self.c.device_config, report())
        self.c._record_success(self.c.data)
        self.c.bridge.relay = AsyncMock(return_value=partial())
        self.c.bridge.read = AsyncMock(return_value=report())

    async def asyncTearDown(self):
        await self.c.async_shutdown()

    async def test_partial_updates_relay_without_freshening_snapshot(self):
        timestamp, data, revision = self.c.last_success_at, self.c.data, self.c._snapshot_revision
        entity = YutaiEntity(self.c, "relay_output", measurement=True)
        await self.c.async_set_relay(True)
        self.assertTrue(entity.available)
        self.assertTrue(entity.verified_value)
        self.assertIs(self.c.data, data)
        self.assertEqual(self.c.last_success_at, timestamp)
        self.assertEqual(self.c._snapshot_revision, revision)
        self.assertEqual(self.c.published, [])
        self.assertEqual(self.c.last_operation_metrics["worker_elapsed_seconds"], 0.5)
        self.c.bridge.read.assert_not_awaited()
        await self.c._fast_refresh_task
        self.c.bridge.read.assert_awaited_once()
        self.assertIsNone(self.c.relay_readback)

    async def test_partial_does_not_revive_failed_meters(self):
        self.c.last_update_success = False
        self.c._snapshot_valid = False
        await self.c.async_set_relay(True)
        self.assertFalse(self.c.last_update_success)
        self.assertTrue(YutaiEntity(self.c, "relay_output", measurement=True).available)
        self.assertFalse(YutaiEntity(self.c, "power", measurement=True).available)

    async def test_partial_cannot_satisfy_a_queued_full_read(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def relay(*_):
            started.set()
            await release.wait()
            return partial()
        self.c.bridge.relay = AsyncMock(side_effect=relay)
        first = asyncio.create_task(self.c.async_set_relay(True))
        await started.wait()
        refresh = asyncio.create_task(self.c.async_request_refresh())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, refresh)
        self.c.bridge.read.assert_awaited_once()

    async def test_consecutive_overlapping_partial_duplicates_share_verified_result(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def relay(*_):
            started.set()
            await release.wait()
            return partial()
        self.c.bridge.relay = AsyncMock(side_effect=relay)
        first = asyncio.create_task(self.c.async_set_relay(True))
        await started.wait()
        second = asyncio.create_task(self.c.async_set_relay(True))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.c.bridge.relay.assert_awaited_once()
        await self.c.async_set_relay(True)
        self.assertEqual(self.c.bridge.relay.await_count, 2)

    async def test_invalid_readback_invalidates_session_without_replay(self):
        self.c.bridge.relay = AsyncMock(return_value={**partial(), "verified": False})
        self.c.bridge.async_reset_session = AsyncMock()
        with self.assertRaises(HAError):
            await self.c.async_set_relay(True)
        self.c.bridge.relay.assert_awaited_once()
        self.c.bridge.async_reset_session.assert_awaited_once()
        self.assertIsNone(self.c.relay_readback)
        self.assertFalse(self.c.last_update_success)

    async def test_reset_failure_does_not_hide_failed_read_validation(self):
        original = self.c.data
        self.c.relay_readback = True
        self.c.bridge.read = AsyncMock(return_value={})
        self.c.bridge.async_reset_session = AsyncMock(side_effect=coordinator_module.BridgeError("cleanup_failed"))
        with patch.object(coordinator_module._LOGGER, "warning") as warning:
            await self.c.async_request_refresh()
        self.c.bridge.read.assert_awaited_once()
        self.c.bridge.async_reset_session.assert_awaited_once()
        warning.assert_called_once()
        self.assertIs(self.c.data, original)
        self.assertFalse(self.c._snapshot_valid)
        self.assertFalse(self.c.last_update_success)
        self.assertIsNone(self.c.relay_readback)
        self.assertEqual(self.c.last_error_code, "validation_failed")
        self.assertEqual(self.c.consecutive_failures, 1)
        self.assertEqual(self.c.error.translation_key, "read_unverified")

    async def test_reset_failure_does_not_hide_relay_uncertainty_or_recovery(self):
        self.c.relay_readback = True
        self.c.bridge.relay = AsyncMock(return_value={**partial(), "verified": False})
        self.c.bridge.async_reset_session = AsyncMock(side_effect=coordinator_module.BridgeError("cleanup_failed"))
        with patch.object(coordinator_module._LOGGER, "warning"), self.assertRaises(HAError) as caught:
            await self.c.async_set_relay(True)
        self.assertEqual(caught.exception.translation_key, "relay_unverified")
        self.c.bridge.relay.assert_awaited_once()
        self.c.bridge.async_reset_session.assert_awaited_once()
        self.assertFalse(self.c._snapshot_valid)
        self.assertFalse(self.c.last_update_success)
        self.assertIsNone(self.c.relay_readback)
        self.assertEqual(self.c.last_error_code, "write_result_uncertain")
        self.assertIsNotNone(self.c._recovery_task)

    async def test_reset_failure_does_not_hide_setting_uncertainty_or_recovery(self):
        self.c.bridge.setting = AsyncMock(return_value={**report(), "status": "setting_verified", "verified": False})
        self.c.bridge.async_reset_session = AsyncMock(side_effect=coordinator_module.BridgeError("cleanup_failed"))
        with patch.object(coordinator_module._LOGGER, "warning"), self.assertRaises(HAError) as caught:
            await self.c.async_set_setting("indicator_mode", "follow")
        self.assertEqual(caught.exception.translation_key, "setting_unverified")
        self.c.bridge.setting.assert_awaited_once()
        self.c.bridge.async_reset_session.assert_awaited_once()
        self.assertFalse(self.c._snapshot_valid)
        self.assertFalse(self.c.last_update_success)
        self.assertEqual(self.c.last_error_code, "write_result_uncertain")
        self.assertIsNotNone(self.c._recovery_task)

    async def test_shutdown_cleanup_failure_still_drains_tasks_and_ha_lifecycle(self):
        finished = []

        async def pending(name):
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                finished.append(name)

        active = asyncio.create_task(pending("active"))
        recovery = asyncio.create_task(pending("recovery"))
        refresh = asyncio.create_task(pending("refresh"))
        self.c._inflight.add(active)
        self.c._recovery_task, self.c._fast_refresh_task = recovery, refresh
        await asyncio.sleep(0)
        self.c.bridge.async_close = AsyncMock(side_effect=coordinator_module.BridgeError("cleanup_failed"))
        with patch.object(coordinator_module._LOGGER, "warning") as warning:
            await self.c.async_shutdown()
        warning.assert_called_once()
        self.assertCountEqual(finished, ["active", "recovery", "refresh"])
        self.assertTrue(all(task.done() for task in (active, recovery, refresh)))
        self.assertTrue(self.c.shutdown_complete)
        self.assertIsNone(self.c._recovery_task)
        self.assertIsNone(self.c._fast_refresh_task)
        self.c.bridge.async_close = AsyncMock()

    async def test_unexpected_close_failure_still_runs_ha_shutdown(self):
        self.c.bridge.async_close = AsyncMock(side_effect=RuntimeError("test_close_failure"))
        with self.assertRaisesRegex(RuntimeError, "test_close_failure"):
            await self.c.async_shutdown()
        self.assertTrue(self.c.shutdown_complete)
        self.c.bridge.async_close = AsyncMock()

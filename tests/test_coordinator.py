"""Minimal HA stubs exercise state publication, without installing HA."""

import asyncio
from datetime import timezone
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _load import integration
from test_models import config_data, report

models = integration("models")
bridge = integration("bridge")


class HAError(Exception):
    pass


class UpdateFailed(Exception):
    pass


class CoordinatorStub:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, *args, **kwargs):
        self.data = None
        self.last_update_success = True
        self.error = None
        self.published = []
        self.update_interval = kwargs.get("update_interval")
        self.always_update = kwargs.get("always_update")
        self.shutdown_complete = False

    def async_set_updated_data(self, value):
        self.data = value
        self.last_update_success = True
        self.published.append(value)

    def async_set_update_error(self, error):
        self.error = error
        self.last_update_success = False

    async def async_request_refresh(self):
        try:
            result = await self._async_update_data()
        except UpdateFailed as error:
            self.async_set_update_error(error)
        else:
            self.async_set_updated_data(result)

    async def async_shutdown(self):
        self.shutdown_complete = True


def load_coordinator():
    stubs = {}
    for name in (
        "homeassistant", "homeassistant.config_entries", "homeassistant.core",
        "homeassistant.exceptions", "homeassistant.helpers", "homeassistant.helpers.update_coordinator",
    ):
        stubs[name] = types.ModuleType(name)
    stubs["homeassistant.config_entries"].ConfigEntry = object
    stubs["homeassistant.core"].HomeAssistant = object
    stubs["homeassistant.exceptions"].HomeAssistantError = HAError
    stubs["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = CoordinatorStub
    stubs["homeassistant.helpers.update_coordinator"].UpdateFailed = UpdateFailed
    with patch.dict(sys.modules, stubs):
        return integration("coordinator")


coordinator_module = load_coordinator()


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        entry = SimpleNamespace(data=config_data(), options={})
        self.coordinator = coordinator_module.YutaiCoordinator(None, entry)
        self.original = models.normalize_report(models.validate_config(config_data()), report())
        self.coordinator.data = self.original

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()

    async def test_verified_write_publishes_readback_only(self):
        readback = report()
        readback.update(status="setting_verified", verified=True)
        readback["settings"]["indicator_mode"] = "follow"
        self.coordinator.bridge.setting = AsyncMock(return_value=readback)
        await self.coordinator.async_set_setting("indicator_mode", "follow")
        self.assertEqual(self.coordinator.data.settings["indicator_mode"], "follow")
        self.assertEqual(len(self.coordinator.published), 1)
        self.coordinator.bridge.setting.assert_awaited_once()
        self.assertEqual(self.coordinator.consecutive_failures, 0)
        self.assertIs(self.coordinator.last_success_at.tzinfo, timezone.utc)

    async def test_failed_write_does_not_optimistically_change_state_or_retry(self):
        for response in (bridge.BridgeError("worker_failed"), {**report(), "status": "setting_verified", "verified": True}):
            method = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
            self.coordinator.bridge.setting = method
            with self.assertRaises(HAError):
                await self.coordinator.async_set_setting("indicator_mode", "follow")
            self.assertIs(self.coordinator.data, self.original)
            self.assertFalse(self.coordinator.last_update_success)
            self.assertEqual(self.coordinator.published, [])
            method.assert_awaited_once()

    async def test_unverified_field_never_reaches_backend(self):
        self.coordinator.bridge.setting = AsyncMock()
        with self.assertRaises(HAError):
            await self.coordinator.async_set_setting("relay_output", True)
        self.coordinator.bridge.setting.assert_not_awaited()

    async def test_missing_meters_in_write_report_are_not_retained(self):
        readback = report()
        readback.update(status="setting_verified", verified=True, properties=[])
        self.coordinator.bridge.setting = AsyncMock(return_value=readback)
        await self.coordinator.async_set_setting("indicator_mode", "inverse")
        self.assertIsNone(self.coordinator.data.measurements["power"])

    async def test_read_error_is_sanitized(self):
        self.coordinator.bridge.read = AsyncMock(side_effect=bridge.BridgeError("secret data"))
        with self.assertRaises(UpdateFailed) as caught:
            await self.coordinator._async_update_data()
        self.assertNotIn("secret", str(caught.exception))

    async def test_refresh_failures_back_off_and_verified_success_resets_diagnostics(self):
        self.assertTrue(self.coordinator.always_update)
        self.coordinator.bridge.read = AsyncMock(side_effect=bridge.BridgeError("ble_connect_failed"))
        for count, interval in enumerate((120, 240, 300, 300), 1):
            await self.coordinator.async_request_refresh()
            self.assertEqual(self.coordinator.consecutive_failures, count)
            self.assertEqual(self.coordinator.update_interval.total_seconds(), interval)
            self.assertFalse(self.coordinator.last_update_success)
            self.assertEqual(self.coordinator.last_error_code, "ble_connect_failed")
            self.assertIsNone(self.coordinator.last_success_at)
            self.assertIs(self.coordinator.data, self.original)
            self.assertEqual(self.coordinator.published, [])
        self.coordinator.bridge.read = AsyncMock(return_value=report())
        await self.coordinator.async_request_refresh()
        self.assertEqual(self.coordinator.consecutive_failures, 0)
        self.assertEqual(self.coordinator.update_interval.total_seconds(), 60)
        self.assertTrue(self.coordinator.last_update_success)
        self.assertIsNone(self.coordinator.last_error_code)
        self.assertIs(self.coordinator.last_success_at.tzinfo, timezone.utc)

    async def test_backoff_does_not_shorten_long_configured_interval(self):
        await self.coordinator.async_shutdown()
        self.coordinator = coordinator_module.YutaiCoordinator(
            None, SimpleNamespace(data=config_data(), options={"poll_interval": 3600}))
        self.coordinator.data = self.original
        self.coordinator.bridge.read = AsyncMock(side_effect=bridge.BridgeError("worker_failed"))
        for _ in range(3):
            with self.assertRaises(UpdateFailed):
                await self.coordinator._async_update_data()
            self.assertEqual(self.coordinator.update_interval.total_seconds(), 3600)

    async def test_uncertain_write_only_recovers_by_get_without_publishing_requested_value(self):
        self.coordinator.bridge.setting = AsyncMock(
            side_effect=bridge.BridgeError("worker_failed", may_have_written=True))
        self.coordinator.bridge.read = AsyncMock(return_value=report())
        with patch.object(coordinator_module.asyncio, "sleep", AsyncMock()) as sleep:
            with self.assertRaises(HAError):
                await self.coordinator.async_set_setting("indicator_mode", "follow")
            self.assertFalse(self.coordinator.last_update_success)
            self.assertEqual(self.coordinator.last_error_code, "write_result_uncertain")
            self.assertEqual(self.coordinator.published, [])
            await self.coordinator._recovery_task
        self.coordinator.bridge.setting.assert_awaited_once()
        self.coordinator.bridge.read.assert_awaited_once()
        sleep.assert_awaited_once_with(5)
        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.data.settings["indicator_mode"], "inverse")
        self.assertEqual(len(self.coordinator.published), 1)

    async def test_unverified_recovery_remains_unavailable_without_fake_zero(self):
        self.coordinator.bridge.setting = AsyncMock(
            side_effect=bridge.BridgeError("worker_timeout", may_have_written=True))
        self.coordinator.bridge.read = AsyncMock(return_value={**report(), "status": "error"})
        with patch.object(coordinator_module.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(HAError):
                await self.coordinator.async_set_setting("indicator_mode", "follow")
            await self.coordinator._recovery_task
        self.assertFalse(self.coordinator.last_update_success)
        self.assertIs(self.coordinator.data, self.original)
        self.assertEqual(self.coordinator.published, [])
        self.assertEqual(self.coordinator.consecutive_failures, 1)
        self.assertEqual(self.coordinator.last_error_code, "validation_failed")

    async def test_shutdown_cancels_active_and_queued_operations_and_blocks_new_ones(self):
        started = asyncio.Event()

        async def blocked(*args):
            started.set()
            await asyncio.Event().wait()

        self.coordinator.bridge.setting = AsyncMock(side_effect=blocked)
        self.coordinator.bridge.read = AsyncMock()
        write = asyncio.create_task(self.coordinator.async_set_setting("indicator_mode", "follow"))
        await started.wait()
        read = asyncio.create_task(self.coordinator._async_update_data())
        await asyncio.sleep(0)
        await self.coordinator.async_shutdown()
        outcomes = await asyncio.gather(write, read, return_exceptions=True)
        self.assertTrue(all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes))
        self.assertTrue(self.coordinator.shutdown_complete)
        self.assertTrue(self.coordinator.bridge._closing)
        self.assertEqual(self.coordinator._inflight, set())
        self.assertIsNone(self.coordinator._recovery_task)
        self.coordinator.bridge.read.assert_not_awaited()
        with self.assertRaises(HAError):
            await self.coordinator.async_set_setting("indicator_mode", "follow")
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()

    async def test_cancelled_write_is_unavailable_and_never_retried(self):
        started = asyncio.Event()

        async def blocked(*args):
            started.set()
            await asyncio.Event().wait()

        self.coordinator.bridge.setting = AsyncMock(side_effect=blocked)
        task = asyncio.create_task(self.coordinator.async_set_setting("indicator_mode", "follow"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.coordinator.bridge.setting.assert_awaited_once()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.last_error_code, "request_cancelled")
        self.assertIs(self.coordinator.data, self.original)

    async def test_repeated_shutdown_cancellation_still_finishes_drain(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def close():
            started.set()
            await release.wait()

        self.coordinator.bridge.async_close = AsyncMock(side_effect=close)
        task = asyncio.create_task(self.coordinator.async_shutdown())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.coordinator.shutdown_complete)
        self.assertEqual(self.coordinator._inflight, set())


if __name__ == "__main__":
    unittest.main()

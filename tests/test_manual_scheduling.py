"""Manual latency reductions preserve ordering, readback and write uncertainty."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _load import integration
from test_bridge import FakeProcess, connection_failure
from test_coordinator import coordinator_module, HAError
from test_models import config_data, report
from test_relay import relay_report

bridge = integration("bridge")
models = integration("models")


def setting_report(mode):
    result = report()
    result.update(status="setting_verified", verified=True)
    result["settings"]["indicator_mode"] = mode
    return result


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bridge._LOCK = bridge.RequestQueue()
        bridge._NEXT_REQUEST_AT = 0
        self.coordinator = coordinator_module.YutaiCoordinator(None, SimpleNamespace(data=config_data(), options={}))
        self.coordinator.data = models.normalize_report(self.coordinator.device_config, report())

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()

    async def test_queued_read_uses_write_readback_and_manual_fifo_wins(self):
        started, release = asyncio.Event(), asyncio.Event()
        order = []

        async def write(_config, value):
            order.append(value)
            if len(order) == 1:
                started.set()
                await release.wait()
            return relay_report(value, not value)

        self.coordinator.bridge.relay = AsyncMock(side_effect=write)
        self.coordinator.bridge.read = AsyncMock(return_value=report())
        first = asyncio.create_task(self.coordinator.async_set_relay(True))
        await started.wait()
        refresh = asyncio.create_task(self.coordinator.async_request_refresh())
        second = asyncio.create_task(self.coordinator.async_set_relay(False))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, refresh, second)
        self.assertEqual(order, [True, False])
        self.coordinator.bridge.read.assert_not_awaited()
        self.assertIs(self.coordinator.data.measurements["relay_output"], False)

    async def test_only_overlapping_consecutive_identical_commands_are_coalesced(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def write(_config, value):
            started.set()
            await release.wait()
            return relay_report(value, not value)

        self.coordinator.bridge.relay = AsyncMock(side_effect=write)
        tasks = [asyncio.create_task(self.coordinator.async_set_relay(True))]
        await started.wait()
        tasks += [asyncio.create_task(self.coordinator.async_set_relay(True)) for _ in range(2)]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        self.coordinator.bridge.relay.assert_awaited_once()
        self.assertEqual(len(self.coordinator.published), 1)
        # A later explicit request must check the device, not trust the old snapshot.
        await self.coordinator.async_set_relay(True)
        self.assertEqual(self.coordinator.bridge.relay.await_count, 2)

    async def test_on_off_on_and_interleaved_setting_are_not_coalesced(self):
        order = []
        started, release = asyncio.Event(), asyncio.Event()

        async def relay(_config, value):
            order.append(("relay", value))
            if len(order) == 1:
                started.set()
                await release.wait()
            return relay_report(value, not value)

        async def setting(_config, field, value):
            order.append((field, value))
            return setting_report(value)

        self.coordinator.bridge.relay = AsyncMock(side_effect=relay)
        self.coordinator.bridge.setting = AsyncMock(side_effect=setting)
        tasks = [asyncio.create_task(self.coordinator.async_set_relay(True))]
        await started.wait()
        tasks += [asyncio.create_task(self.coordinator.async_set_relay(False)),
                  asyncio.create_task(self.coordinator.async_set_relay(True)),
                  asyncio.create_task(self.coordinator.async_set_setting("indicator_mode", "follow")),
                  asyncio.create_task(self.coordinator.async_set_relay(True))]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(order, [("relay", True), ("relay", False), ("relay", True),
                                 ("indicator_mode", "follow"), ("relay", True)])

    async def test_duplicate_settings_share_one_verified_transaction(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def setting(*_):
            started.set()
            await release.wait()
            return setting_report("follow")

        self.coordinator.bridge.setting = AsyncMock(side_effect=setting)
        first = asyncio.create_task(self.coordinator.async_set_setting("indicator_mode", "follow"))
        await started.wait()
        second = asyncio.create_task(self.coordinator.async_set_setting("indicator_mode", "follow"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.coordinator.bridge.setting.assert_awaited_once()

    async def test_overlapping_refreshes_share_one_new_snapshot(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def read(*_args, **_kwargs):
            started.set()
            await release.wait()
            return report()

        self.coordinator.bridge.read = AsyncMock(side_effect=read)
        first = asyncio.create_task(self.coordinator.async_request_refresh())
        await started.wait()
        second = asyncio.create_task(self.coordinator.async_request_refresh())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.coordinator.bridge.read.assert_awaited_once()
        await self.coordinator.async_request_refresh()
        self.assertEqual(self.coordinator.bridge.read.await_count, 2)

    async def test_failed_duplicate_is_not_reported_as_success_or_automatically_retried(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def write(*_args):
            started.set()
            await release.wait()
            raise bridge.BridgeError("worker_timeout", may_have_written=True)

        self.coordinator.bridge.relay = AsyncMock(side_effect=write)
        first = asyncio.create_task(self.coordinator.async_set_relay(True))
        await started.wait()
        second = asyncio.create_task(self.coordinator.async_set_relay(True))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(result, HAError) for result in results))
        # Two explicit user requests, at most one attempt each; no hidden retry.
        self.assertEqual(self.coordinator.bridge.relay.await_count, 2)
        self.assertEqual(self.coordinator.published, [])

    async def test_cancelled_queued_read_does_not_invalidate_verified_state(self):
        async with self.coordinator._request_lock.hold(manual=True):
            self.coordinator._record_success(self.coordinator.data)
            task = asyncio.create_task(self.coordinator._async_update_data())
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(self.coordinator._snapshot_valid)
        self.assertTrue(self.coordinator.last_update_success)
        self.assertIsNone(self.coordinator.last_error_code)

    async def test_deferred_read_is_not_failure_and_reuses_following_manual_snapshot(self):
        started = asyncio.Event()

        async def read(_config, *, interrupt):
            started.set()
            await interrupt.wait()
            raise bridge.ReadDeferred

        self.coordinator.bridge.read = AsyncMock(side_effect=read)
        self.coordinator.bridge.relay = AsyncMock(return_value=relay_report())
        refresh = asyncio.create_task(self.coordinator.async_request_refresh())
        await started.wait()
        await self.coordinator.async_set_relay(True)
        await refresh
        self.coordinator.bridge.read.assert_awaited_once()
        self.assertEqual(self.coordinator.consecutive_failures, 0)
        self.assertTrue(self.coordinator.last_update_success)
        self.assertIs(self.coordinator.data.measurements["relay_output"], True)

    async def test_cancelled_queued_manual_does_not_invalidate_successful_active_request(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def relay(*_):
            started.set()
            await release.wait()
            return relay_report()

        self.coordinator.bridge.relay = AsyncMock(side_effect=relay)
        first = asyncio.create_task(self.coordinator.async_set_relay(True))
        await started.wait()
        cancelled = asyncio.create_task(self.coordinator.async_set_relay(False))
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        release.set()
        await first
        self.assertTrue(self.coordinator.last_update_success)
        self.assertIsNone(self.coordinator._recovery_task)
        self.assertFalse(self.coordinator._manual_requested.is_set())
        self.assertEqual(self.coordinator._manual_count, 0)
        self.coordinator.bridge.relay.assert_awaited_once()

    async def test_failed_write_cannot_satisfy_queued_refresh(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def relay(*_):
            started.set()
            await release.wait()
            raise bridge.BridgeError("worker_timeout", may_have_written=True)

        self.coordinator.bridge.relay = AsyncMock(side_effect=relay)
        self.coordinator.bridge.read = AsyncMock(return_value=report())
        first = asyncio.create_task(self.coordinator.async_set_relay(True))
        await started.wait()
        refresh = asyncio.create_task(self.coordinator.async_request_refresh())
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, refresh, return_exceptions=True)
        self.assertIsInstance(results[0], HAError)
        self.coordinator.bridge.read.assert_awaited_once()
        self.assertIs(self.coordinator.data.measurements["relay_output"], False)

    async def test_delayed_recovery_skips_connection_after_intervening_verified_command(self):
        sleeping, release = asyncio.Event(), asyncio.Event()

        async def sleep(_delay):
            sleeping.set()
            await release.wait()

        self.coordinator.bridge.relay = AsyncMock(side_effect=bridge.BridgeError("worker_failed", may_have_written=True))
        self.coordinator.bridge.read = AsyncMock()
        with patch.object(coordinator_module.asyncio, "sleep", sleep):
            with self.assertRaises(HAError):
                await self.coordinator.async_set_relay(True)
            recovery = self.coordinator._recovery_task
            await sleeping.wait()
            self.coordinator.bridge.relay = AsyncMock(return_value=relay_report(False, False))
            await self.coordinator.async_set_relay(False)
            release.set()
            await recovery
        self.coordinator.bridge.read.assert_not_awaited()


class BridgeSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bridge._LOCK = bridge.RequestQueue()
        bridge._NEXT_REQUEST_AT = 0
        self.config = models.validate_config(config_data())

    async def test_manual_global_priority_across_devices(self):
        order = []

        class RecordingProcess(FakeProcess):
            async def communicate(self, payload):
                order.append(json.loads(payload)["operation"])
                return await super().communicate(payload)

        client = bridge.BridgeClient(cooldown=0)
        launch = AsyncMock(side_effect=[RecordingProcess(), RecordingProcess()])
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            async with bridge._LOCK.hold():
                read = asyncio.create_task(client.read(self.config))
                other = models.DeviceConfig("02:00:00:00:00:02", self.config.model, "cd" * 32)
                write = asyncio.create_task(client.relay(other, True))
                await asyncio.sleep(0)
            await asyncio.gather(read, write)
        self.assertEqual(order, ["relay", "read"])

    async def test_manual_interrupts_read_retry_wait_without_second_connection(self):
        interrupt = asyncio.Event()
        process = FakeProcess(connection_failure(), code=1)
        launch = AsyncMock(return_value=process)
        client = bridge.BridgeClient(cooldown=0)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            task = asyncio.create_task(client.read(self.config, interrupt=interrupt))
            await process.started.wait()
            await asyncio.sleep(0)
            interrupt.set()
            with self.assertRaises(bridge.ReadDeferred):
                await asyncio.wait_for(task, 0.5)
        launch.assert_awaited_once()
        self.assertIsNotNone(process.returncode)

    async def test_manual_arriving_during_read_cooldown_goes_first(self):
        import time

        bridge._NEXT_REQUEST_AT = time.monotonic() + 0.03
        order = []

        class RecordingProcess(FakeProcess):
            async def communicate(self, payload):
                order.append(json.loads(payload)["operation"])
                return await super().communicate(payload)

        launch = AsyncMock(side_effect=[RecordingProcess(), RecordingProcess()])
        client = bridge.BridgeClient(cooldown=0)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            read = asyncio.create_task(client.read(self.config))
            await asyncio.sleep(0)
            write = asyncio.create_task(client.relay(self.config, False))
            await asyncio.gather(read, write)
        self.assertEqual(order, ["relay", "read"])

    async def test_read_cooldown_yields_before_spawning(self):
        import time

        interrupt = asyncio.Event()
        bridge._NEXT_REQUEST_AT = time.monotonic() + 10
        launch = AsyncMock()
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            task = asyncio.create_task(bridge.BridgeClient().read(self.config, interrupt=interrupt))
            await asyncio.sleep(0)
            interrupt.set()
            with self.assertRaises(bridge.ReadDeferred):
                await asyncio.wait_for(task, 0.5)
        launch.assert_not_awaited()

    async def test_pending_manual_does_not_interrupt_inflight_transaction(self):
        interrupt = asyncio.Event()
        started, release = asyncio.Event(), asyncio.Event()
        process = FakeProcess()

        async def communicate(payload):
            started.set()
            await release.wait()
            process.returncode = 0
            return b"{}", b""

        process.communicate = communicate
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(bridge.BridgeClient(cooldown=0).read(self.config, interrupt=interrupt))
            await started.wait()
            interrupt.set()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertFalse(process.terminated)
            release.set()
            self.assertEqual(await task, {})

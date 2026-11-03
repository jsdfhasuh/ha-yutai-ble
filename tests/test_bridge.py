"""Mocked processes: no Bluetooth, network, HA, credentials or child launch."""

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _load import integration

bridge = integration("bridge")
models = integration("models")
const = integration("const")


def connection_failure(**changes):
    return json.dumps({
        "address": "02:00:00:00:00:01", "model": const.SUPPORTED_MODEL,
        "status": "error", "phase": "ble_connect", "error_class": "BleakError",
        "error_code": "probe_failed", "connected_after_cleanup": False,
        "connection_attempted": True, "disconnect_attempted": True, "relay_writes": 0,
        "setting_write_attempts": 0, "maybe_executed": False, **changes,
    }).encode()


class FakeProcess:
    def __init__(self, stdout=b'{}', *, code=0, block=False, delay=0):
        self.stdout = stdout
        self.code = code
        self.block = block
        self.delay = delay
        self.returncode = None
        self.inputs = []
        self.terminated = False
        self.waited = False
        self.finished_at = None
        self.started = asyncio.Event()

    async def communicate(self, payload):
        self.inputs.append(payload)
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.returncode = self.code
        self.finished_at = time.monotonic()
        return self.stdout, b"private stderr must never surface"

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.finished_at = time.monotonic()

    async def wait(self):
        self.waited = True
        return self.returncode

    def kill(self):
        self.returncode = -9


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bridge._LOCK = asyncio.Lock()
        bridge._NEXT_REQUEST_AT = 0
        self.config = models.DeviceConfig("02:00:00:00:00:01", const.SUPPORTED_MODEL, "ab" * 32)

    async def test_secret_only_on_stdin_not_args_or_environment(self):
        process = FakeProcess()
        launch = AsyncMock(return_value=process)
        worker = Path("test-only-worker.py")
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            await bridge.BridgeClient(worker, cooldown=0).read(self.config)
        args, kwargs = launch.call_args
        self.assertEqual(args, (sys.executable, "-B", str(worker)))
        self.assertNotIn(self.config.gatt_ltmk, repr((args, kwargs)))
        self.assertNotIn("env", kwargs)
        self.assertEqual(json.loads(process.inputs[0]), {**self.config.worker_payload(), "operation": "read"})

    async def test_setting_does_not_retry(self):
        process = FakeProcess(code=1)
        launch = AsyncMock(return_value=process)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            with self.assertRaises(bridge.BridgeError):
                await bridge.BridgeClient(cooldown=0).setting(self.config, "indicator_mode", "inverse")
        self.assertEqual(launch.await_count, 1)
        self.assertEqual(len(process.inputs), 1)
        payload = json.loads(process.inputs[0])
        self.assertEqual((payload["operation"], payload["field"], payload["value"]), ("setting", "indicator_mode", "inverse"))

    async def test_read_connect_failure_retries_once_after_five_seconds_and_reaping(self):
        now = [0]
        for error_class in ("BleakError", "BleakDBusError", "BleakDeviceNotFoundError", "TimeoutError"):
            with self.subTest(error_class=error_class):
                processes = [FakeProcess(connection_failure(error_class=error_class), code=1), FakeProcess()]
                launch = AsyncMock(side_effect=processes)
                sleeps = []

                async def sleep(delay):
                    self.assertIsNotNone(processes[0].returncode)
                    sleeps.append(delay)
                    now[0] += delay

                with patch.object(bridge, "time", SimpleNamespace(monotonic=lambda: now[0])), patch.object(
                        bridge.asyncio, "create_subprocess_exec", launch), patch.object(
                        bridge.asyncio, "sleep", sleep):
                    await bridge.BridgeClient(cooldown=0).read(self.config)
                self.assertEqual(sleeps, [5])
                self.assertEqual(launch.await_count, 2)
                self.assertTrue(all(json.loads(p.inputs[0])["operation"] == "read" for p in processes))

    async def test_second_read_failure_stops_and_safe_error_is_fixed(self):
        now = [0]

        async def sleep(delay):
            now[0] += delay

        launch = AsyncMock(side_effect=[FakeProcess(connection_failure(), code=1),
                                       FakeProcess(connection_failure(), code=1)])
        with patch.object(bridge, "time", SimpleNamespace(monotonic=lambda: now[0])), patch.object(
                bridge.asyncio, "create_subprocess_exec", launch), patch.object(
                bridge.asyncio, "sleep", sleep):
            with self.assertRaises(bridge.BridgeError) as caught:
                await bridge.BridgeClient(cooldown=0).read(self.config)
        self.assertEqual(launch.await_count, 2)
        self.assertEqual(caught.exception.code, "ble_connect_failed")
        self.assertFalse(caught.exception.may_have_written)

    async def test_retry_waits_remaining_time_after_early_wakeup(self):
        now, sleeps, starts = [0], [], []
        processes = [FakeProcess(connection_failure(), code=1), FakeProcess()]

        async def launch(*args, **kwargs):
            starts.append(now[0])
            return processes[len(starts) - 1]

        async def sleep(delay):
            sleeps.append(delay)
            now[0] += 2 if len(sleeps) == 1 else delay

        with patch.object(bridge, "time", SimpleNamespace(monotonic=lambda: now[0])), patch.object(
                bridge.asyncio, "create_subprocess_exec", launch), patch.object(
                bridge.asyncio, "sleep", sleep):
            await bridge.BridgeClient(cooldown=0).read(self.config)
        self.assertEqual(sleeps, [5, 3])
        self.assertEqual(starts, [0, 5])

    async def test_retry_requires_strict_failed_read_attempt_and_disconnect_attestations(self):
        mutations = (
            {"status": None}, {"status": "read_verified"},
            {"connection_attempted": None}, {"connection_attempted": False}, {"connection_attempted": 1},
            {"disconnect_attempted": None}, {"disconnect_attempted": False}, {"disconnect_attempted": 1},
            {"relay_writes": None}, {"relay_writes": False}, {"relay_writes": True},
            {"relay_writes": 0.0}, {"relay_writes": -1}, {"relay_writes": 1},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                launch = AsyncMock(return_value=FakeProcess(connection_failure(**changes), code=1))
                with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
                    with self.assertRaises(bridge.BridgeError) as caught:
                        await bridge.BridgeClient(cooldown=0).read(self.config)
                self.assertEqual(launch.await_count, 1)
                self.assertFalse(caught.exception.retryable)

    async def test_long_worker_timeout_in_connect_phase_does_not_retry(self):
        process = FakeProcess(connection_failure(error_class="TimeoutError", error_code="phase_timeout"), code=1)
        now = [0]

        async def communicate(payload):
            process.returncode = 1
            now[0] = 62
            return process.stdout, b""

        process.communicate = communicate
        launch = AsyncMock(return_value=process)
        with patch.object(bridge, "time", SimpleNamespace(monotonic=lambda: now[0])), patch.object(
                bridge.asyncio, "create_subprocess_exec", launch):
            with self.assertRaises(bridge.BridgeError) as caught:
                await bridge.BridgeClient(cooldown=0).read(self.config)
        self.assertEqual(launch.await_count, 1)
        self.assertFalse(caught.exception.retryable)

    async def test_auth_cleanup_unknown_and_unconfirmed_disconnect_never_retry(self):
        reports = [
            connection_failure(phase="authentication_proof"),
            connection_failure(cleanup_error_code="disconnect_failed_or_timed_out"),
            connection_failure(error_class="UnknownPrivateError"),
            connection_failure(error_code="unknown_error"),
            connection_failure(connected_after_cleanup=True),
            connection_failure(connected_after_cleanup=None),
            connection_failure(address="02:00:00:00:00:02"),
            connection_failure(model="yutai.plug.fsov7m"),
            connection_failure(phase=["bad"], error_class={"private": "value"}),
            b"not json",
        ]
        for stdout in reports:
            with self.subTest(stdout=stdout):
                launch = AsyncMock(return_value=FakeProcess(stdout, code=1))
                with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
                    with self.assertRaises(bridge.BridgeError):
                        await bridge.BridgeClient(cooldown=0).read(self.config)
                self.assertEqual(launch.await_count, 1)

    async def test_write_failure_never_retries_and_reports_uncertainty_conservatively(self):
        for changes, uncertain in (({}, False),
                                   ({"setting_write_attempts": 1, "maybe_executed": True}, True),
                                   ({"setting_write_attempts": None, "maybe_executed": None}, True)):
            with self.subTest(changes=changes):
                launch = AsyncMock(return_value=FakeProcess(connection_failure(**changes), code=1))
                with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
                    with self.assertRaises(bridge.BridgeError) as caught:
                        await bridge.BridgeClient(cooldown=0).setting(self.config, "indicator_mode", "inverse")
                self.assertEqual(launch.await_count, 1)
                self.assertFalse(caught.exception.retryable)
                self.assertIs(caught.exception.may_have_written, uncertain)

    async def test_global_gate_and_post_success_cooldown(self):
        processes = [FakeProcess(delay=0.015), FakeProcess()]
        starts = []

        async def launch(*args, **kwargs):
            starts.append(time.monotonic())
            return processes[len(starts) - 1]

        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            await asyncio.gather(
                bridge.BridgeClient(cooldown=0.02).read(self.config),
                bridge.BridgeClient(cooldown=0.02).read(self.config),
            )
        self.assertGreaterEqual(starts[1] - processes[0].finished_at, 0.018)

    async def test_cooldown_after_failure(self):
        processes = [FakeProcess(code=1), FakeProcess()]
        starts = []

        async def launch(*args, **kwargs):
            starts.append(time.monotonic())
            return processes[len(starts) - 1]

        client = bridge.BridgeClient(cooldown=0.02)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            with self.assertRaises(bridge.BridgeError):
                await client.read(self.config)
            await client.read(self.config)
        self.assertGreaterEqual(starts[1] - processes[0].finished_at, 0.018)

    async def test_timeout_terminates_and_reaps_without_retry(self):
        process = FakeProcess(block=True)
        launch = AsyncMock(return_value=process)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            with self.assertRaisesRegex(bridge.BridgeError, "worker_timeout"):
                await bridge.BridgeClient(timeout=0.005, cooldown=0).read(self.config)
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertEqual(launch.await_count, 1)

    async def test_termination_allows_ten_second_cleanup_grace(self):
        process = FakeProcess()
        timeouts = []

        async def wait_for(awaitable, timeout):
            timeouts.append(timeout)
            return await awaitable

        with patch.object(bridge.asyncio, "wait_for", wait_for):
            await bridge.BridgeClient._stop(process)
        self.assertEqual(timeouts, [10])
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

    async def test_termination_kills_and_reaps_when_grace_expires(self):
        process = FakeProcess()
        process.wait = AsyncMock(return_value=-9)

        async def expired(awaitable, timeout):
            await awaitable
            raise TimeoutError

        with patch.object(bridge.asyncio, "wait_for", expired):
            await bridge.BridgeClient._stop(process)
        self.assertEqual(process.returncode, -9)
        self.assertEqual(process.wait.await_count, 2)

    async def test_cancellation_reaps_and_preserves_cooldown(self):
        process = FakeProcess(block=True)
        next_process = FakeProcess()
        launch = AsyncMock(side_effect=[process, next_process])
        client = bridge.BridgeClient(timeout=1, cooldown=0.02)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            task = asyncio.create_task(client.read(self.config))
            await process.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(process.terminated)
            self.assertTrue(process.waited)
            await client.read(self.config)
        self.assertGreaterEqual(next_process.finished_at - process.finished_at, 0.018)

    async def test_repeated_cancel_cannot_skip_cleanup_or_cooldown(self):
        process = FakeProcess(block=True)
        cleanup_started, release = asyncio.Event(), asyncio.Event()

        def terminate():
            process.terminated = True
            cleanup_started.set()

        async def wait():
            process.waited = True
            await release.wait()
            process.returncode = -15
            return -15

        process.terminate, process.wait = terminate, wait
        client = bridge.BridgeClient(cooldown=0.02)
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(client.read(self.config))
            await process.started.wait()
            task.cancel()
            await cleanup_started.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.waited)
        self.assertEqual(process.returncode, -15)
        self.assertEqual(client._inflight, set())
        self.assertGreater(bridge._NEXT_REQUEST_AT, time.monotonic())

    async def test_cancellation_during_spawn_still_reaps_eventual_child(self):
        process = FakeProcess()
        started, release = asyncio.Event(), asyncio.Event()

        async def launch(*args, **kwargs):
            started.set()
            await release.wait()
            return process

        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            task = asyncio.create_task(bridge.BridgeClient(cooldown=0).read(self.config))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertEqual(process.inputs, [])

    async def test_close_drains_active_and_queued_requests_and_blocks_new_ones(self):
        process = FakeProcess(block=True)
        client = bridge.BridgeClient(cooldown=0)
        launch = AsyncMock(return_value=process)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            first = asyncio.create_task(client.read(self.config))
            await process.started.wait()
            second = asyncio.create_task(client.setting(self.config, "indicator_mode", "inverse"))
            await asyncio.sleep(0)
            await client.async_close()
            outcomes = await asyncio.gather(first, second, return_exceptions=True)
            with self.assertRaisesRegex(bridge.BridgeError, "entry_closing"):
                await client.read(self.config)
        self.assertTrue(all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes))
        self.assertTrue(process.waited)
        self.assertEqual(launch.await_count, 1)
        self.assertEqual(client._inflight, set())

    async def test_invalid_output_exit_and_os_errors_do_not_leak(self):
        secret = self.config.gatt_ltmk.encode()
        for process in [FakeProcess(stdout=secret), FakeProcess(stdout=b'[]'), FakeProcess(stdout=secret, code=1)]:
            with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                with self.assertRaises(bridge.BridgeError) as caught:
                    await bridge.BridgeClient(cooldown=0).read(self.config)
            self.assertNotIn(self.config.gatt_ltmk, str(caught.exception))
            self.assertNotIn("private stderr", str(caught.exception))
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError(self.config.gatt_ltmk))):
            with self.assertRaises(bridge.BridgeError) as caught:
                await bridge.BridgeClient(cooldown=0).read(self.config)
        self.assertEqual(str(caught.exception), "worker_unavailable")

    async def test_error_report_messages_and_unknown_codes_never_surface(self):
        stdout = connection_failure(error_class=self.config.gatt_ltmk,
                                    error_code=self.config.gatt_ltmk,
                                    error_message=self.config.gatt_ltmk)
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(
                return_value=FakeProcess(stdout, code=1))):
            with self.assertRaises(bridge.BridgeError) as caught:
                await bridge.BridgeClient(cooldown=0).read(self.config)
        self.assertEqual(str(caught.exception), "worker_failed")
        self.assertEqual(str(bridge.BridgeError(self.config.gatt_ltmk)), "worker_failed")

    def test_production_defaults(self):
        client = bridge.BridgeClient()
        self.assertEqual(client.timeout, 100)
        self.assertEqual(client.cooldown, 3)
        self.assertEqual(client.worker_path.name, "bridge_worker.py")


if __name__ == "__main__":
    unittest.main()

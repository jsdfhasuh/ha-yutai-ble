"""Resident subprocess transport tests without Bluetooth or real child processes."""

import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _load import integration

bridge = integration("bridge")
session = integration("session_bridge")
models = integration("models")


class StreamProcess:
    def __init__(self, respond=None):
        self.returncode = None
        self.stdin = self
        self.stdout = asyncio.StreamReader(limit=session._MAX_REPORT_BYTES)
        self.inputs = []
        self.respond = respond
        self.identity = {}
        self.relay_state = False
        self.started = asyncio.Event()
        self.terminated = False
        self.waited = False
        self.closed_at = None
        self.block_drain = False

    def write(self, data):
        request = json.loads(data)
        self.inputs.append(request)
        operation = request["operation"]
        if operation == "init":
            self.identity = {name: request[name] for name in ("address", "model")}
            response = {"id": 0, "status": "ready", **self.identity}
        else:
            self.started.set()
            if operation == "close":
                report = {**self.identity, "status": "close_session_verified", "connected_after_cleanup": False}
            else:
                report = self.success(request)
            response = {"id": request["id"], "report": report}
        if self.respond is not None:
            response = self.respond(request, response)
        if response is None:
            return
        if response == b"":
            self.stdout.feed_eof()
        else:
            self.stdout.feed_data(response if isinstance(response, bytes) else json.dumps(response).encode() + b"\n")
        if operation == "close":
            self.returncode = 0
            self.closed_at = time.monotonic()

    def success(self, request):
        report = {**self.identity, "transport": "retained_session", "transaction_complete": True,
                  "session_healthy": True, "session_connected": True, "authentication_verified": True,
                  "f0_verified": True, "connection_reused": request["id"] > 1,
                  "connect_auth_elapsed_seconds": 0.1, "operation_elapsed_seconds": 0.01}
        if request["operation"] == "read":
            report.update(status="read_session_verified", operation="read", verification_scope="full",
                          relay_writes=0, settings={}, capabilities={}, properties=[])
        else:
            initial, value = self.relay_state, request["value"]
            writes = int(initial != value)
            self.relay_state = value
            report.update(status="relay_session_verified", operation="relay_control", verification_scope="relay_only",
                          verified=True, initial_relay_verified=True, requested_value=value,
                          initial_relay_state=initial, observed_relay_state=value,
                          relay_write_attempts=writes, relay_writes=writes, maybe_executed=bool(writes),
                          properties=[{"siid": 2, "piid": 1, "value": value}], settings_unchanged=None)
        return report

    async def drain(self):
        if self.block_drain and len(self.inputs) > 1:
            await asyncio.Event().wait()

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.closed_at = time.monotonic()

    def kill(self):
        self.returncode = -9

    async def wait(self):
        self.waited = True
        return self.returncode


class OneShotProcess:
    def __init__(self):
        self.inputs = []
        self.returncode = None

    async def communicate(self, payload):
        self.inputs.append(json.loads(payload))
        self.returncode = 0
        return b"{}", b""


class SessionBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bridge._LOCK = bridge.RequestQueue()
        bridge._NEXT_REQUEST_AT = 0
        self.config = models.DeviceConfig("02:00:00:00:00:01", "yutai.plug.fsov8m", "ab" * 32)

    def client(self, **kwargs):
        return bridge.BridgeClient(session_idle_timeout=90, cooldown=0, **kwargs)

    async def test_reuses_worker_credentials_only_init_stdin_and_no_success_cooldown(self):
        process = StreamProcess()
        launch = AsyncMock(return_value=process)
        client = bridge.BridgeClient(session_idle_timeout=90, cooldown=3)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            first = await client.read(self.config)
            second = await client.relay(self.config, True)
            self.assertEqual(bridge._NEXT_REQUEST_AT, 0)
            self.assertFalse(process.terminated)
            await client.async_close()
        self.assertEqual(launch.await_count, 1)
        args, kwargs = launch.call_args
        self.assertTrue(args[-1].endswith("session_worker.py"))
        self.assertNotIn(self.config.gatt_ltmk, repr((args, kwargs)))
        self.assertEqual(kwargs["stderr"], asyncio.subprocess.DEVNULL)
        self.assertNotIn("env", kwargs)
        self.assertEqual([item["id"] for item in process.inputs], [0, 1, 2, 3])
        self.assertEqual(process.inputs[0]["gatt_ltmk"], self.config.gatt_ltmk)
        self.assertTrue(all("gatt_ltmk" not in item for item in process.inputs[1:]))
        self.assertFalse(first["connection_reused"])
        self.assertTrue(second["connection_reused"])
        self.assertTrue(process.waited)
        self.assertEqual(process.returncode, 0)
        self.assertGreater(bridge._NEXT_REQUEST_AT, time.monotonic())
        for report in (first, second):
            self.assertGreaterEqual(report["queue_wait_seconds"], 0)
            self.assertGreaterEqual(report["worker_elapsed_seconds"], 0)

    async def test_noop_relay_is_verified_without_a_write(self):
        process, client = StreamProcess(), self.client()
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            report = await client.relay(self.config, False)
            self.assertEqual(report["relay_writes"], 0)
            await client.async_close()

    async def test_other_models_still_use_legacy_worker(self):
        process, client = OneShotProcess(), self.client()
        config = models.DeviceConfig(self.config.address, "yutai.plug.fsov7m", self.config.gatt_ltmk)
        launch = AsyncMock(return_value=process)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            await client.read(config)
        self.assertTrue(launch.call_args.args[-1].endswith("bridge_worker.py"))
        self.assertFalse(client._session.started)

    async def test_idle_session_does_not_hold_global_gate(self):
        resident, legacy = StreamProcess(), OneShotProcess()
        client = self.client()
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[resident, legacy])):
            await client.read(self.config)
            await asyncio.wait_for(bridge.BridgeClient(cooldown=0).read(self.config), 0.5)
            self.assertIsNone(resident.returncode)
            await client.async_close()

    async def test_active_transaction_is_not_preempted_and_queued_manual_goes_first(self):
        held = []

        def respond(request, response):
            if request["operation"] == "read":
                held.append(response)
                return None
            return response

        processes = [StreamProcess(respond), StreamProcess(), StreamProcess()]
        clients = [self.client() for _ in range(3)]
        configs = [models.DeviceConfig(f"02:00:00:00:00:0{i}", self.config.model, self.config.gatt_ltmk)
                   for i in range(1, 4)]
        launch = AsyncMock(side_effect=processes)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            active = asyncio.create_task(clients[0].read(configs[0]))
            await processes[0].started.wait()
            queued_read = asyncio.create_task(clients[1].read(configs[1]))
            await asyncio.sleep(0)
            queued_relay = asyncio.create_task(clients[2].relay(configs[2], True))
            await asyncio.sleep(0)
            self.assertEqual(launch.await_count, 1)
            self.assertFalse(active.done())
            processes[0].stdout.feed_data(json.dumps(held[0]).encode() + b"\n")
            await asyncio.gather(active, queued_read, queued_relay)
            self.assertEqual(processes[1].inputs[0]["address"], configs[2].address)
            self.assertEqual(processes[1].inputs[1]["operation"], "relay")
            self.assertEqual(processes[2].inputs[0]["address"], configs[1].address)
            for client in clients:
                await client.async_close()

    async def test_safe_read_connect_failure_retries_once_after_reaping(self):
        def respond(request, response):
            if request["operation"] != "read":
                return response
            return {"id": request["id"], "report": {
                "address": self.config.address, "model": self.config.model, "status": "error",
                "phase": "ble_connect", "error_class": "BleakError", "error_code": "probe_failed",
                "relay_writes": 0, "maybe_executed": False, "connection_attempted": True,
                "disconnect_attempted": True, "connected_after_cleanup": False,
            }}

        first, second, client = StreamProcess(respond), StreamProcess(), self.client()
        now, pauses = [0], []

        async def pause(delay, interrupt):
            self.assertTrue(first.waited)
            pauses.append(delay)
            now[0] += delay

        launch = AsyncMock(side_effect=[first, second])
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch), patch.object(
                bridge, "time", SimpleNamespace(monotonic=lambda: now[0])), patch.object(
                bridge, "_read_pause", pause):
            await client.read(self.config)
            await client.async_close()
        self.assertEqual(pauses, [5])
        self.assertEqual(launch.await_count, 2)

    async def test_resident_internal_connect_budget_never_causes_outer_retry(self):
        for attempts in (0, 1, 2, None, True, "2"):
            def respond(request, response):
                if request["operation"] != "read":
                    return response
                return {"id": request["id"], "report": {
                    "address": self.config.address, "model": self.config.model, "status": "error",
                    "phase": "ble_connect", "error_class": "BleakError", "error_code": "probe_failed",
                    "relay_writes": 0, "maybe_executed": False, "connection_attempted": True,
                    "disconnect_attempted": True, "connected_after_cleanup": False,
                    "connection_attempts": attempts, "phase_timings_seconds": {"ble_connect": 2.5},
                }}

            with self.subTest(attempts=attempts):
                process, client = StreamProcess(respond), self.client()
                launch = AsyncMock(return_value=process)
                with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
                    with self.assertRaises(bridge.BridgeError) as caught:
                        await client.read(self.config)
                self.assertEqual(launch.await_count, 1)
                self.assertTrue(process.waited)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.phase, "ble_connect")
                self.assertEqual(caught.exception.protocol_error_code, "probe_failed")
                self.assertEqual(caught.exception.phase_timings_seconds, {"ble_connect": 2.5})

    async def test_new_background_read_retry_yields_to_manual_without_second_read_worker(self):
        def respond(request, response):
            if request["operation"] != "read":
                return response
            return {"id": request["id"], "report": {
                "address": self.config.address, "model": self.config.model, "status": "error",
                "phase": "ble_connect", "error_class": "BleakError", "error_code": "probe_failed",
                "relay_writes": 0, "maybe_executed": False, "connection_attempted": True,
                "disconnect_attempted": True, "connected_after_cleanup": False,
                "connection_attempts": 1, "connect_retry_managed": False,
            }}

        first, manual, client = StreamProcess(respond), StreamProcess(), self.client()
        cleanup_finished, interrupt = asyncio.Event(), asyncio.Event()
        original_wait = first.wait

        async def wait():
            result = await original_wait()
            cleanup_finished.set()
            return result

        first.wait = wait
        launch = AsyncMock(side_effect=[first, manual])
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            reading = asyncio.create_task(client.read(self.config, interrupt=interrupt))
            await cleanup_finished.wait()
            interrupt.set()
            with self.assertRaises(bridge.ReadDeferred):
                await reading
            self.assertEqual(launch.await_count, 1)
            await client.relay(self.config, True)
            await client.async_close()
        self.assertTrue(first.waited)
        self.assertEqual([item["operation"] for item in first.inputs], ["init", "read"])
        self.assertEqual([item["operation"] for item in manual.inputs], ["init", "relay", "close"])

    async def test_resident_error_diagnostics_discard_secret_values(self):
        secret = self.config.gatt_ltmk

        def respond(request, response):
            if request["operation"] != "relay":
                return response
            return {"id": request["id"], "report": {
                "address": self.config.address, "model": self.config.model, "status": "error",
                "phase": secret, "error_code": secret, "error_class": secret, "error_message": secret,
                "relay_write_attempts": 0, "relay_writes": 0, "maybe_executed": False,
                "connection_attempts": secret, "phase_timings_seconds": {secret: 1, "ble_connect": secret},
            }}

        process, client = StreamProcess(respond), self.client()
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(bridge.BridgeError) as caught:
                await client.relay(self.config, True)
        self.assertFalse(caught.exception.may_have_written)
        self.assertEqual(caught.exception.phase, "unknown")
        self.assertEqual(caught.exception.protocol_error_code, "unknown")
        self.assertIsNone(caught.exception.connection_attempts)
        self.assertEqual(caught.exception.phase_timings_seconds, {})
        self.assertNotIn(secret, repr(vars(caught.exception)))

    async def test_setting_closes_resident_and_observes_disconnect_cooldown_first(self):
        resident, legacy = StreamProcess(), OneShotProcess()
        client = bridge.BridgeClient(session_idle_timeout=90, cooldown=0.02)
        launched_at = []

        async def launch(*args, **kwargs):
            launched_at.append(time.monotonic())
            if len(launched_at) == 1:
                return resident
            self.assertTrue(resident.waited)
            self.assertEqual(resident.returncode, 0)
            return legacy

        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            await client.read(self.config)
            await client.setting(self.config, "indicator_mode", "always_on")
        self.assertGreaterEqual(launched_at[1] - resident.closed_at, 0.019)
        self.assertEqual([item["operation"] for item in resident.inputs], ["init", "read", "close"])
        self.assertEqual(legacy.inputs[0]["operation"], "setting")
        self.assertFalse(client._session.started)

    async def test_exact_config_key_change_closes_before_new_initialization(self):
        first, second, client = StreamProcess(), StreamProcess(), self.client()
        config = models.DeviceConfig(self.config.address, self.config.model, "cd" * 32)

        async def launch(*args, **kwargs):
            if not first.inputs:
                return first
            self.assertTrue(first.waited)
            return second

        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            await client.read(self.config)
            await client.read(config)
            self.assertEqual(first.inputs[-1]["operation"], "close")
            self.assertEqual(second.inputs[0]["gatt_ltmk"], config.gatt_ltmk)
            await client.async_close()

    async def test_reset_releases_session_but_allows_next_request(self):
        first, second, client = StreamProcess(), StreamProcess(), self.client()
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[first, second])):
            await client.read(self.config)
            await client.async_reset_session()
            self.assertTrue(first.waited)
            self.assertFalse(client._session.started)
            await client.read(self.config)
            await client.async_close()
        with self.assertRaisesRegex(bridge.BridgeError, "entry_closing"):
            await client.relay(self.config, True)

    async def test_malformed_success_invalidates_worker_without_write_retry(self):
        alterations = [
            lambda response: {**response, "id": 999},
            lambda response: {**response, "id": True},
            lambda response: {**response, "extra": "bad"},
            lambda response: {**response, "report": {**response["report"], "address": "02:00:00:00:00:02"}},
            lambda response: {**response, "report": {**response["report"], "session_healthy": False}},
            lambda response: {**response, "report": {**response["report"], "connected_after_cleanup": False}},
            lambda response: {**response, "report": {**response["report"], "relay_writes": True}},
            lambda response: {**response, "report": {**response["report"], "observed_relay_state": False}},
            lambda response: b'{"id":1,"id":1,"report":{}}\n',
            lambda response: b'[]\n',
            lambda response: b'not-json\n',
            lambda response: b'',
            lambda response: b'x' * (session._MAX_REPORT_BYTES + 1) + b'\n',
        ]
        for alter in alterations:
            with self.subTest(alter=alter):
                process = StreamProcess(lambda request, response: alter(response) if request["operation"] == "relay" else response)
                launch = AsyncMock(return_value=process)
                client = self.client()
                with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
                    with self.assertRaises(bridge.BridgeError) as caught:
                        await client.relay(self.config, True)
                self.assertEqual(caught.exception.code, "invalid_worker_report")
                self.assertTrue(caught.exception.may_have_written)
                self.assertTrue(process.terminated)
                self.assertTrue(process.waited)
                self.assertFalse(client._session.started)
                self.assertEqual(launch.await_count, 1)

    async def test_bad_init_is_known_not_to_have_written(self):
        process = StreamProcess(lambda request, response: b'private-stderr-or-key\n')
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(bridge.BridgeError) as caught:
                await self.client().relay(self.config, True)
        self.assertFalse(caught.exception.may_have_written)
        self.assertTrue(process.waited)
        self.assertEqual(len(process.inputs), 1)
        self.assertNotIn("private", str(caught.exception))

    async def test_error_report_write_certainty_and_no_automatic_resend(self):
        for writes in (0, 1):
            def respond(request, response):
                if request["operation"] != "relay":
                    return response
                return {"id": request["id"], "report": {
                    "address": self.config.address, "model": self.config.model, "status": "error",
                    "phase": "protocol_exchange", "error_class": "ProbeError",
                    "relay_write_attempts": writes, "relay_writes": writes,
                    "maybe_executed": bool(writes), "connected_after_cleanup": False,
                }}

            process = StreamProcess(respond)
            client = bridge.BridgeClient(session_idle_timeout=90, cooldown=0.01)
            launch = AsyncMock(return_value=process)
            with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
                with self.assertRaises(bridge.BridgeError) as caught:
                    await client.relay(self.config, True)
            self.assertEqual(caught.exception.may_have_written, bool(writes))
            self.assertFalse(caught.exception.retryable)
            self.assertTrue(process.waited)
            self.assertEqual(launch.await_count, 1)
            self.assertGreater(bridge._NEXT_REQUEST_AT, time.monotonic())

    async def test_relay_timeout_including_stdin_drain_reaps_and_does_not_retry(self):
        for block_drain in (False, True):
            process = StreamProcess(lambda request, response: None if request["operation"] == "relay" else response)
            process.block_drain = block_drain
            launch = AsyncMock(return_value=process)
            with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
                with self.assertRaises(bridge.BridgeError) as caught:
                    await self.client(timeout=0.05).relay(self.config, True)
            self.assertEqual(caught.exception.code, "worker_timeout")
            self.assertTrue(caught.exception.may_have_written)
            self.assertTrue(process.terminated)
            self.assertTrue(process.waited)
            self.assertEqual(launch.await_count, 1)

    async def test_close_cancels_active_and_queued_then_reaps(self):
        process = StreamProcess(lambda request, response: None if request["operation"] == "read" else response)
        client = self.client()
        launch = AsyncMock(return_value=process)
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            first = asyncio.create_task(client.read(self.config))
            await process.started.wait()
            second = asyncio.create_task(client.relay(self.config, True))
            await asyncio.sleep(0)
            await client.async_close()
            results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertTrue(process.waited)
        self.assertEqual(client._inflight, set())
        self.assertEqual(launch.await_count, 1)
        self.assertEqual([item["operation"] for item in process.inputs], ["init", "read"])

    async def test_repeated_cancellation_does_not_escape_cleanup(self):
        process = StreamProcess(lambda request, response: None if request["operation"] == "relay" else response)
        cleanup_started, release = asyncio.Event(), asyncio.Event()
        original_wait = process.wait

        async def wait():
            cleanup_started.set()
            await release.wait()
            return await original_wait()

        process.wait = wait
        client = self.client()
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(client.relay(self.config, True))
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
        self.assertFalse(client._session.started)

    async def test_cancellation_during_spawn_reaps_eventual_worker(self):
        process, client = StreamProcess(), self.client()
        started, release = asyncio.Event(), asyncio.Event()

        async def launch(*args, **kwargs):
            started.set()
            await release.wait()
            return process

        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            task = asyncio.create_task(client.read(self.config))
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

    async def test_cleanup_failure_prevents_setting_worker_start(self):
        process = StreamProcess(lambda request, response: {"id": request["id"], "report": {}}
                                if request["operation"] == "close" else response)
        launch = AsyncMock(return_value=process)
        client = self.client()
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            await client.read(self.config)
            with self.assertRaisesRegex(bridge.BridgeError, "cleanup_failed"):
                await client.setting(self.config, "indicator_mode", "always_on")
        self.assertEqual(launch.await_count, 1)
        self.assertTrue(process.waited)
        self.assertFalse(client._session.started)

    async def test_transport_errors_do_not_expose_credentials(self):
        launch = AsyncMock(side_effect=OSError(self.config.gatt_ltmk))
        with patch.object(bridge.asyncio, "create_subprocess_exec", launch):
            with self.assertRaises(bridge.BridgeError) as caught:
                await self.client().relay(self.config, True)
        self.assertEqual(str(caught.exception), "worker_unavailable")
        self.assertFalse(caught.exception.may_have_written)

    def test_timeout_configuration_is_bounded_and_off_by_default(self):
        self.assertIsNone(bridge.BridgeClient()._session)
        for value in (-1, 121, True, 2.5, "90"):
            with self.assertRaises(ValueError):
                bridge.BridgeClient(session_idle_timeout=value)


if __name__ == "__main__":
    unittest.main()

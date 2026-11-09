"""Offline cold-connect retries and phase timing, without BLE access."""

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

HERE = Path(__file__).resolve().parents[2] / "custom_components/yutai_ble/protocol"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import read_probe as read
import session_worker as worker
import bridge_worker
from test_settings_control import UPSTREAM

TARGET = read.validated_input({"address": "AA:BB:CC:DD:EE:01", "model": "yutai.plug.fsov8m",
                               "gatt_ltmk": "ab" * 32})
BleakDBusError = type("BleakDBusError", (Exception,), {})


class FakeEnvironment:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.clients, self.raws, self.buses = [], [], []
        self.connect_started = asyncio.Event()
        self.bus_started = asyncio.Event()
        self.disconnect_started = asyncio.Event()
        self.connect_wait = self.bus_wait = None
        self.disconnect_wait = None
        self.cleanup_fails = False
        self.other_connected = False
        self.operation_error = None
        self.snapshots = None
        self.dbus_members = []
        env = self

        class Raw:
            def __init__(self, device, **kwargs):
                self.device, self.kwargs = device, kwargs
                self.is_connected = False
                self.index = len(env.raws)
                self.closed = 0
                env.raws.append(self)

            async def connect(self):
                self.is_connected = True
                env.connect_started.set()
                if self.index < len(env.outcomes) and env.outcomes[self.index] is not None:
                    raise env.outcomes[self.index]
                if env.connect_wait is not None:
                    await env.connect_wait.wait()

            async def disconnect(self):
                self.closed += 1
                env.disconnect_started.set()
                if env.disconnect_wait is not None:
                    await env.disconnect_wait.wait()
                if env.cleanup_fails:
                    raise TimeoutError("private-cleanup-message")
                self.is_connected = False

        class Client:
            def __init__(self, device, key, raw, result, **kwargs):
                self.c, self.result, self.ltmk = raw, result, key
                self.k = object()
                self.next_counter, self.next_tid = 1, read.READ_TID
                self.operations = []
                env.clients.append(self)

            async def connect(self):
                with read.timed_phase(self.result, "ble_connect", "ble_connect"):
                    self.result["connection_attempted"] = True
                    await self.c.connect()
                self.result.update(authentication_verified=True, f0_verified=True)

            async def close(self):
                self.result["disconnect_attempted"] = True
                await self.c.disconnect()
                self.ltmk, self.k = b"", None

            async def read_switch(self):
                self.operations.append("read")
                self.result["phase"] = "read_property"
                return {"value": False}

            async def operate(self, operation, value):
                self.operations.append(operation)
                self.result["phase"] = "session_operation"
                if env.operation_error:
                    self.result.update(relay_writes=1, relay_write_attempts=1, maybe_executed=True)
                    raise env.operation_error
                return {"operation": operation, "status": "read_session_verified", "relay_writes": 0}

            def reusable(self, budget):
                return self.c.is_connected

        class Bus:
            def __init__(self, **kwargs):
                self.closed = 0
                self.queries = 0
                env.buses.append(self)

            async def connect(self):
                env.bus_started.set()
                if env.bus_wait is not None:
                    await env.bus_wait.wait()
                return self

            async def call(self, message):
                env.dbus_members.append(message["member"])
                if message["member"] != "GetManagedObjects":
                    raise AssertionError("unexpected_dbus_mutation")
                self.queries += 1
                if env.snapshots is not None:
                    snapshot = env.snapshots[min(self.queries - 1, len(env.snapshots) - 1)]
                    return types.SimpleNamespace(message_type="error" if snapshot is None else "return",
                                                 body=[snapshot])
                props = {"Address": TARGET.address, "Connected": env.other_connected,
                         "Adapter": "/org/bluez/hci0", "RSSI": -60}
                return types.SimpleNamespace(message_type="return", body=[{
                    "/org/bluez/hci0/device": {"org.bluez.Device1": props},
                }])

            def disconnect(self):
                self.closed += 1

        self.Client = Client
        self.modules = {
            "bleak": types.SimpleNamespace(BleakClient=Raw),
            "bleak.backends": types.SimpleNamespace(),
            "bleak.backends.device": types.SimpleNamespace(
                BLEDevice=lambda address, name, details: types.SimpleNamespace(address=address, details=details)),
            "dbus_fast": types.SimpleNamespace(Message=lambda **kwargs: kwargs,
                                               MessageType=types.SimpleNamespace(ERROR="error"),
                                               Variant=type("Variant", (), {})),
            "dbus_fast.aio": types.SimpleNamespace(MessageBus=Bus),
        }

    def install(self):
        stack = ExitStack()
        stack.enter_context(patch.dict(sys.modules, self.modules))
        stack.enter_context(patch.object(read, "load_vendor", return_value=object()))
        stack.enter_context(patch.object(read, "CONNECT_COOLDOWN_SECONDS", 0))
        stack.enter_context(patch.object(read, "CACHE_POLL_SECONDS", 0.001))
        stack.enter_context(patch.object(worker, "RECONNECT_COOLDOWN", 0))
        stack.enter_context(patch.object(worker, "make_session_client", return_value=self.Client))
        return stack

    async def probe(self, operation=None, *, connect_retry=False):
        return await read.run_probe(TARGET, client_factory=lambda _: self.Client, operation=operation,
                                    connect_retry=connect_retry)


def cache_snapshot(*, present=True, discovering=True, adapter="/org/bluez/hci0", connected=False):
    snapshot = {adapter: {"org.bluez.Adapter1": {"Powered": True, "Discovering": discovering}}}
    if present:
        snapshot[adapter + "/device"] = {"org.bluez.Device1": {
            "Address": TARGET.address, "Adapter": adapter, "Connected": connected,
        }}
    return snapshot


class ColdConnectTests(unittest.IsolatedAsyncioTestCase):
    def assert_timings(self, result):
        self.assertEqual(set(result["phase_timings_seconds"]), set(read.PHASE_TIMINGS))
        for value in result["phase_timings_seconds"].values():
            self.assertIs(type(value), float)
            self.assertGreaterEqual(value, 0)
        self.assertNotIn(TARGET.key.hex(), json.dumps(result))
        self.assertNotIn("private-", json.dumps(result))

    async def test_first_success_has_one_attempt_and_cleanup_timings(self):
        env = FakeEnvironment()
        with env.install():
            result = await env.probe()
        self.assertEqual(result["status"], "read_succeeded")
        self.assertEqual(result["connection_attempts"], 1)
        self.assertFalse(result["connected_after_cleanup"])
        self.assertEqual(env.clients[0].operations, ["read"])
        self.assertEqual(env.buses[0].closed, 1)
        self.assert_timings(result)

    async def test_read_connect_retry_is_left_to_bridge_and_next_read_reacquires_cache(self):
        env = FakeEnvironment([TimeoutError("private-first"), None])
        with env.install():
            first = await env.probe()
            self.assertEqual(len(env.raws), 1)
            env.snapshots = [cache_snapshot(present=False), cache_snapshot()]
            second = await env.probe()
        self.assertEqual(first["connection_attempts"], 1)
        self.assertIs(first["connect_retry_managed"], False)
        self.assertEqual(first["status"], "error")
        self.assertFalse(first["connected_after_cleanup"])
        self.assertEqual(first["phase_timings_seconds"]["connect_retry_cooldown"], 0.0)
        self.assertEqual(second["status"], "read_succeeded")
        self.assertEqual(second["connection_attempts"], 1)
        self.assertIs(second["connect_retry_managed"], False)
        self.assertEqual(len(env.raws), 2)
        self.assert_timings(first)
        self.assert_timings(second)

    async def test_retained_read_failure_does_not_internally_retry(self):
        env = FakeEnvironment([TimeoutError("private-read"), None])
        manager = worker.SessionManager(TARGET, 90)
        with env.install():
            report = await manager.execute("read")
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["connection_attempts"], 1)
        self.assertIs(report["connect_retry_managed"], False)
        self.assertFalse(report["connected_after_cleanup"])
        self.assertIsNone(manager.client)
        self.assertEqual(len(env.raws), 1)
        self.assert_timings(report)

    async def test_bridge_dispatch_enables_internal_retry_only_for_manual_operations(self):
        for setting in (None, True, False, ("indicator_mode", "follow")):
            report = {"address": TARGET.address, "model": TARGET.model, "status": "error", "relay_writes": 0}
            with patch.object(read, "run_probe", AsyncMock(return_value=report)) as probe:
                await bridge_worker.run_request(TARGET, setting)
            self.assertIs(probe.call_args.kwargs["connect_retry"], setting is not None)

    async def test_transient_connect_failure_retries_once_with_fresh_owned_client(self):
        env = FakeEnvironment([BleakDBusError("private-connect-message"), None])
        with env.install():
            result = await env.probe(connect_retry=True)
        self.assertEqual(result["status"], "read_succeeded")
        self.assertEqual(result["connection_attempts"], 2)
        self.assertEqual(len(env.raws), 2)
        self.assertIsNot(env.clients[0], env.clients[1])
        self.assertEqual([client.next_counter for client in env.clients], [1, 1])
        self.assertEqual(env.clients[0].ltmk, b"")
        self.assertIsNone(env.clients[0].k)
        self.assertEqual([client.operations for client in env.clients], [[], ["read"]])
        self.assertEqual([raw.closed for raw in env.raws], [1, 1])
        self.assertEqual(env.buses[0].queries, 3)
        self.assert_timings(result)

    async def test_second_failure_never_creates_third_transport(self):
        env = FakeEnvironment([TimeoutError("private-first"), TimeoutError("private-second")])
        with env.install():
            result = await env.probe(connect_retry=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["phase"], "ble_connect")
        self.assertEqual(result["connection_attempts"], 2)
        self.assertEqual(len(env.raws), 2)
        self.assertFalse(result["connected_after_cleanup"])
        self.assert_timings(result)

    async def test_validation_or_unknown_failure_does_not_retry(self):
        for error in (read.ProbeError("required_characteristic_missing"), ValueError("private-invalid")):
            env = FakeEnvironment([error])
            with env.install():
                result = await env.probe(connect_retry=True)
            self.assertEqual(result["connection_attempts"], 1)
            self.assertEqual(len(env.raws), 1)
            self.assertEqual(result["status"], "error")
            self.assert_timings(result)

    async def test_auth_phase_failure_never_retries_even_timeout(self):
        env = FakeEnvironment()
        async def fail_auth(client):
            client.result.update(phase="authentication_proof", connection_attempted=True)
            client.c.is_connected = True
            raise TimeoutError("private-auth")
        with env.install(), patch.object(env.Client, "connect", fail_auth):
            result = await env.probe(connect_retry=True)
        self.assertEqual(result["connection_attempts"], 1)
        self.assertEqual(result["phase"], "authentication_proof")
        self.assertEqual(len(env.raws), 1)
        self.assert_timings(result)

    async def test_cleanup_failure_prevents_second_connect(self):
        env = FakeEnvironment([TimeoutError("private-connect")])
        env.cleanup_fails = True
        with env.install():
            result = await env.probe(connect_retry=True)
        self.assertEqual(result["connection_attempts"], 1)
        self.assertEqual(result["error_code"], "connect_cleanup_failed")
        self.assertEqual(result["phase"], "ble_connect")
        self.assertTrue(result["connected_after_cleanup"])
        self.assertEqual(len(env.raws), 1)
        self.assert_timings(result)

    async def test_foreign_connection_after_cleanup_is_never_adopted(self):
        env = FakeEnvironment([TimeoutError("private-connect")])
        original = env.Client.close
        async def close_then_foreign(client):
            await original(client)
            env.other_connected = True
        with env.install(), patch.object(env.Client, "close", close_then_foreign):
            result = await env.probe(connect_retry=True)
        self.assertEqual(result["connection_attempts"], 1)
        self.assertEqual(result["error_code"], "target_already_connected")
        self.assertEqual(len(env.raws), 1)
        self.assert_timings(result)

    async def test_business_set_failure_is_not_replayed(self):
        env = FakeEnvironment()
        calls = []
        async def operation(client, result):
            calls.append(client)
            result.update(phase="relay_set", relay_writes=1, relay_write_attempts=1, maybe_executed=True)
            raise TimeoutError("private-set")
        with env.install():
            result = await env.probe(operation, connect_retry=True)
        self.assertEqual(result["connection_attempts"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(env.raws), 1)
        self.assertTrue(result["maybe_executed"])
        self.assertEqual(result["phase"], "relay_set")
        self.assert_timings(result)

    async def test_connect_cancellation_owns_client_for_final_cleanup(self):
        env = FakeEnvironment()
        env.connect_wait = asyncio.Event()
        with env.install():
            task = asyncio.create_task(env.probe())
            await env.connect_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(env.raws), 1)
        self.assertEqual(env.raws[0].closed, 1)
        self.assertFalse(env.raws[0].is_connected)
        self.assertEqual(env.buses[0].closed, 1)

    async def test_bus_cancellation_owns_bus_before_connect_await(self):
        env = FakeEnvironment()
        env.bus_wait = asyncio.Event()
        with env.install():
            task = asyncio.create_task(env.probe())
            await env.bus_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(env.raws, [])
        self.assertEqual(env.buses[0].closed, 1)

    async def test_repeated_cancellation_during_cleanup_still_disconnects_bus(self):
        env = FakeEnvironment()
        env.disconnect_wait = asyncio.Event()
        loop = asyncio.get_running_loop()
        handler = loop.get_exception_handler()
        with env.install():
            task = asyncio.create_task(env.probe())
            await env.disconnect_started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            env.disconnect_wait.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(env.raws[0].is_connected)
        self.assertEqual(env.buses[0].closed, 1)
        self.assertIs(loop.get_exception_handler(), handler)

    async def test_retained_cleanup_cancellation_cannot_skip_bus_disconnect(self):
        env = FakeEnvironment()
        env.disconnect_wait = asyncio.Event()
        manager = worker.SessionManager(TARGET, 90)
        with env.install():
            await manager.execute("read")
            task = asyncio.create_task(manager.close())
            await env.disconnect_started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            env.disconnect_wait.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(env.raws[0].is_connected)
        self.assertEqual(env.buses[0].closed, 1)
        self.assertIsNone(manager.bus)

    async def test_shared_work_deadline_does_not_restart_for_retry(self):
        env = FakeEnvironment([TimeoutError("private-first"), None])
        env.connect_wait = asyncio.Event()
        with env.install(), patch.object(read, "WORK_SECONDS", 0.1):
            result = await env.probe(connect_retry=True)
        self.assertEqual(result["connection_attempts"], 2)
        self.assertEqual(result["error_code"], "phase_timeout")
        self.assertEqual(result["phase"], "ble_connect")
        self.assertEqual([raw.closed for raw in env.raws], [1, 1])
        self.assertFalse(result["connected_after_cleanup"])
        self.assert_timings(result)

    async def test_deadline_during_retry_cooldown_never_starts_second_connect(self):
        env = FakeEnvironment([TimeoutError("private-first")])
        with env.install(), patch.object(read, "WORK_SECONDS", 0.02), \
                patch.object(read, "CONNECT_COOLDOWN_SECONDS", 30):
            result = await env.probe(connect_retry=True)
        self.assertEqual(result["connection_attempts"], 1)
        self.assertEqual(result["phase"], "connect_retry_cooldown")
        self.assertEqual(len(env.raws), 1)
        self.assertFalse(env.raws[0].is_connected)
        self.assertEqual(env.buses[0].closed, 1)
        self.assert_timings(result)

    async def test_retained_startup_uses_same_retry_and_reuse_reports_zero_attempts(self):
        env = FakeEnvironment([TimeoutError("private-first"), None])
        manager = worker.SessionManager(TARGET, 90)
        with env.install():
            first = await manager.execute("relay", False)
            second = await manager.execute("read")
            cleanup = await manager.close()
        self.assertEqual(first["connection_attempts"], 2)
        self.assertIs(first["connect_retry_managed"], True)
        self.assertFalse(first["connection_reused"])
        self.assertEqual(second["connection_attempts"], 0)
        self.assertIs(second["connect_retry_managed"], False)
        self.assertTrue(second["connection_reused"])
        self.assertNotIn("connected_after_cleanup", first)
        self.assertFalse(cleanup["connected_after_cleanup"])
        self.assertEqual([raw.closed for raw in env.raws], [1, 1])
        self.assert_timings(first)
        self.assert_timings(second)

    async def test_retained_set_failure_not_replayed_and_timings_preserved(self):
        env = FakeEnvironment()
        env.operation_error = TimeoutError("private-write")
        manager = worker.SessionManager(TARGET, 90)
        with env.install():
            result = await manager.execute("relay", True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["connection_attempts"], 1)
        self.assertEqual(result["relay_write_attempts"], 1)
        self.assertTrue(result["maybe_executed"])
        self.assertFalse(result["connected_after_cleanup"])
        self.assertIsNone(manager.client)
        self.assertEqual(len(env.raws), 1)
        self.assert_timings(result)

    async def test_initial_missing_target_reappears_via_existing_discovery(self):
        env = FakeEnvironment()
        env.snapshots = [cache_snapshot(present=False), cache_snapshot()]
        with env.install():
            result = await env.probe()
        self.assertEqual(result["status"], "read_succeeded")
        self.assertEqual(result["connection_attempts"], 1)
        self.assertEqual(env.buses[0].queries, 3)
        self.assertEqual(set(env.dbus_members), {"GetManagedObjects"})
        self.assert_timings(result)

    async def test_retry_missing_target_reacquired_without_starting_scan(self):
        env = FakeEnvironment([TimeoutError("private-connect"), None])
        env.snapshots = [cache_snapshot(), cache_snapshot(), cache_snapshot(present=False), cache_snapshot()]
        with env.install():
            result = await env.probe(connect_retry=True)
        self.assertEqual(result["status"], "read_succeeded")
        self.assertEqual(result["connection_attempts"], 2)
        self.assertEqual(env.buses[0].queries, 4)
        self.assertEqual([raw.closed for raw in env.raws], [1, 1])
        self.assertEqual([raw.device.details["props"]["Adapter"] for raw in env.raws],
                         ["/org/bluez/hci0", "/org/bluez/hci0"])
        self.assertEqual(set(env.dbus_members), {"GetManagedObjects"})
        self.assert_timings(result)

    async def test_missing_target_without_existing_discovery_never_starts_scan(self):
        for retry in (False, True):
            env = FakeEnvironment([TimeoutError("private-connect")] if retry else [])
            env.snapshots = ([cache_snapshot(), cache_snapshot()] if retry else []) + [
                cache_snapshot(present=False, discovering=False)]
            with env.install():
                result = await env.probe(connect_retry=retry)
            self.assertEqual(result["error_code"], "target_absent_no_existing_discovery")
            self.assertEqual(result["connection_attempts"], int(retry))
            self.assertEqual(set(env.dbus_members), {"GetManagedObjects"})
            self.assertEqual(len(env.raws), int(retry))
            self.assert_timings(result)

    async def test_retry_wait_rejects_adapter_change_and_foreign_connection(self):
        cases = (
            ([cache_snapshot(present=False, adapter="/org/bluez/hci1")], "target_adapter_changed"),
            ([cache_snapshot(adapter="/org/bluez/hci1")], "target_adapter_changed"),
            ([cache_snapshot(present=False), cache_snapshot(connected=True)], "target_already_connected"),
            ([cache_snapshot(present=False), cache_snapshot(present=False, discovering=False)],
             "existing_discovery_stopped_or_changed"),
            ([cache_snapshot(present=False), cache_snapshot(adapter="/org/bluez/hci1")],
             "existing_discovery_stopped_or_changed"),
        )
        for tail, expected in cases:
            with self.subTest(expected=expected):
                env = FakeEnvironment([TimeoutError("private-connect")])
                env.snapshots = [cache_snapshot(), cache_snapshot(), *tail]
                with env.install():
                    result = await env.probe(connect_retry=True)
                self.assertEqual(result["error_code"], expected)
                self.assertEqual(result["connection_attempts"], 1)
                self.assertEqual(len(env.raws), 1)
                self.assertFalse(result["connected_after_cleanup"])
                self.assertEqual(set(env.dbus_members), {"GetManagedObjects"})

    async def test_retry_cache_query_error_does_not_connect_or_mutate_discovery(self):
        env = FakeEnvironment([TimeoutError("private-connect")])
        env.snapshots = [cache_snapshot(), cache_snapshot(), cache_snapshot(present=False), None]
        with env.install():
            result = await env.probe(connect_retry=True)
        self.assertEqual(result["error_code"], "bluez_query_failed")
        self.assertEqual(result["connection_attempts"], 1)
        self.assertEqual(result["phase"], "bluez_cache")
        self.assertFalse(result["connected_after_cleanup"])
        self.assertEqual(set(env.dbus_members), {"GetManagedObjects"})
        self.assert_timings(result)

    async def test_retry_cache_wait_is_bounded_by_own_and_shared_deadlines(self):
        for shared in (False, True):
            env = FakeEnvironment([TimeoutError("private-connect")])
            env.snapshots = [cache_snapshot(), cache_snapshot(), cache_snapshot(present=False)]
            with env.install(), patch.object(read, "RETRY_CACHE_WAIT_SECONDS", 10 if shared else 0.02), \
                    patch.object(read, "WORK_SECONDS", 0.03 if shared else 62):
                result = await env.probe(connect_retry=True)
            self.assertEqual(result["error_code"], "phase_timeout" if shared else "target_not_seen")
            self.assertEqual(result["phase"], "bluez_cache")
            self.assertEqual(result["connection_attempts"], 1)
            self.assertEqual(len(env.raws), 1)
            self.assertFalse(result["connected_after_cleanup"])
            self.assertEqual(env.buses[0].closed, 1)
            self.assert_timings(result)

    async def test_cancel_during_retry_cache_wait_cleans_bus_without_second_connect(self):
        env = FakeEnvironment([TimeoutError("private-connect")])
        env.snapshots = [cache_snapshot(), cache_snapshot(), cache_snapshot(present=False)]
        with env.install():
            task = asyncio.create_task(env.probe(connect_retry=True))
            while not env.buses or env.buses[0].queries < 3:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(env.raws), 1)
        self.assertFalse(env.raws[0].is_connected)
        self.assertEqual(env.raws[0].closed, 1)
        self.assertEqual(env.buses[0].closed, 1)
        self.assertEqual(set(env.dbus_members), {"GetManagedObjects"})


class HandshakeTimingTests(unittest.IsolatedAsyncioTestCase):
    async def handshake(self, fail_phase=None):
        result = {"connection_attempted": False}
        read.reset_connection_telemetry(result)
        async def step(*args, **kwargs):
            if result["phase"] == fail_phase:
                raise TimeoutError("private-handshake")
        class Channel:
            def __init__(self, *args, **kwargs):
                self._task, self.rxlog, self.dmtu = None, [], 242
            start = _send = staticmethod(step)
        private = types.SimpleNamespace(
            public_key=lambda: types.SimpleNamespace(public_bytes=lambda *args: b"x" * 65),
            exchange=lambda *args: b"x" * 32)
        upstream = types.SimpleNamespace(**vars(UPSTREAM), Channel=Channel,
            ec=types.SimpleNamespace(generate_private_key=lambda *args: private,
                SECP256R1=lambda: None, ECDH=lambda: None,
                EllipticCurvePublicKey=types.SimpleNamespace(from_encoded_point=lambda *args: None)),
            serialization=types.SimpleNamespace(Encoding=types.SimpleNamespace(X962=None),
                PublicFormat=types.SimpleNamespace(UncompressedPoint=None)),
            hkdf=lambda *args: b"x" * 64, SALT=b"", INFO=b"",
            AESCCM=lambda *args, **kwargs: types.SimpleNamespace(encrypt=lambda *args: b"x" * 8))
        raw = types.SimpleNamespace(services=types.SimpleNamespace(get_characteristic=lambda uuid: object()))
        client = read.make_client_class(upstream)(object(), b"x" * 32, raw, result)
        client.c = types.SimpleNamespace(raw=raw, connect=step, start_notify=step, write_gatt_char=step)
        client.capture_transport_metadata = lambda: None
        async def acquire():
            await step()
            client.handshake["effective_auth_write_capacity"] = 244
        async def auth_single(package, payload):
            await step()
            if package == 5:
                client.authenticated = True
                client.auth_event.set()
        async def device_key(**kwargs):
            await step()
            return b"x" * 64
        async def exchange(*args):
            await step()
            client.f0_verified = True
            return {"verified": True}
        client.acquire_transport_mtu = acquire
        client.wait_handshake_event = step
        client.send_auth_single = auth_single
        client.wait_device_public_key = device_key
        client.exchange = exchange
        sleeps = []
        async def record_sleep(delay):
            sleeps.append(delay)
        with patch.object(read.asyncio, "sleep", record_sleep):
            if fail_phase:
                with self.assertRaises(TimeoutError):
                    await client.connect()
            else:
                await client.connect()
        return result, sleeps

    async def test_all_handshake_phases_timed_without_changing_fixed_waits(self):
        result, sleeps = await self.handshake()
        self.assertEqual(sleeps, [0.25, 0.25, 0.25, 0.1, 0.2, 0.25])
        self.assertEqual(result["phase"], "protocol_exchange")
        self.assertTrue(result["authentication_verified"])
        self.assertTrue(result["f0_verified"])
        self.assertEqual(set(result["phase_timings_seconds"]), set(read.PHASE_TIMINGS))

    async def test_failures_keep_exact_phase_and_numeric_timings(self):
        for phase in read.PHASE_TIMINGS[2:10]:
            with self.subTest(phase=phase):
                result, _sleeps = await self.handshake(phase)
                self.assertEqual(result["phase"], phase)
                self.assertTrue(all(type(value) is float and value >= 0
                                    for value in result["phase_timings_seconds"].values()))
                self.assertNotIn("private-handshake", json.dumps(result))


if __name__ == "__main__":
    unittest.main()

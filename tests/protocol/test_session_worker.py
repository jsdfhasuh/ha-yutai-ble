"""NDJSON and session lifecycle tests, with no BLE or credentials."""

import asyncio
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[2] / "custom_components/yutai_ble/protocol"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import read_probe as read
import session_worker as worker

INIT = {"id": 0, "operation": "init", "address": "AA:BB:CC:DD:EE:01",
        "model": "yutai.plug.fsov8m", "gatt_ltmk": "ab" * 32, "idle_timeout": 90}


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.c = types.SimpleNamespace(is_connected=True)
        self.allow_reuse = True
        self.delay = 90
        self.closed = 0
        self.operations = []
        self.error = None
        self.hang = None
        self.cleanup_fail = False
        self.budgets = []

    def reusable(self, budget):
        self.budgets.append(budget)
        return self.allow_reuse

    def remaining_idle(self):
        return self.delay

    async def operate(self, operation, value):
        self.operations.append((operation, value))
        if self.hang:
            await self.hang.wait()
        if self.error:
            self.result.update(relay_writes=1, relay_write_attempts=1, maybe_executed=True)
            raise self.error
        return {"status": "read_session_verified", "operation": operation,
                "relay_writes": 0, "relay_write_attempts": 0, "maybe_executed": False}

    async def close(self):
        self.closed += 1
        if self.cleanup_fail:
            raise TimeoutError("secret-should-not-appear")
        self.c.is_connected = False


class FakeManager(worker.SessionManager):
    def __init__(self, target, idle_timeout):
        super().__init__(target, idle_timeout)
        self.clients = []
        self.next_error = None

    async def _open(self):
        self.client = FakeClient(self.result)
        self.client.error = self.next_error
        self.clients.append(self.client)


class ValidationTests(unittest.TestCase):
    def test_strict_init(self):
        target, idle = worker.validated_init(INIT)
        self.assertEqual(target.address, INIT["address"])
        self.assertEqual(idle, 90)
        for delta in ({"extra": 1}, {"id": True}, {"id": 1}, {"operation": "read"},
                      {"idle_timeout": 0}, {"idle_timeout": 121}, {"idle_timeout": True},
                      {"model": "yutai.plug.fsov7m"}, {"gatt_ltmk": "bad"}):
            with self.subTest(delta=delta), self.assertRaises(read.ProbeError):
                worker.validated_init({**INIT, **delta})

    def test_strict_requests_and_ids(self):
        self.assertEqual(worker.validated_request({"id": 2, "operation": "relay", "value": True}, 1),
                         (2, "relay", True))
        for data in ({"id": 1, "operation": "read"}, {"id": True, "operation": "read"},
                     {"id": 2, "operation": "relay", "value": 1},
                     {"id": 2, "operation": "read", "address": "AA:BB:CC:DD:EE:02"},
                     {"id": 2, "operation": "setting"}, {"id": 2, "operation": "close", "value": False}):
            with self.assertRaises(read.ProbeError):
                worker.validated_request(data, 1)

    def test_duplicate_keys_overflow_and_credential_output(self):
        with self.assertRaises(read.ProbeError):
            worker.decode_line(b'{"id":0,"id":1}')
        with self.assertRaises(read.ProbeError):
            worker.decode_line(b"x" * 4097)
        with self.assertRaisesRegex(read.ProbeError, "credential_in_result"):
            worker.encoded_reply({"bad": INIT["gatt_ltmk"]}, bytes.fromhex(INIT["gatt_ltmk"]))
        self.assertEqual(worker.safe_error(RuntimeError(INIT["gatt_ltmk"]))["error_code"], "probe_failed")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cooldown = patch.object(worker, "RECONNECT_COOLDOWN", 0)
        cooldown.start()
        self.addCleanup(cooldown.stop)

    def prepare(self):
        target, idle = worker.validated_init(INIT)
        self.manager = FakeManager(target, idle)
        return self.manager

    async def test_cold_reused_honest_report_and_explicit_cleanup(self):
        manager = self.prepare()
        first = await manager.execute("read")
        second = await manager.execute("relay", True)
        self.assertFalse(first["connection_reused"])
        self.assertTrue(second["connection_reused"])
        self.assertTrue(second["session_connected"])
        self.assertTrue(second["transaction_complete"])
        self.assertNotIn("connected_after_cleanup", second)
        self.assertEqual(len(manager.clients), 1)
        report = await manager.close()
        self.assertEqual(report["status"], "close_session_verified")
        self.assertIs(report["connected_after_cleanup"], False)
        self.assertEqual(manager.clients[0].closed, 1)

    async def test_budget_preflight_renews_before_new_request(self):
        manager = self.prepare()
        await manager.execute("read")
        old = manager.client
        old.allow_reuse = False
        result = await manager.execute("relay", False)
        self.assertFalse(result["connection_reused"])
        self.assertEqual(old.closed, 1)
        self.assertEqual(old.operations, [("read", None)])
        self.assertEqual(manager.client.operations, [("relay", False)])
        self.assertEqual(old.budgets, [3])

    async def test_actual_cleanup_sets_cooldown_before_reopening(self):
        manager = self.prepare()
        await manager.execute("read")
        with patch.object(worker, "RECONNECT_COOLDOWN", 0.02):
            await manager.close()
        deadline = manager.next_connect_at
        self.assertGreater(deadline, worker.time.monotonic())
        await manager.execute("read")
        self.assertGreaterEqual(worker.time.monotonic(), deadline)
        self.assertEqual(len(manager.clients), 2)

    async def test_failed_set_uncertainty_preserved_without_replay(self):
        manager = self.prepare()
        manager.next_error = TimeoutError(INIT["gatt_ltmk"])
        report = await manager.execute("relay", True)
        self.assertEqual(report["status"], "error")
        self.assertTrue(report["maybe_executed"])
        self.assertEqual(report["relay_write_attempts"], 1)
        self.assertFalse(report["transaction_complete"])
        self.assertFalse(report["connected_after_cleanup"])
        self.assertEqual(len(manager.clients), 1)
        self.assertEqual(manager.clients[0].operations, [("relay", True)])
        self.assertNotIn(INIT["gatt_ltmk"], json.dumps(report))
        manager.next_error = None
        await manager.execute("read")
        self.assertEqual(len(manager.clients), 2)

    async def test_failed_cleanup_blocks_new_connection(self):
        manager = self.prepare()
        await manager.execute("read")
        manager.client.allow_reuse = False
        manager.client.cleanup_fail = True
        report = await manager.execute("relay", True)
        self.assertEqual(report["status"], "error")
        self.assertTrue(report["connected_after_cleanup"])
        self.assertEqual(len(manager.clients), 1)
        self.assertFalse(report["maybe_executed"])
        self.assertNotIn("secret-should-not-appear", json.dumps(report))
        manager.client.cleanup_fail = False
        await manager.close()

    async def test_execute_cancellation_disconnects(self):
        manager = self.prepare()
        await manager.execute("read")
        client = manager.client
        client.hang = asyncio.Event()
        task = asyncio.create_task(manager.execute("relay", True))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(manager.client)
        self.assertEqual(client.closed, 1)

    async def run_messages(self, messages):
        self.prepare()
        reader = asyncio.StreamReader()
        for message in messages:
            reader.feed_data(json.dumps(message).encode() + b"\n")
        reader.feed_eof()
        output = []
        await worker.serve(reader, output.append, manager_factory=lambda *args: self.manager)
        return [json.loads(line) for line in output]

    async def test_init_does_not_connect_and_eof_cleans_up(self):
        output = await self.run_messages([INIT])
        self.assertEqual(output[0]["status"], "ready")
        self.assertEqual(self.manager.clients, [])
        output = await self.run_messages([INIT, {"id": 1, "operation": "read"}])
        self.assertEqual(len(output), 2)
        self.assertEqual(self.manager.clients[0].closed, 1)
        self.assertNotIn(INIT["gatt_ltmk"], json.dumps(output))

    async def test_request_ids_and_close_reply(self):
        output = await self.run_messages([INIT, {"id": 1, "operation": "read"},
                                          {"id": 2, "operation": "close"}])
        self.assertEqual([item["id"] for item in output], [0, 1, 2])
        self.assertEqual(output[-1]["report"]["status"], "close_session_verified")
        output = await self.run_messages([INIT, {"id": 1, "operation": "read"},
                                          {"id": 1, "operation": "relay", "value": True}])
        self.assertEqual(output[-1]["report"]["error_code"], "session_request_id_invalid")
        self.assertEqual(self.manager.clients[0].operations, [("read", None)])

    async def test_idle_timer_really_disconnects_without_closing_worker(self):
        self.prepare()
        reader = asyncio.StreamReader()
        output = []
        reader.feed_data(json.dumps(INIT).encode() + b"\n")
        reader.feed_data(b'{"id":1,"operation":"read"}\n')
        task = asyncio.create_task(worker.serve(reader, output.append, manager_factory=lambda *args: self.manager))
        while len(output) < 2:
            await asyncio.sleep(0)
        client = self.manager.client
        client.delay = 0.01
        reader.feed_data(b'{"id":2,"operation":"read"}\n')
        await asyncio.sleep(0.05)
        self.assertEqual(client.closed, 1)
        self.assertIsNone(self.manager.client)
        self.assertFalse(task.done())
        reader.feed_data(b'{"id":3,"operation":"read"}\n')
        reader.feed_eof()
        await task
        self.assertEqual(len(self.manager.clients), 2)
        self.assertEqual(self.manager.clients[1].closed, 1)

    async def test_worker_cancellation_like_sigterm_cleans_up(self):
        self.prepare()
        reader = asyncio.StreamReader()
        output = []
        reader.feed_data(json.dumps(INIT).encode() + b"\n")
        reader.feed_data(b'{"id":1,"operation":"read"}\n')
        task = asyncio.create_task(worker.serve(reader, output.append, manager_factory=lambda *args: self.manager))
        while len(output) < 2:
            await asyncio.sleep(0)
        client = self.manager.client
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client.closed, 1)
        self.assertFalse(client.c.is_connected)

    async def test_stdio_sigterm_handler_cancels_and_disconnects(self):
        self.prepare()
        original_serve = worker.serve
        handlers, removed, output = {}, [], []
        transport = types.SimpleNamespace(close=lambda: removed.append("pipe"))
        async def connect_read_pipe(factory, pipe):
            protocol = factory()
            protocol.data_received(json.dumps(INIT).encode() + b"\n")
            protocol.data_received(b'{"id":1,"operation":"read"}\n')
            return transport, protocol
        fake_loop = types.SimpleNamespace(
            add_signal_handler=lambda sig, callback: handlers.update({sig: callback}),
            remove_signal_handler=lambda sig: removed.append(sig),
            set_exception_handler=lambda handler: None,
            connect_read_pipe=connect_read_pipe,
        )
        async def managed_serve(reader, emit):
            return await original_serve(reader, emit, manager_factory=lambda *args: self.manager)
        def capture(line, **kwargs):
            output.append(json.loads(line))
            if len(output) == 2:
                handlers[worker.signal.SIGTERM]()
        with patch.object(worker.asyncio, "get_running_loop", return_value=fake_loop), \
                patch.object(worker, "serve", managed_serve), \
                patch("builtins.print", capture):
            await worker.run_stdio()
        self.assertEqual(self.manager.clients[0].closed, 1)
        self.assertFalse(self.manager.clients[0].c.is_connected)
        self.assertIn(worker.signal.SIGTERM, removed)
        self.assertIn("pipe", removed)


if __name__ == "__main__":
    unittest.main()

"""Offline cycle tests; all protocol exchanges use an in-process fake."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest

import cycle_probe as cycle
import read_probe as read
from test_read_probe import FakeRaw, UPSTREAM, pack


class ParseTests(unittest.TestCase):
    def test_bool_encoding_and_strict_set_response(self):
        self.assertEqual(cycle.bool_set_packet(pack, 9, True), pack(9, 0, b"\1\2\1\0\1\0\1"))
        self.assertEqual(cycle.bool_set_packet(pack, 9, False)[-3:], b"\1\0\0")
        with self.assertRaises(read.ProbeError):
            cycle.bool_set_packet(pack, 9, 1)
        valid = pack(9, 1, b"\1\2\1\0\0\0")
        self.assertEqual(cycle.parse_set_response(valid, 9)["status"], 0)
        for packet in (valid[:-1], valid + b"x", pack(8, 1, valid[5:]), pack(9, 0, valid[5:]),
                       pack(9, 1, b"\2\2\1\0\0\0"), pack(9, 1, b"\1\3\1\0\0\0"),
                       pack(9, 1, b"\1\2\2\0\0\0"), pack(9, 1, b"\1\2\1\0\1\0")):
            with self.assertRaises(read.ProbeError):
                cycle.parse_set_response(packet, 9)

    def test_both_clis_reject_extra_operation_fields_without_secrets(self):
        secret = "ab" * 32
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": secret, "operation": "cycle"}
        for module in (read, cycle):
            result = subprocess.run([sys.executable, str(Path(module.__file__))], input=json.dumps(data),
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertIn("input_fields_invalid", result.stdout)


class CycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.result = {"connection_attempted": True}
        self.client = cycle.make_cycle_client(UPSTREAM)(object(), b"x" * 32, FakeRaw(), self.result)
        self.client.authenticated = self.client.f0_verified = True
        self.client.sent = [(pack(1, 0xF0), 0)]
        self.state = False
        self.records = []
        self.sets = []
        self.test_error = self.restore_error = None
        self.initial_typelen = 1
        self.block_test = self.block_restore = False
        self.test_started = asyncio.Event()
        self.restore_started = asyncio.Event()
        self.release_restore = asyncio.Event()

        async def fake_exchange(packet, tid, counter):
            self.assertGreater(self.client.next_counter, counter)
            self.assertGreater(self.client.next_tid, tid)
            self.client.authorize_send(packet, counter)
            self.client.sent.append((packet, counter))
            self.records.append((tid, counter, packet[4]))
            if packet[4] == 0:
                self.assertTrue(self.result["maybe_executed"])
                self.state = bool(packet[-1])
                self.sets.append(self.state)
                if len(self.sets) == 1:
                    self.test_started.set()
                    if self.block_test:
                        await asyncio.Event().wait()
                    if self.test_error:
                        raise self.test_error
                else:
                    self.restore_started.set()
                    if self.block_restore:
                        await self.release_restore.wait()
                    if self.restore_error:
                        raise self.restore_error
                response = pack(tid, 1, b"\1\2\1\0\0\0")
            else:
                typelen = self.initial_typelen if len(self.records) == 1 else 1
                response = pack(tid, 3, b"\1\2\1\0\0\0" + typelen.to_bytes(2, "little") + bytes([self.state]))
            return self.client.parse_pending_response(response)
        self.client.exchange = fake_exchange

    def assert_unique_ids(self):
        tids, counters = [r[0] for r in self.records], [r[1] for r in self.records]
        self.assertEqual(len(tids), len(set(tids)))
        self.assertEqual(counters, sorted(set(counters)))

    async def test_complete_cycle_and_two_write_limit(self):
        await self.client.run_cycle()
        self.assertEqual(self.sets, [True, False])
        self.assertFalse(self.state)
        self.assertTrue(self.result["restore_verified"])
        self.assertEqual(self.result["status"], "cycle_verified_restored")
        for role in ("test", "restore", "other"):
            with self.assertRaises(read.ProbeError):
                await self.client.set_cycle_state(role)
        with self.assertRaises(read.ProbeError):
            await self.client.request_reserved("set", True)
        with self.assertRaises(read.ProbeError):
            self.client.action_pkt()
        self.assertEqual(self.result["relay_writes"], 2)
        self.assert_unique_ids()

    async def test_uncertain_test_timeout_restores_without_repeating_test(self):
        self.test_error = TimeoutError("must-not-log")
        await self.client.run_cycle()
        self.assertEqual(self.sets, [True, False])
        self.assertEqual(self.result["status"], "cycle_failed_restored")
        self.assertTrue(self.result["restore_verified"])
        self.assertNotIn("must-not-log", json.dumps(self.result))
        self.assert_unique_ids()

    async def test_restore_response_timeout_still_independently_reads(self):
        self.restore_error = TimeoutError()
        await self.client.run_cycle()
        self.assertTrue(self.result["restore_verified"])
        self.assertEqual(self.records[-1][2], 2)
        self.assertEqual(self.result["status"], "cycle_failed_restored")
        self.assertEqual(self.sets, [True, False])
        self.assert_unique_ids()

    async def test_cancelled_test_still_restores(self):
        self.block_test = True
        task = asyncio.create_task(self.client.run_cycle())
        await self.test_started.wait()
        task.cancel()
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.sets, [True, False])
        self.assertTrue(self.result["restore_verified"])
        self.assertEqual(self.result["cycle_error"]["code"], "cycle_cancelled")
        self.assert_unique_ids()

    async def test_cancellation_during_restore_does_not_cancel_recovery(self):
        self.block_restore = True
        task = asyncio.create_task(self.client.run_cycle())
        await self.restore_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.release_restore.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(self.result["restore_verified"])
        self.assertEqual(self.sets, [True, False])

    async def test_non_bool_initial_value_never_writes(self):
        self.initial_typelen = 0x1001
        await self.client.run_cycle()
        self.assertEqual(self.sets, [])
        self.assertFalse(self.result["maybe_executed"])
        self.assertEqual(self.result["status"], "cycle_failed_no_switch")


if __name__ == "__main__":
    unittest.main()

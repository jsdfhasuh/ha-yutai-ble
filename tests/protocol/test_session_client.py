"""Offline retained-session packet tests; no Bluetooth access."""

import asyncio
from pathlib import Path
import sys
import types
import unittest

HERE = Path(__file__).resolve().parents[2] / "custom_components/yutai_ble/protocol"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import read_probe as read
import session_client as session
from test_settings_control import BASE, UPSTREAM, FakeRaw, get_response, pack
from test_relay_control import set_response

ADDRESS = "AA:BB:CC:DD:EE:01"
MODEL = "yutai.plug.fsov8m"


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, *, initial=False, fail_at=None, observed=None, set_status=0):
        self.words = {**BASE, (2, 1): initial}
        self.result = {"address": ADDRESS, "model": MODEL, "connection_attempted": True}
        self.raw = FakeRaw()
        self.client = session.make_session_client(UPSTREAM)(
            types.SimpleNamespace(address=ADDRESS), b"x" * 32, self.raw, self.result)
        self.raw.owner = self.client
        self.client.authenticated = self.client.f0_verified = True
        self.client.sent = [(pack(1, 0xF0), 0)]
        self.records = []
        self.block_at = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def exchange(packet, tid, counter):
            self.assertGreater(self.client.next_tid, tid)
            self.assertGreater(self.client.next_counter, counter)
            self.client.authorize_send(packet, counter)
            self.client.sent.append((packet, counter))
            self.records.append((tid, counter, packet))
            if len(self.records) == self.block_at:
                self.entered.set()
                await self.release.wait()
            if len(self.records) == fail_at:
                raise TimeoutError("must-not-log")
            if packet[4] == 0:
                self.words[(2, 1)] = bool(packet[-1])
                response = set_response(tid, status=set_status)
            else:
                batch = self.client.expected_responses[tid][1]
                words = self.words.copy()
                if self.client.stage == "verify" and observed is not None:
                    words[(2, 1)] = observed
                response = get_response(tid, batch, words)
            return self.client.parse_pending_response(response)

        self.client.exchange = exchange

    async def test_four_relay_commands_never_reuse_counters_or_tids(self):
        self.prepare()
        for requested in (True, False, True, False):
            report = await self.client.operate("relay", requested)
            self.assertTrue(report["verified"])
            self.assertEqual(report["verification_scope"], "relay_only")
            self.assertEqual(report["relay_writes"], 1)
            self.assertEqual(len(report["properties"]), 1)
            self.assertIsNone(report["settings_unchanged"])
            self.assertIs(report["observed_relay_state"], requested)
        self.assertEqual([item[1] for item in self.records], list(range(1, 13)))
        self.assertEqual([item[0] for item in self.records], list(range(read.READ_TID, read.READ_TID + 12)))
        self.assertEqual(self.client.next_counter, 13)
        self.assertEqual(len(self.client.sent), 13)

    async def test_full_read_is_four_batches_and_no_set(self):
        self.prepare()
        report = await self.client.operate("read")
        self.assertEqual([item[2][4] for item in self.records], [2, 2, 2, 2])
        self.assertEqual(len(report["properties"]), 13)
        self.assertEqual(report["relay_writes"], 0)
        self.assertFalse(report["maybe_executed"])
        self.assertEqual(report["status"], "read_session_verified")
        self.assertIn("indicator_mode", report["settings"])
        self.assertEqual(report["verification_scope"], "full")

    async def test_noop_still_reads_twice_without_set(self):
        self.prepare()
        report = await self.client.operate("relay", False)
        self.assertEqual(report["command_count"], 2)
        self.assertEqual(report["relay_write_attempts"], 0)
        self.assertFalse(report["maybe_executed"])
        self.assertTrue(all(item[2][4] == 2 for item in self.records))

    async def test_each_failure_poisons_without_replay(self):
        for fail_at in (1, 2, 3):
            self.prepare(fail_at=fail_at)
            with self.assertRaises(TimeoutError):
                await self.client.operate("relay", True)
            self.assertTrue(self.client.permanently_failed)
            self.assertEqual(self.result["maybe_executed"], fail_at >= 2)
            with self.assertRaisesRegex(read.ProbeError, "session_unusable"):
                await self.client.operate("relay", False)
            self.assertEqual(len(self.records), fail_at)
            self.assertNotIn("must-not-log", str(self.result))

    async def test_wrong_readback_or_set_ack_is_not_success(self):
        for args in ({"observed": False}, {"set_status": 1}):
            self.prepare(**args)
            with self.assertRaises(read.ProbeError):
                await self.client.operate("relay", True)
            self.assertEqual(self.result["relay_write_attempts"], 1)
            self.assertTrue(self.client.permanently_failed)

    async def test_cancelled_set_consumes_counter_without_replay(self):
        self.prepare()
        self.block_at = 2
        task = asyncio.create_task(self.client.operate("relay", True))
        await self.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client.next_counter, 3)
        self.assertEqual(self.result["relay_write_attempts"], 1)
        self.assertTrue(self.result["maybe_executed"])
        self.assertTrue(self.client.permanently_failed)
        self.assertEqual(self.client.expected_responses, {})

    async def test_concurrent_operation_rejected_without_extra_packets(self):
        self.prepare()
        self.block_at = 1
        task = asyncio.create_task(self.client.operate("relay", True))
        await self.entered.wait()
        with self.assertRaisesRegex(read.ProbeError, "session_operation_in_progress"):
            await self.client.operate("read")
        self.release.set()
        self.assertTrue((await task)["verified"])
        self.assertEqual(len(self.records), 3)

    async def test_callback_auth_or_disconnect_prevent_any_packet(self):
        for attr, value in (("authenticated", False), ("f0_verified", False),
                            ("callback_error", "transport_ack_failed")):
            self.prepare()
            setattr(self.client, attr, value)
            with self.assertRaises(read.ProbeError):
                await self.client.operate("relay", True)
            self.assertTrue(self.client.permanently_failed)
            self.assertEqual(self.records, [])
        self.prepare()
        self.raw.is_connected = False
        self.assertFalse(self.client.reusable(3))
        with self.assertRaisesRegex(read.ProbeError, "session_disconnected"):
            await self.client.operate("relay", True)
        self.assertEqual(self.records, [])

    async def test_ack_failure_after_readback_prevents_success(self):
        self.prepare()
        async def failed_ack():
            await asyncio.sleep(0)
            raise RuntimeError("private-frame-must-not-appear")
        ack = asyncio.create_task(failed_ack())
        self.client.ack_tasks.add(ack)
        ack.add_done_callback(self.client._ack_done)
        with self.assertRaises(read.ProbeError):
            await self.client.operate("relay", True)
        self.assertTrue(self.client.permanently_failed)
        self.assertTrue(self.result["maybe_executed"])

    async def test_cancelled_ack_prevents_success(self):
        self.prepare()
        ack = asyncio.create_task(asyncio.sleep(30))
        self.client.ack_tasks.add(ack)
        ack.add_done_callback(self.client._ack_done)
        ack.cancel()
        with self.assertRaises(read.ProbeError):
            await self.client.operate("read")
        self.assertTrue(self.client.permanently_failed)

    async def test_idle_lifetime_and_whole_request_budget(self):
        self.prepare()
        await self.client.operate("relay", False)
        self.client.last_completed_at -= 90.1
        self.assertFalse(self.client.reusable(3))
        self.assertEqual(self.client.remaining_idle(), 0)
        self.prepare()
        self.client.session_started_at -= 300.1
        self.assertFalse(self.client.reusable(3))
        self.prepare()
        self.client.next_counter = 62
        self.assertTrue(self.client.reusable(3))
        self.assertFalse(self.client.reusable(4))
        self.client.next_counter = 63
        with self.assertRaisesRegex(read.ProbeError, "session_command_budget_exhausted"):
            await self.client.operate("relay", True)
        self.assertEqual(self.records, [])

    async def test_invalid_types_do_not_send_or_poison(self):
        self.prepare()
        for value in (0, 1, "on", None):
            with self.assertRaises(read.ProbeError):
                await self.client.operate("relay", value)
        with self.assertRaises(read.ProbeError):
            await self.client.operate("setting", True)
        self.assertEqual(self.records, [])
        self.assertFalse(self.client.permanently_failed)

    async def test_target_binding_and_model_restriction(self):
        cls = session.make_session_client(UPSTREAM)
        for address, model in (("AA:BB:CC:DD:EE:02", MODEL), (ADDRESS, "yutai.plug.fsov7m")):
            with self.assertRaises(read.ProbeError):
                cls(types.SimpleNamespace(address=address), b"x" * 32, FakeRaw(),
                    {"address": ADDRESS, "model": model})
        for address in (ADDRESS, "AA:BB:CC:DD:EE:02"):
            self.assertEqual(cls(types.SimpleNamespace(address=address), b"x" * 32, FakeRaw(),
                                 {"address": address, "model": MODEL}).address, address)

    async def test_exact_set_whitelist(self):
        self.prepare(initial=True)
        client = self.client
        client.active, client.stage = True, "set"
        client.initial, client.requested, client.write_attempts = True, False, 1
        client.next_counter = 2
        tid = read.READ_TID
        client.expected_responses[tid] = ("set", (2, 1))
        valid = session.relay.bool_set_packet(pack, tid, False)
        for packet, counter in (
            (session.relay.bool_set_packet(pack, tid, True), 1),
            (pack(tid, 0, b"\1\7\1\0\1\0\0"), 1),
            (pack(tid, 0, b"\1\5\4\0\4\x50\0\0\0\0"), 1),
            (valid, 0), (valid, 2),
        ):
            client.reserved_tx = (packet, counter)
            with self.assertRaises(read.ProbeError):
                await client.send(packet, counter)
        self.assertEqual(self.raw.writes, [])
        client.reserved_tx = (valid, 1)
        await client.send(valid, 1)
        self.assertEqual(len(self.raw.writes), 2)
        with self.assertRaises(read.ProbeError):
            await client.c.write_gatt_char(UPSTREAM.CMD, b"unreserved")

    async def test_f0_only_once(self):
        self.prepare()
        self.client.sent.clear()
        self.client.f0_verified = False
        self.client.authorize_send(pack(1, 0xF0), 0)
        self.client.sent.append((pack(1, 0xF0), 0))
        self.assertTrue(self.client.parse_pending_response(pack(1, 0xF0))["verified"])
        with self.assertRaises(read.ProbeError):
            self.client.authorize_send(pack(1, 0xF0), 0)
        self.assertTrue((await self.client.operate("relay", True))["verified"])


if __name__ == "__main__":
    unittest.main()

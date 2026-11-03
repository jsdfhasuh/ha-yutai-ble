"""Offline absolute-ON tests; no Bluetooth, SSH, phone, or real credentials."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest

import read_probe as read
import relay_on as on
from test_read_probe import FakeRaw, UPSTREAM, pack
from test_relay_off import get_response, set_response


class ParserTests(unittest.TestCase):
    def test_fixed_bool_true_wire_has_no_value_argument(self):
        self.assertEqual(read.parse_frame(on.on_set_packet(pack, 9)), (9, 0, b"\1\2\1\0\1\0\1"))
        with self.assertRaises(TypeError):
            on.on_set_packet(pack, 9, False)

    def test_get_parser_rejects_other_targets_types_values_and_frames(self):
        self.assertIs(on.parse_get_response(get_response(9, 0), 9), False)
        self.assertIs(on.parse_get_response(get_response(9, 1), 9), True)
        valid = get_response(9, 1)
        for packet in (get_response(8, 1), get_response(9, 2), get_response(9, 1, typelen=0x1001),
                       get_response(9, 1, siid=5, piid=4), get_response(9, 1, status=1),
                       valid[:-1], valid + b"x", pack(9, 1, valid[5:])):
            with self.subTest(packet=packet.hex()), self.assertRaises(read.ProbeError):
                on.parse_get_response(packet, 9)

    def test_set_parser_is_strict(self):
        valid = set_response(9)
        self.assertEqual(on.wire.parse_set_response(valid, 9), {"siid": 2, "piid": 1, "status": 0})
        for packet in (set_response(8), set_response(9, siid=5, piid=4), set_response(9, status=1),
                       valid[:-1], valid + b"x", pack(9, 0, valid[5:])):
            with self.subTest(packet=packet.hex()), self.assertRaises(read.ProbeError):
                on.wire.parse_set_response(packet, 9)

    def test_cli_input_has_exact_identity_and_no_action_options(self):
        secret = "ab" * 32
        base = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": secret}
        for changed in ({**base, "target": False}, {**base, "target": True},
                        {**base, "toggle": True}, {**base, "retry": 1},
                        {**base, "restore": True}, {**base, "model": "other"},
                        {**base, "address": "00:00:00:00:00:00"}):
            process = subprocess.run([sys.executable, "-B", str(Path(on.__file__))],
                                     input=json.dumps(changed), text=True, capture_output=True, timeout=5)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(process.stderr, "")
            self.assertNotIn(secret, process.stdout)
            self.assertEqual(json.loads(process.stdout)["status"], "error")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, initial=False, error_on=None, observed=True, set_status=0):
        self.result, self.raw = {}, FakeRaw()
        self.client = on.make_on_client(UPSTREAM)(object(), b"x" * 32, self.raw, self.result)
        self.raw.owner = self.client
        self.client.authenticated = self.client.f0_verified = True
        self.client.sent = [(pack(1, 0xF0), 0)]
        self.records = []
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.block_on = None

        async def exchange(packet, tid, counter):
            self.assertGreater(self.client.next_tid, tid)
            self.assertGreater(self.client.next_counter, counter)
            self.client.authorize_send(packet, counter)
            self.client.sent.append((packet, counter))
            self.records.append((tid, counter, packet))
            if len(self.records) == self.block_on:
                self.started.set()
                await self.release.wait()
            if len(self.records) == error_on:
                raise TimeoutError("must-not-log")
            if packet[4] == 0:
                self.assertEqual(packet, on.on_set_packet(pack, tid))
                response = set_response(tid, status=set_status)
            else:
                response = get_response(tid, initial if len(self.records) == 1 else observed)
            return self.client.parse_pending_response(response)

        self.client.exchange = exchange

    async def test_off_to_on_is_single_absolute_true_write_and_fresh_readback(self):
        self.prepare()
        await self.client.control()
        self.assertEqual([packet[4] for _, _, packet in self.records], [2, 0, 2])
        self.assertEqual([counter for _, counter, _ in self.records], [1, 2, 3])
        self.assertEqual([tid for tid, _, _ in self.records], [read.READ_TID + i for i in range(3)])
        self.assertIs(self.result["initial"], False)
        self.assertIs(self.result["target"], True)
        self.assertIs(self.result["observed"], True)
        self.assertTrue(self.result["verified"])
        self.assertTrue(self.result["changed"])
        self.assertTrue(self.result["maybe_executed"])
        self.assertEqual(self.result["relay_writes"], 1)
        self.assertEqual(self.result["relay_write_attempts"], 1)
        self.assertEqual(self.result["status"], "on_verified")

    async def test_already_on_skips_write_but_independently_reads_back(self):
        self.prepare(initial=True)
        await self.client.control()
        self.assertEqual([packet[4] for _, _, packet in self.records], [2, 2])
        self.assertFalse(self.result["maybe_executed"])
        self.assertFalse(self.result["changed"])
        self.assertTrue(self.result["verified"])
        self.assertEqual(self.result["relay_writes"], 0)
        self.assertEqual(self.result["relay_write_attempts"], 0)

    async def test_failures_are_not_retried_or_restored(self):
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at):
                self.prepare(error_on=fail_at)
                with self.assertRaises(TimeoutError):
                    await self.client.control()
                self.assertEqual(len(self.records), fail_at)
                self.assertEqual(self.result["relay_writes"], int(fail_at > 1))
                self.assertEqual(self.result["maybe_executed"], fail_at > 1)
                self.assertFalse(self.result["verified"])
                self.assertTrue(self.client.failed)
                self.assertIsNone(self.client.reserved_tx)
                self.assertEqual(self.client.expected_responses, {})
                for kind in ("initial", "on", "verify"):
                    with self.assertRaises(read.ProbeError):
                        await self.client.request_reserved(kind)
                with self.assertRaises(read.ProbeError):
                    await self.client.control()
                self.assertEqual(len(self.records), fail_at)
                self.assertNotIn("must-not-log", json.dumps(self.result))

    async def test_readback_off_and_failed_ack_do_not_trigger_another_write(self):
        self.prepare(observed=False)
        with self.assertRaisesRegex(read.ProbeError, "on_readback_mismatch"):
            await self.client.control()
        self.assertEqual(len(self.records), 3)
        self.assertEqual(self.result["relay_write_attempts"], 1)
        self.assertFalse(self.result["verified"])
        self.prepare(set_status=1)
        with self.assertRaises(read.ProbeError):
            await self.client.control()
        self.assertEqual(len(self.records), 2)
        self.assertEqual(self.result["relay_write_attempts"], 1)

    async def test_already_on_that_changes_to_off_is_not_falsely_verified_or_written(self):
        self.prepare(initial=True, observed=False)
        with self.assertRaisesRegex(read.ProbeError, "on_readback_mismatch"):
            await self.client.control()
        self.assertEqual(len(self.records), 2)
        self.assertEqual(self.result["relay_writes"], 0)

    async def test_no_repeated_control_after_success(self):
        self.prepare()
        await self.client.control()
        with self.assertRaises(read.ProbeError):
            await self.client.control()
        for kind in ("initial", "on", "verify"):
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved(kind)
        self.assertEqual(len(self.records), 3)

    async def test_pre_guard_and_bad_protocol_phase_are_blocked(self):
        for kind in ("on", "verify"):
            self.prepare()
            self.client.started = True
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved(kind)
            self.assertEqual(self.records, [])
        for attribute in ("authenticated", "f0_verified"):
            self.prepare()
            setattr(self.client, attribute, False)
            with self.assertRaises(read.ProbeError):
                await self.client.control()
            self.assertEqual(self.records, [])

    async def test_cancelled_set_is_not_retried(self):
        self.prepare()
        self.block_on = 2
        task = asyncio.create_task(self.client.control())
        await self.started.wait()
        with self.assertRaisesRegex(read.ProbeError, "on_request_in_progress"):
            await self.client.request_reserved("verify")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.client.failed)
        self.assertEqual(len(self.records), 2)
        self.assertEqual(self.result["relay_writes"], 1)

    def test_direct_unreserved_set_false_other_target_and_action_are_blocked(self):
        self.prepare()
        for packet in (on.on_set_packet(pack, 9), on.wire.bool_set_packet(pack, 9, False),
                       pack(9, 0, b"\1\5\4\0\4\x50\0\0\0\0"), pack(9, 5),
                       self.client.get_pkt(9, [(2, 1)]), pack(1, 0xF0)):
            with self.assertRaises(read.ProbeError):
                self.client.authorize_send(packet, 1)
        for method in (self.client.set_pkt, self.client.action_pkt):
            with self.assertRaises(read.ProbeError):
                method()

    async def test_actual_transport_accepts_only_reserved_true_not_false(self):
        self.prepare()
        self.client.ch = type("Channel", (), {"_task": None})()
        self.client.started, self.client.initial, self.client.write_attempts = True, False, 1
        tid, counter = read.READ_TID + 1, 2
        false_packet = on.wire.bool_set_packet(pack, tid, False)
        self.client.expected_responses[tid] = "on"
        self.client.reserved_tx = (false_packet, counter)
        with self.assertRaises(read.ProbeError):
            await self.client.send(false_packet, counter)
        self.assertEqual(self.raw.writes, [])
        packet = on.on_set_packet(pack, tid)
        self.client.reserved_tx = (packet, counter)
        await self.client.send(packet, counter)
        self.assertEqual(len(self.raw.writes), 2)
        self.assertEqual(self.raw.writes[1][1][4:], packet)
        with self.assertRaises(read.ProbeError):
            await self.client.c.write_gatt_char(UPSTREAM.CMD, b"not-reserved")


if __name__ == "__main__":
    unittest.main()

"""Offline native-local write tests; no credentials or device access."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest

import native_local_control as control
import read_probe as read
from test_read_probe import FakeRaw, UPSTREAM, pack
from test_timer_control import get_response, set_response


LOCAL = 0x2AAB8D77
MASTER_OFF = 0x4B03C004
MASTER_ON = 0x4B03C006


class ParserTests(unittest.TestCase):
    def test_observed_master_vector_changes_only_bit1(self):
        self.assertEqual(control.field_target(MASTER_ON, "master", False), MASTER_OFF)
        self.assertEqual(control.field_target(MASTER_OFF, "master", True), MASTER_ON)
        for original in (0, 0xFFFFFFFF, MASTER_ON):
            for value in (False, True):
                self.assertEqual((control.field_target(original, "master", value) ^ original) & ~2, 0)

    def test_all_six_local_fields_preserve_every_other_bit(self):
        vectors = {"start_min": (0, 1393, 1439), "end_min": (0, 1399, 1439),
                   "start_enabled": (False, True), "end_enabled": (False, True),
                   "repeat": (False, True), "weekday_mask": (0, 42, 85, 127)}
        for field, values in vectors.items():
            for original in (0, LOCAL, 0xFFFFFFFF):
                for value in values:
                    changed = control.field_target(original, field, value)
                    self.assertEqual((changed ^ original) & ~control.FIELD_MASKS[field], 0)
        self.assertEqual(control.field_target(LOCAL, "start_enabled", True), LOCAL | 0x80000000)
        self.assertEqual(control.field_target(LOCAL, "end_enabled", True), LOCAL | 0x40000000)
        self.assertEqual(control.field_target(LOCAL, "weekday_mask", 85), 0x356B8D77)

    def test_strict_types_ranges_and_unknown_fields(self):
        for field in ("master", "start_enabled", "end_enabled", "repeat"):
            for value in (0, 1, None, "true", 1.0):
                with self.subTest(field=field, value=value), self.assertRaises(read.ProbeError):
                    control.field_target(LOCAL, field, value)
        for field, maximum in (("start_min", 1439), ("end_min", 1439), ("weekday_mask", 127)):
            for value in (True, False, -1, maximum + 1, None, "1", 1.0):
                with self.subTest(field=field, value=value), self.assertRaises(read.ProbeError):
                    control.field_target(LOCAL, field, value)
        for field in ("cycle", "clock", "raw", "mode", "relay", "weekday_names", "enabled", None, []):
            with self.subTest(field=field), self.assertRaises(read.ProbeError):
                control.field_target(LOCAL, field, True)
        for word in (True, -1, 0x100000000, None, 1.0):
            with self.assertRaises(read.ProbeError):
                control.field_target(word, "master", False)

    def test_input_exact_identity_options_and_snapshot_types(self):
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": "ab" * 32,
                "field": "master", "value": False, "expected_local_word": LOCAL, "expected_master_word": MASTER_ON}
        key, options = control.validated_input(data)
        self.assertEqual(key.key, b"\xab" * 32)
        self.assertEqual(set(options), {"field", "value", "expected_local_word", "expected_master_word"})
        for changed in ({**data, "siid": 2}, {**data, "target": True}, {**data, "model": "other"},
                        {**data, "address": "00:00:00:00:00:00"}, {**data, "expected_local_word": True},
                        {**data, "expected_master_word": -1}, {key: value for key, value in data.items() if key != "value"}):
            with self.subTest(keys=list(changed)), self.assertRaises(read.ProbeError):
                control.validated_input(changed)

    def test_exact_uint32_wire_and_no_other_properties(self):
        for pair, value in (((5, 6), MASTER_OFF), ((5, 7), LOCAL)):
            self.assertEqual(read.parse_frame(control.uint32_set_packet(pack, 9, pair, value)),
                (9, 0, bytes([1, 5, pair[1], 0, 4, 0x50]) + value.to_bytes(4, "little")))
        for pair in ((2, 1), (5, 1), (5, 4), (5, 5)):
            with self.assertRaises(read.ProbeError):
                control.uint32_set_packet(pack, 9, pair, 0)

    def test_typed_get_and_strict_ack_parser(self):
        props = control.GUARD_PROPERTIES
        words = {(2, 1): False, (5, 6): MASTER_ON, (5, 7): LOCAL}
        valid = get_response(9, props, words)
        self.assertEqual(len(control.parse_get_response(valid, 9, props)), 3)
        for packet in (get_response(8, props, words), get_response(9, props[::-1], words),
                       get_response(9, props, words, typelens={(2, 1): 0x1001}),
                       get_response(9, props, words, typelens={(5, 6): 0x6004}),
                       get_response(9, props, words, statuses={(5, 7): 1}), valid[:-1], valid + b"x"):
            with self.assertRaises(read.ProbeError):
                control.parse_get_response(packet, 9, props)
        self.assertEqual(control.parse_set_response(set_response(9, (5, 6)), 9, (5, 6))["status"], 0)
        for packet in (set_response(8, (5, 6)), set_response(9, (5, 7)),
                       set_response(9, (5, 6), status=1), set_response(9, (5, 6)) + b"x"):
            with self.assertRaises(read.ProbeError):
                control.parse_set_response(packet, 9, (5, 6))

    def test_rejected_cli_input_never_exposes_secret(self):
        secret = "ab" * 32
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": secret,
                "field": "clock", "value": 123, "expected_local_word": LOCAL, "expected_master_word": MASTER_ON}
        process = subprocess.run([sys.executable, "-B", str(Path(control.__file__))], input=json.dumps(data),
                                 text=True, capture_output=True, timeout=5)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stderr, "")
        self.assertNotIn(secret, process.stdout)
        self.assertEqual(json.loads(process.stdout)["error_code"], "native_local_field_not_supported")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, field="master", value=False, local=LOCAL, master=MASTER_ON, relay=False,
                expected_local=None, expected_master=None, error_on=None, observed=None, typelens=None, statuses=None):
        self.result, self.raw, self.records = {}, FakeRaw(), []
        self.words = {(2, 1): relay, (5, 6): master, (5, 7): local}
        self.client = control.make_native_local_client(UPSTREAM, field=field, value=value,
            expected_local_word=local if expected_local is None else expected_local,
            expected_master_word=master if expected_master is None else expected_master)(object(), b"x" * 32, self.raw, self.result)
        self.raw.owner = self.client
        self.client.authenticated = self.client.f0_verified = True
        self.client.sent = [(pack(1, 0xF0), 0)]
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.block_on = None
        pair, _mask = control.selection(field)

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
                self.assertEqual((packet[6], int.from_bytes(packet[7:9], "little")), pair)
                self.words[pair] = int.from_bytes(packet[-4:], "little")
                response = set_response(tid, pair)
            else:
                body = packet[5:]
                props = tuple((body[offset], int.from_bytes(body[offset + 1:offset + 3], "little"))
                              for offset in range(1, len(body), 3))
                words = dict(self.words)
                if len(self.records) > 1 and observed is not None:
                    words[pair] = observed
                response = get_response(tid, props, words, typelens, statuses)
            return self.client.parse_pending_response(response)

        self.client.exchange = exchange

    async def test_master_disarm_while_relay_on_preserves_all_other_bits(self):
        self.prepare(relay=True)
        await self.client.control()
        self.assertEqual(self.result["initial"], MASTER_ON)
        self.assertEqual(self.result["target"], MASTER_OFF)
        self.assertEqual(self.result["observed"], MASTER_OFF)
        self.assertTrue(self.result["disarm"])
        self.assertTrue(self.result["verified"])
        self.assertEqual(self.words[(2, 1)], True)
        self.assertEqual(self.words[(5, 7)], LOCAL)
        self.assertEqual([packet[4] for _, _, packet in self.records], [2, 0, 2])
        self.assertEqual([counter for _, counter, _ in self.records], [1, 2, 3])
        self.assertEqual(self.result["setting_write_attempts"], 1)
        self.assertEqual(self.result["relay_writes"], 0)

    async def test_child_disarm_allowed_with_master_and_relay_on(self):
        for field, bit in (("start_enabled", 0x80000000), ("end_enabled", 0x40000000)):
            self.prepare(field=field, value=False, local=LOCAL | bit, relay=True)
            await self.client.control()
            self.assertEqual(self.result["observed"], LOCAL)
            self.assertEqual(self.words[(5, 6)], MASTER_ON)
            self.assertEqual(self.result["setting_write_attempts"], 1)

    async def test_all_local_fields_configure_with_master_off_even_if_other_child_on(self):
        for field, value in (("start_min", 600), ("end_min", 1080), ("weekday_mask", 85),
                             ("repeat", False), ("start_enabled", True), ("end_enabled", True)):
            other_child = 0x40000000 if field == "start_enabled" else 0x80000000
            self.prepare(field=field, value=value, local=LOCAL | other_child, master=MASTER_OFF)
            await self.client.control()
            self.assertTrue(self.result["verified"])
            self.assertEqual((self.result["initial"] ^ self.result["observed"]) & ~control.FIELD_MASKS[field], 0)
            self.assertEqual(self.result["relay_writes"], 0)

    async def test_master_arm_allows_children_already_armed_but_requires_relay_off(self):
        self.prepare(field="master", value=True, local=LOCAL | 0xC0000000, master=MASTER_OFF)
        await self.client.control()
        self.assertEqual(self.result["observed"], MASTER_ON)
        self.assertFalse(self.result["disarm"])

    async def test_non_disarm_fields_reject_relay_on(self):
        for field, value in (("master", True), ("start_min", 600), ("end_min", 1000),
                             ("weekday_mask", 85), ("repeat", False), ("start_enabled", True), ("end_enabled", True)):
            self.prepare(field=field, value=value, relay=True, master=MASTER_OFF)
            with self.assertRaisesRegex(read.ProbeError, "native_local_relay_must_be_off"):
                await self.client.control()
            self.assertEqual(len(self.records), 1)
            self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_local_non_disarm_rejects_master_on(self):
        for field, value in (("start_min", 600), ("end_min", 1000), ("weekday_mask", 85),
                             ("repeat", False), ("start_enabled", True), ("end_enabled", True)):
            self.prepare(field=field, value=value)
            with self.assertRaisesRegex(read.ProbeError, "native_local_master_must_be_off"):
                await self.client.control()
            self.assertEqual(len(self.records), 1)
            self.assertFalse(self.result["maybe_executed"])

    async def test_either_snapshot_mismatch_blocks_even_disarm(self):
        for options, error in (({"expected_local": LOCAL ^ 1}, "native_local_snapshot_mismatch"),
                               ({"expected_master": MASTER_ON ^ 4}, "native_local_master_snapshot_mismatch")):
            self.prepare(relay=True, **options)
            with self.assertRaisesRegex(read.ProbeError, error):
                await self.client.control()
            self.assertEqual(len(self.records), 1)
            self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_no_change_still_guards_and_reads_back_independently(self):
        self.prepare(master=MASTER_OFF, relay=True)
        await self.client.control()
        self.assertEqual([packet[4] for _, _, packet in self.records], [2, 2])
        self.assertEqual(self.result["setting_write_attempts"], 0)
        self.assertFalse(self.result["maybe_executed"])
        self.assertFalse(self.result["changed"])
        self.assertTrue(self.result["verified"])

    async def test_full_word_readback_rejects_non_target_bit_change(self):
        self.prepare(observed=MASTER_OFF ^ 4)
        with self.assertRaisesRegex(read.ProbeError, "native_local_readback_mismatch"):
            await self.client.control()
        self.assertEqual(len(self.records), 3)
        self.assertFalse(self.result["verified"])

    async def test_all_request_failures_stop_without_retry_or_restore(self):
        for fail_at in (1, 2, 3):
            self.prepare(error_on=fail_at)
            with self.assertRaises(TimeoutError):
                await self.client.control()
            self.assertEqual(len(self.records), fail_at)
            self.assertEqual(self.result["setting_write_attempts"], int(fail_at > 1))
            self.assertEqual(self.result["maybe_executed"], fail_at > 1)
            self.assertTrue(self.client.failed)
            self.assertFalse(self.result["verified"])
            self.assertIsNone(self.client.reserved_tx)
            self.assertEqual(self.client.expected_responses, {})
            for kind in ("guards", "set", "verify"):
                with self.assertRaises(read.ProbeError):
                    await self.client.request_reserved(kind)
            with self.assertRaises(read.ProbeError):
                await self.client.control()
            self.assertNotIn("must-not-log", json.dumps(self.result))

    async def test_bad_guard_type_and_status_never_write(self):
        for options in ({"typelens": {(2, 1): 0x1001}}, {"typelens": {(5, 6): 0x6004}},
                        {"statuses": {(5, 7): 1}}):
            self.prepare(**options)
            with self.assertRaises(read.ProbeError):
                await self.client.control()
            self.assertEqual(len(self.records), 1)
            self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_cancelled_set_never_retries(self):
        self.prepare()
        self.block_on = 2
        task = asyncio.create_task(self.client.control())
        await self.started.wait()
        with self.assertRaisesRegex(read.ProbeError, "native_local_request_in_progress"):
            await self.client.request_reserved("verify")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.client.failed)
        self.assertEqual(len(self.records), 2)

    async def test_finished_or_pre_guard_requests_rejected(self):
        self.prepare()
        self.client.started = True
        for kind in ("set", "verify"):
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved(kind)
        self.prepare()
        await self.client.control()
        for kind in ("guards", "set", "verify"):
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved(kind)
        with self.assertRaises(read.ProbeError):
            await self.client.control()

    def test_unreserved_relay_other_settings_actions_and_raw_helpers_blocked(self):
        self.prepare()
        for packet in (pack(9, 0, b"\1\2\1\0\1\0\1"), pack(9, 5), pack(1, 0xF0),
                       self.client.get_pkt(9, [(5, 4)]), control.uint32_set_packet(pack, 9, (5, 6), MASTER_OFF)):
            with self.assertRaises(read.ProbeError):
                self.client.authorize_send(packet, 1)
        for method in (self.client.set_pkt, self.client.action_pkt):
            with self.assertRaises(read.ProbeError):
                method()

    async def test_actual_guarded_transport_rejects_reserved_outside_mask(self):
        self.prepare()
        self.client.ch = type("Channel", (), {"_task": None})()
        self.client.started = self.client.guards_verified = True
        self.client.initial, self.client.target, self.client.write_attempts = MASTER_ON, MASTER_OFF, 1
        tid, counter = read.READ_TID + 1, 2
        wrong = control.uint32_set_packet(pack, tid, (5, 6), MASTER_OFF ^ 4)
        self.client.expected_responses[tid] = ("set", ())
        self.client.reserved_tx = (wrong, counter)
        with self.assertRaises(read.ProbeError):
            await self.client.send(wrong, counter)
        self.assertEqual(self.raw.writes, [])
        packet = control.uint32_set_packet(pack, tid, (5, 6), MASTER_OFF)
        self.client.reserved_tx = (packet, counter)
        await self.client.send(packet, counter)
        self.assertEqual(len(self.raw.writes), 2)
        self.assertEqual(self.raw.writes[1][1][4:], packet)
        with self.assertRaises(read.ProbeError):
            await self.client.c.write_gatt_char(UPSTREAM.CMD, b"not-reserved")


if __name__ == "__main__":
    unittest.main()

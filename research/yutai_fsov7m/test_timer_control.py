"""Offline guarded timer writes; no Bluetooth, SSH, phone, or cloud operations."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest

import read_probe as read
import timer_control as control
from test_read_probe import FakeRaw, UPSTREAM, pack


BASE_LOCAL = 0x2AAB8D77
BASE_MASTER = 0x4B03C004
BASE_SCHEDULE = 0x6A0B4438
BOOLEAN_FIELDS = ("start_state", "end_state", "repeat", "master")
COMPOUND_VALUES = {
    "schedule_config": {"start_min": 600, "end_min": 720, "repeat": False, "weekday_mask": 127},
    "on_config": {"on_seconds": 60, "start_state": True},
    "off_config": {"off_seconds": 60, "end_state": False},
}


def field_value(field, integer=600, weekday=85):
    if field in COMPOUND_VALUES:
        return COMPOUND_VALUES[field].copy()
    return False if field in BOOLEAN_FIELDS else weekday if field == "weekday_mask" else integer


def get_response(tid, properties, words, typelens=None, statuses=None):
    typelens = typelens or {}
    statuses = statuses or {}
    body = bytes([len(properties)])
    for pair in properties:
        status = statuses.get(pair, 0)
        body += bytes([pair[0]]) + pair[1].to_bytes(2, "little") + status.to_bytes(2, "little")
        if status == 0:
            typelen = typelens.get(pair, 1 if pair == (2, 1) else 0x5004)
            length = typelen & 0xFFF
            body += typelen.to_bytes(2, "little") + int(words[pair]).to_bytes(length, "little")
    return pack(tid, 3, body)


def set_response(tid, pair, status=0):
    return pack(tid, 1, bytes([1, pair[0]]) + pair[1].to_bytes(2, "little") + status.to_bytes(2, "little"))


class ParserTests(unittest.TestCase):
    def test_supported_selections_and_field_preservation(self):
        for target, field in control.SELECTIONS:
            if field in COMPOUND_VALUES:
                continue
            pair, mask = control.selection(target, field)
            for initial in (0, 0xFFFFFFFF, BASE_LOCAL, BASE_SCHEDULE):
                values = (False, True) if field in BOOLEAN_FIELDS else (0, 127) if field == "weekday_mask" else (0, 1399)
                for value in values:
                    with self.subTest(target=target, field=field, initial=initial, value=value):
                        changed = control.timer_target(initial, target, field, value)
                        self.assertEqual((changed ^ initial) & ~mask, 0)
                        shift = {"start_min": 11, "weekday_mask": 22, "start_state": 31, "end_state": 31, "repeat": 29, "master": 31}.get(field, 0)
                        self.assertEqual(changed & mask, int(value) << shift)
                        self.assertIn(pair, ((5, 1), (5, 2), (5, 3), (5, 7)))

    def test_compound_words_match_scalar_codec_and_preserve_unknown_schedule_bits(self):
        for field, value in COMPOUND_VALUES.items():
            pair, mask = control.selection("cycle", field)
            for initial in (0, 0xFFFFFFFF, BASE_SCHEDULE):
                changed = control.timer_target(initial, "cycle", field, value)
                scalar = initial
                for name, member in value.items():
                    scalar = control.timer_target(scalar, "cycle", name, member)
                self.assertEqual(changed, scalar)
                self.assertEqual((initial ^ changed) & ~mask, 0)
                if field == "schedule_config":
                    self.assertEqual(changed & 0xC0000000, initial & 0xC0000000)
                    self.assertEqual(mask, 0x3FFFFFFF)
                    self.assertEqual(pair, (5, 1))
                else:
                    self.assertEqual(mask, 0xFFFFFFFF)

    def test_compound_exact_keys_and_strict_member_values(self):
        for field, valid in COMPOUND_VALUES.items():
            malformed = [None, True, 60, "{}", list(valid), {},
                         {**valid, "master": False}, {**valid, "raw": 0}]
            malformed.extend({key: value for key, value in valid.items() if key != missing} for missing in valid)
            for value in malformed:
                with self.subTest(field=field, value=value), self.assertRaises(read.ProbeError):
                    control.timer_target(0, "cycle", field, value)
            for name in valid:
                boolean = name in BOOLEAN_FIELDS
                maximum = 1439 if name.endswith("_min") else 127 if name == "weekday_mask" else 0x7FFFFFFF
                accepted = (False, True) if boolean else (0, maximum)
                rejected = (0, 1, "false", None, 1.0) if boolean else (False, True, -1, maximum + 1, None, "60", 60.0)
                for member in accepted:
                    self.assertIsInstance(control.timer_target(0, "cycle", field, {**valid, name: member}), int)
                for member in rejected:
                    with self.subTest(field=field, name=name, member=member), self.assertRaises(read.ProbeError):
                        control.timer_target(0, "cycle", field, {**valid, name: member})
            with self.assertRaises(read.ProbeError):
                control.timer_target(0, "local", field, valid)

    def test_cycle_unknown_high_bits_and_duration_state_are_preserved(self):
        for field in ("start_min", "end_min"):
            self.assertEqual(control.timer_target(0xFFFFFFFF, "cycle", field, 360) & 0xC0000000, 0xC0000000)
        for field in ("on_seconds", "off_seconds"):
            for state in (0, 0x80000000):
                self.assertEqual(control.timer_target(state | 1800, "cycle", field, 60), state | 60)

    def test_unsupported_targets_and_fields_never_fall_through(self):
        for target, field in (("local", "on_seconds"), ("local", "off_seconds"),
                              ("relay", "start_min"), ("5.7", "start_min"),
                              (None, "start_min"), ([], "start_min")):
            with self.subTest(target=target, field=field), self.assertRaises(read.ProbeError):
                control.timer_target(0, target, field, 1)
        for target in ("local", "cycle"):
            for field in ("enable", "enabled", "start_enabled", "end_enabled", "clock", "raw", None, []):
                with self.subTest(target=target, field=field), self.assertRaises(read.ProbeError):
                    control.timer_target(0, target, field, 1)
        for field in ("start_state", "end_state", "repeat", "weekday_mask", "master"):
            with self.assertRaises(read.ProbeError):
                control.timer_target(0, "local", field, True if field in BOOLEAN_FIELDS else 42)

    def test_value_and_expected_words_are_strict_integers(self):
        for target, field in control.SELECTIONS:
            if field in COMPOUND_VALUES:
                continue
            if field in BOOLEAN_FIELDS:
                for value in (0, 1, None, "true", 1.0):
                    with self.subTest(field=field, value=value), self.assertRaises(read.ProbeError):
                        control.timer_target(0, target, field, value)
                continue
            maximum = 1439 if field.endswith("_min") else 127 if field == "weekday_mask" else 0x7FFFFFFF
            self.assertIsInstance(control.timer_target(0, target, field, maximum), int)
            for value in (True, False, -1, maximum + 1, None, "60", 60.0):
                with self.subTest(target=target, field=field, value=value), self.assertRaises(read.ProbeError):
                    control.timer_target(0, target, field, value)
        for word in (True, False, -1, 0x100000000, None, "1", 1.0):
            with self.subTest(word=word), self.assertRaises(read.ProbeError):
                control.make_timer_client(UPSTREAM, target="local", field="start_min", value=1,
                                          expected_initial=word, expected_master_word=BASE_MASTER, expected_local_word=BASE_LOCAL,
                                          expected_cycle_word=BASE_SCHEDULE)
            with self.assertRaises(read.ProbeError):
                control.make_timer_client(UPSTREAM, target="local", field="start_min", value=1,
                                          expected_initial=0, expected_master_word=word, expected_local_word=BASE_LOCAL,
                                          expected_cycle_word=BASE_SCHEDULE)
            with self.assertRaises(read.ProbeError):
                control.make_timer_client(UPSTREAM, target="local", field="start_min", value=1,
                                          expected_initial=0, expected_master_word=BASE_MASTER, expected_local_word=word,
                                          expected_cycle_word=BASE_SCHEDULE)
            with self.assertRaises(read.ProbeError):
                control.make_timer_client(UPSTREAM, target="local", field="start_min", value=1,
                                          expected_initial=0, expected_master_word=BASE_MASTER, expected_local_word=BASE_LOCAL,
                                          expected_cycle_word=word)

    def test_exact_bound_input_and_no_raw_target_override(self):
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": "ab" * 32,
                "target": "local", "field": "start_min", "value": 360,
                "expected_initial": BASE_LOCAL, "expected_master_word": BASE_MASTER, "expected_local_word": BASE_LOCAL,
                "expected_cycle_word": BASE_SCHEDULE}
        key, options = control.validated_input(data)
        self.assertEqual(key.key, b"\xab" * 32)
        self.assertEqual(options, {name: data[name] for name in
                                  ("target", "field", "value", "expected_initial", "expected_master_word", "expected_local_word", "expected_cycle_word")})
        cases = ({**data, "siid": 2}, {**data, "piid": 1}, {**data, "properties": [[2, 1]]},
                 {**data, "model": "other"}, {**data, "address": "00:00:00:00:00:00"},
                 {key: value for key, value in data.items() if key != "expected_initial"},
                 {key: value for key, value in data.items() if key != "expected_master_word"},
                 {key: value for key, value in data.items() if key != "expected_local_word"},
                 {key: value for key, value in data.items() if key != "expected_cycle_word"},
                 {**data, "expected_control_word": 0x20400})
        for changed in cases:
            with self.subTest(keys=list(changed)), self.assertRaises(read.ProbeError):
                control.validated_input(changed)

    def test_exact_uint32_wire_packets_and_forbidden_properties(self):
        for pair in ((5, 1), (5, 2), (5, 3), (5, 7)):
            packet = control.uint32_set_packet(pack, 9, pair, 0x12345678)
            self.assertEqual(read.parse_frame(packet),
                             (9, 0, bytes([1, 5, pair[1], 0, 4, 0x50, 0x78, 0x56, 0x34, 0x12])))
        for pair in ((2, 1), (5, 4), (5, 5), (5, 6)):
            with self.subTest(pair=pair), self.assertRaises(read.ProbeError):
                control.uint32_set_packet(pack, 9, pair, 0)
        for word in (True, -1, 0x100000000, 0.0):
            with self.assertRaises(read.ProbeError):
                control.uint32_set_packet(pack, 9, (5, 7), word)

    def test_get_parser_strict_status_type_target_order_and_framing(self):
        props = ((2, 1), (5, 6), (5, 7), (5, 1))
        words = {(2, 1): False, (5, 7): BASE_LOCAL, (5, 6): BASE_MASTER, (5, 1): BASE_SCHEDULE}
        valid = get_response(9, props, words)
        self.assertEqual([item["value"] for item in control.parse_get_response(valid, 9, props)],
                         [False, BASE_MASTER, BASE_LOCAL, BASE_SCHEDULE])
        cases = (get_response(8, props, words), get_response(9, props[::-1], words),
                 get_response(9, props, words, statuses={(5, 7): 1}),
                 get_response(9, props, words, typelens={(5, 7): 0x6004}),
                 get_response(9, props, words, typelens={(2, 1): 0x1001}),
                 valid[:-1], valid + b"x", pack(9, 1, valid[5:]))
        for packet in cases:
            with self.subTest(packet=packet.hex()), self.assertRaises(read.ProbeError):
                control.parse_get_response(packet, 9, props)

    def test_set_parser_strict_target_status_and_framing(self):
        valid = set_response(9, (5, 7))
        self.assertEqual(control.parse_set_response(valid, 9, (5, 7)), {"siid": 5, "piid": 7, "status": 0})
        for packet in (set_response(8, (5, 7)), set_response(9, (5, 1)), set_response(9, (2, 1)),
                       set_response(9, (5, 7), status=1), valid[:-1], valid + b"x", pack(9, 0, valid[5:])):
            with self.subTest(packet=packet.hex()), self.assertRaises(read.ProbeError):
                control.parse_set_response(packet, 9, (5, 7))

    def test_invalid_cli_input_does_not_expose_secret(self):
        secret = "ab" * 32
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": secret,
                "target": "local", "field": "start_enabled", "value": True,
                "expected_initial": BASE_LOCAL, "expected_master_word": BASE_MASTER, "expected_local_word": BASE_LOCAL,
                "expected_cycle_word": BASE_SCHEDULE}
        process = subprocess.run([sys.executable, "-B", str(Path(control.__file__))], input=json.dumps(data),
                                 text=True, capture_output=True, timeout=5)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stderr, "")
        self.assertNotIn(secret, process.stdout)
        self.assertEqual(json.loads(process.stdout)["error_code"], "timer_field_not_supported")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, target="local", field="start_min", value=360, initial=None, overrides=None,
                error_on=None, observed=None, expected_initial=None, expected_master_word=BASE_MASTER,
                expected_local_word=None, expected_cycle_word=None,
                response_typelens=None, response_statuses=None):
        pair, self.mask = control.selection(target, field)
        if initial is None:
            initial = BASE_LOCAL if target == "local" else (BASE_SCHEDULE if pair == (5, 1) else 0x80000708)
            if field == "master":
                initial |= control.CYCLE_MASTER_BIT
        self.result = {}
        self.raw = FakeRaw()
        self.words = {(2, 1): False, (5, 7): BASE_LOCAL, (5, 6): BASE_MASTER,
                      (5, 1): BASE_SCHEDULE, (5, 2): 0x80000708, (5, 3): 1800}
        self.words[pair] = initial
        self.words.update(overrides or {})
        self.client = control.make_timer_client(UPSTREAM, target=target, field=field, value=value,
            expected_initial=initial if expected_initial is None else expected_initial,
            expected_master_word=expected_master_word,
            expected_local_word=self.words[(5, 7)] if expected_local_word is None else expected_local_word,
            expected_cycle_word=self.words[(5, 1)] if expected_cycle_word is None else expected_cycle_word)(object(), b"x" * 32, self.raw, self.result)
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
                self.assertEqual((packet[6], int.from_bytes(packet[7:9], "little")), pair)
                self.words[pair] = int.from_bytes(packet[-4:], "little")
                response = set_response(tid, pair)
            else:
                body = packet[5:]
                props = tuple((body[offset], int.from_bytes(body[offset + 1:offset + 3], "little"))
                              for offset in range(1, len(body), 3))
                words = dict(self.words)
                if self.client.guards_verified and observed is not None:
                    words[pair] = observed
                response = get_response(tid, props, words, response_typelens, response_statuses)
            return self.client.parse_pending_response(response)

        self.client.exchange = exchange

    async def test_all_operations_guard_single_write_and_independent_readback(self):
        for target, field in control.SELECTIONS:
            with self.subTest(target=target, field=field):
                self.prepare(target=target, field=field,
                             value=field_value(field))
                await self.client.control()
                count = self.result["guard_batch_count"]
                self.assertEqual([packet[4] for _, _, packet in self.records], [2] * count + [0, 2])
                self.assertEqual([counter for _, counter, _ in self.records], list(range(1, count + 3)))
                self.assertEqual([tid for tid, _, _ in self.records], [read.READ_TID + i for i in range(count + 2)])
                self.assertEqual(self.result["setting_write_attempts"], 1)
                self.assertTrue(self.result["maybe_executed"])
                self.assertTrue(self.result["guards_verified"])
                self.assertTrue(self.result["verified"])
                self.assertEqual(self.result["observed"], self.result["target"])
                self.assertEqual((self.result["initial"] ^ self.result["observed"]) & ~self.mask, 0)
                self.assertEqual(self.result["relay_writes"], 0)
                self.assertEqual(self.result["status"], "timer_verified")

    async def test_guards_share_one_deduplicated_batch_of_at_most_four(self):
        for target, field in control.SELECTIONS:
            self.prepare(target=target, field=field,
                         value=field_value(field, integer=360, weekday=42))
            await self.client.control()
            batch = self.result["initial_properties"]
            expected = 5 if control.selection(target, field)[0] in ((5, 2), (5, 3)) else 4
            self.assertEqual(len(batch), expected)
            pairs = [(item["siid"], item["piid"]) for item in batch]
            self.assertEqual(len(set(pairs)), expected)
            self.assertEqual(pairs[:4], [(2, 1), (5, 6), (5, 7), (5, 1)])
            for _tid, _counter, packet in self.records:
                if packet[4] == 2:
                    self.assertLessEqual(packet[5], 4)

    async def test_guard_failures_never_write(self):
        for overrides, expected_error in (({(2, 1): True}, "timer_relay_must_be_off"),
            ({(5, 7): BASE_LOCAL | 0x80000000}, "timer_local_children_must_be_off"),
            ({(5, 7): BASE_LOCAL | 0x40000000}, "timer_local_children_must_be_off"),
            ({(5, 6): BASE_MASTER ^ 1}, "timer_master_snapshot_mismatch")):
            with self.subTest(overrides=overrides):
                self.prepare(target="cycle", field="on_seconds", overrides=overrides)
                with self.assertRaisesRegex(read.ProbeError, expected_error):
                    await self.client.control()
                self.assertEqual(len(self.records), 2)
                self.assertEqual(self.result["setting_write_attempts"], 0)
                self.assertFalse(self.result["maybe_executed"])
                self.assertFalse(self.result["guards_verified"])

    async def test_stale_expected_initial_fails_without_write(self):
        self.prepare(expected_initial=BASE_LOCAL ^ 1)
        with self.assertRaisesRegex(read.ProbeError, "timer_expected_initial_mismatch"):
            await self.client.control()
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.result["initial"], BASE_LOCAL)
        self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_compound_client_copies_requested_value_before_control(self):
        requested = COMPOUND_VALUES["on_config"].copy()
        self.prepare(target="cycle", field="on_config", value=requested)
        requested["on_seconds"] = 999
        self.result["value"]["on_seconds"] = 888
        await self.client.control()
        self.assertEqual(self.result["observed"], 0x8000003C)

    async def test_compound_readback_verifies_the_entire_word(self):
        expected = control.timer_target(BASE_SCHEDULE, "cycle", "schedule_config", COMPOUND_VALUES["schedule_config"])
        self.prepare(target="cycle", field="schedule_config", value=COMPOUND_VALUES["schedule_config"],
                     observed=expected ^ 0x40000000)
        with self.assertRaisesRegex(read.ProbeError, "timer_readback_mismatch"):
            await self.client.control()
        self.assertEqual(self.result["setting_write_attempts"], 1)
        self.assertFalse(self.result["verified"])

    async def test_compound_failures_keep_single_property_single_write_budget(self):
        for field in COMPOUND_VALUES:
            count = 1 if field == "schedule_config" else 2
            for failure in range(1, count + 3):
                self.prepare(target="cycle", field=field, value=COMPOUND_VALUES[field], error_on=failure)
                with self.assertRaises(TimeoutError):
                    await self.client.control()
                self.assertEqual(len(self.records), failure)
                self.assertEqual(self.result["setting_write_attempts"], int(failure > count))
                self.assertEqual(self.result["relay_writes"], 0)
                for kind in ("guards", "set", "verify"):
                    with self.assertRaises(read.ProbeError):
                        await self.client.request_reserved(kind)

    def test_only_the_known_master_snapshot_is_authorized(self):
        for word in (0, BASE_MASTER ^ 2, BASE_MASTER ^ 1, 0xFFFFFFFF):
            with self.subTest(word=word), self.assertRaisesRegex(read.ProbeError, "timer_master_snapshot_not_authorized"):
                self.prepare(expected_master_word=word)

    async def test_observed_cycle_master_disarm_allows_relay_and_local_on(self):
        original = 0xAA0B4438
        self.prepare(target="cycle", field="master", value=False, initial=original,
                     expected_master_word=BASE_MASTER | 2,
                     overrides={(2, 1): True, (5, 6): BASE_MASTER | 2, (5, 7): BASE_LOCAL | 0xC0000000})
        await self.client.control()
        self.assertEqual(self.result["initial"], original)
        self.assertEqual(self.result["observed"], 0x2A0B4438)
        self.assertEqual(self.result["initial"] ^ self.result["observed"], 0x80000000)
        self.assertTrue(self.result["disarm"])
        self.assertEqual(self.result["setting_write_attempts"], 1)
        self.assertEqual(self.result["relay_writes"], 0)

    async def test_cycle_master_arm_requires_all_other_guards_off(self):
        self.prepare(target="cycle", field="master", value=True, initial=BASE_SCHEDULE)
        await self.client.control()
        self.assertEqual(self.result["observed"], BASE_SCHEDULE | 0x80000000)
        self.prepare(target="cycle", field="master", value=True, initial=BASE_SCHEDULE | 0x80000000)
        with self.assertRaisesRegex(read.ProbeError, "timer_cycle_master_must_be_off"):
            await self.client.control()
        self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_cycle_on_blocks_configuration_including_extra_target_read(self):
        for target, field, value in (("cycle", "on_seconds", 60), ("cycle", "start_state", False),
                                     ("cycle", "repeat", False), ("local", "start_min", 600),
                                     *(("cycle", field, value) for field, value in COMPOUND_VALUES.items())):
            self.prepare(target=target, field=field, value=value,
                         overrides={(5, 1): BASE_SCHEDULE | 0x80000000})
            with self.assertRaisesRegex(read.ProbeError, "timer_cycle_master_must_be_off"):
                await self.client.control()
            self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_cycle_snapshot_mismatch_blocks_even_disarm(self):
        self.prepare(target="cycle", field="master", value=False, expected_cycle_word=BASE_SCHEDULE)
        with self.assertRaisesRegex(read.ProbeError, "timer_cycle_snapshot_mismatch"):
            await self.client.control()
        self.assertFalse(self.result["maybe_executed"])

    async def test_two_guard_gets_share_single_write_budget_and_never_retry(self):
        for failure in (2, 3, 4):
            self.prepare(target="cycle", field="on_seconds", value=60, error_on=failure)
            with self.assertRaises(TimeoutError):
                await self.client.control()
            self.assertEqual(len(self.records), failure)
            self.assertEqual(self.result["setting_write_attempts"], int(failure >= 3))
            self.assertEqual(self.result["maybe_executed"], failure >= 3)
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved("set")

    async def test_stale_local_snapshot_blocks_cycle_configuration(self):
        self.prepare(target="cycle", field="repeat", value=False, expected_local_word=BASE_LOCAL ^ 1)
        with self.assertRaisesRegex(read.ProbeError, "timer_local_snapshot_mismatch"):
            await self.client.control()
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_no_change_still_guards_and_independently_reads_back(self):
        self.prepare(value=1393)
        await self.client.control()
        self.assertEqual([packet[4] for _, _, packet in self.records], [2, 2])
        self.assertEqual(self.result["setting_write_attempts"], 0)
        self.assertFalse(self.result["maybe_executed"])
        self.assertFalse(self.result["changed"])
        self.assertTrue(self.result["verified"])

    async def test_full_word_readback_rejects_a_preserved_bit_change(self):
        target = control.timer_target(BASE_LOCAL, "local", "start_min", 360)
        self.prepare(observed=target ^ 0x00400000)
        with self.assertRaisesRegex(read.ProbeError, "timer_readback_mismatch"):
            await self.client.control()
        self.assertEqual(len(self.records), 3)
        self.assertEqual(self.result["setting_write_attempts"], 1)
        self.assertFalse(self.result["verified"])

    async def test_all_request_failures_stop_without_retry_or_automatic_restore(self):
        for request in (1, 2, 3):
            with self.subTest(request=request):
                self.prepare(error_on=request)
                with self.assertRaises(TimeoutError):
                    await self.client.control()
                self.assertEqual(len(self.records), request)
                self.assertEqual(self.result["setting_write_attempts"], int(request > 1))
                self.assertEqual(self.result["maybe_executed"], request > 1)
                self.assertFalse(self.result["verified"])
                self.assertTrue(self.client.failed)
                self.assertEqual(self.client.expected_responses, {})
                self.assertIsNone(self.client.reserved_tx)
                for kind in ("guards", "set", "verify"):
                    with self.assertRaises(read.ProbeError):
                        await self.client.request_reserved(kind)
                with self.assertRaises(read.ProbeError):
                    await self.client.control()
                self.assertEqual(len(self.records), request)
                self.assertNotIn("must-not-log", json.dumps(self.result))

    async def test_invalid_read_type_or_failed_status_never_writes(self):
        for options in ({"response_typelens": {(5, 7): 0x6004}},
                        {"response_typelens": {(2, 1): 0x1001}},
                        {"response_statuses": {(5, 6): 1}}):
            self.prepare(**options)
            with self.assertRaises(read.ProbeError):
                await self.client.control()
            self.assertEqual(len(self.records), 1)
            self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_cancelled_write_is_not_retried(self):
        self.prepare()
        self.block_on = 2
        task = asyncio.create_task(self.client.control())
        await self.started.wait()
        with self.assertRaisesRegex(read.ProbeError, "timer_request_in_progress"):
            await self.client.request_reserved("verify")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.client.failed)
        self.assertEqual(len(self.records), 2)
        self.assertEqual(self.result["setting_write_attempts"], 1)

    async def test_completed_control_cannot_repeat_or_issue_more_requests(self):
        self.prepare()
        await self.client.control()
        with self.assertRaises(read.ProbeError):
            await self.client.control()
        for kind in ("guards", "set", "verify"):
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved(kind)
        self.assertEqual(len(self.records), 3)

    async def test_unauthenticated_or_pre_guard_requests_are_rejected(self):
        for kind in ("set", "verify"):
            self.prepare()
            self.client.started = True
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved(kind)
            self.assertEqual(self.records, [])
        self.prepare()
        self.client.authenticated = False
        with self.assertRaisesRegex(read.ProbeError, "protocol_phase_invalid"):
            await self.client.control()
        self.assertEqual(self.records, [])

    def test_unreserved_relay_settings_action_and_raw_helpers_are_blocked(self):
        self.prepare()
        for packet in (pack(9, 0, b"\1\2\1\0\1\0\1"), pack(9, 5),
                       self.client.get_pkt(9, [(2, 1)]), pack(1, 0xF0),
                       pack(9, 0, b"\1\5\4\0\4\x50\0\0\0\0")):
            with self.assertRaises(read.ProbeError):
                self.client.authorize_send(packet, 1)
        for method in (self.client.set_pkt, self.client.action_pkt):
            with self.assertRaises(read.ProbeError):
                method()

    async def test_actual_guarded_transport_accepts_only_reserved_target_packet(self):
        self.prepare()
        self.client.ch = type("Channel", (), {"_task": None})()
        self.client.started = self.client.guards_verified = True
        self.client.initial = BASE_LOCAL
        self.client.target = control.timer_target(BASE_LOCAL, "local", "start_min", 360)
        self.client.write_attempts = 1
        tid, counter = read.READ_TID, 1
        packet = control.uint32_set_packet(pack, tid, (5, 7), self.client.target)
        self.client.expected_responses[tid] = ("set", ())
        self.client.reserved_tx = (packet, counter)
        await self.client.send(packet, counter)
        self.assertEqual(len(self.raw.writes), 2)
        self.assertEqual(self.raw.writes[1][1][4:], packet)
        with self.assertRaises(read.ProbeError):
            await self.client.c.write_gatt_char(UPSTREAM.CMD, b"not-reserved")


if __name__ == "__main__":
    unittest.main()

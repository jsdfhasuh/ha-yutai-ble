"""Offline state-machine tests; stub fields are not device protocol mappings."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parents[2] / "custom_components" / "yutai_ble" / "protocol"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import read_probe as read


PLAN = ((2, 1), (4, 2), (4, 3), (4, 4),
        *((5, piid) for piid in range(1, 8)), (7, 1), (7, 2))
MODES = {"follow": 0, "inverse": 0x4000, "off": 0x8000, "always_on": 0xC000}


def validate_setting(field, value):
    if field == "indicator_mode" and type(value) is str and value in MODES:
        return (5, 4), 0xC000
    # This high-bit fake field exists only to exercise unsigned serialization.
    if field == "fake_high_bit" and type(value) is bool:
        return (5, 6), 0x80000000
    raise ValueError("test_setting_unsupported")


def target_word(field, value, initial):
    _, mask = validate_setting(field, value)
    bits = MODES[value] if field == "indicator_mode" else (mask if value else 0)
    return (initial & ~mask) | bits


def decode_settings(properties):
    words = {(item["siid"], item["piid"]): item["value"] for item in properties}
    mode = {bits: name for name, bits in MODES.items()}[words[(5, 4)] & 0xC000]
    return ({"indicator_mode": mode, "fake_high_bit": bool(words[(5, 6)] & 0x80000000)},
            {"indicator_mode": {"kind": "select"}, "fake_high_bit": {"kind": "switch"}})


CODEC = types.SimpleNamespace(READ_PROPERTIES=PLAN, validate_setting=validate_setting,
                              target_word=target_word, decode_settings=decode_settings)
spec = importlib.util.spec_from_file_location("_isolated_settings_control_test", HERE / "settings_control.py")
control = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"settings_codec": CODEC}):
    spec.loader.exec_module(control)


def pack(tid, opcode, body=b""):
    return (0x2000 | (5 + len(body))).to_bytes(2, "little") + tid.to_bytes(2, "little") + bytes((opcode,)) + body


class FakeBase:
    def __init__(self, device, key, log):
        self.ltmk, self.k, self.ch = key, None, None
        self.pending, self.raw, self.notify = {}, [], []

    pack = staticmethod(pack)

    def get_pkt(self, tid, properties):
        return pack(tid, 2, bytes((len(properties),)) + b"".join(
            bytes((siid,)) + piid.to_bytes(2, "little") for siid, piid in properties))

    def enc_frame(self, packet, counter):
        return counter.to_bytes(2, "little") + packet


UPSTREAM = types.SimpleNamespace(Client=FakeBase, CH_CTRL="CTRL", CMD="CMD", DAT="DAT", MCU="MCU")


class FakeRaw:
    is_connected = True

    def __init__(self):
        self.writes = []
        self.owner = None

    async def write_gatt_char(self, uuid, packet, response=False):
        self.writes.append((uuid, packet))
        if uuid == "CMD" and packet == b"\0\0\0\0\1\0":
            self.owner.command_ready.set()


BASE = {(2, 1): True, (4, 2): 7, (4, 3): 234, (4, 4): 29,
        (5, 1): 0x3FCB4438, (5, 2): 0x80000708, (5, 3): 1800,
        (5, 4): 0x00020420, (5, 5): 0x00200005, (5, 6): 0x4B03C004,
        (5, 7): 0x3FCB4438, (7, 1): True, (7, 2): True}


def get_response(tid, properties, words, overrides=None):
    body = bytes((len(properties),))
    for identity in properties:
        siid, piid = identity
        value = words[identity]
        typelen = 1 if type(value) is bool else 0x5004
        status = 0
        if overrides and identity in overrides:
            typelen, value, status = overrides[identity]
        body += bytes((siid,)) + piid.to_bytes(2, "little") + status.to_bytes(2, "little")
        if status == 0:
            size = typelen & 0xFFF
            body += typelen.to_bytes(2, "little") + int(value).to_bytes(size, "little")
    return pack(tid, 3, body)


def set_response(tid, property_id=(5, 4), status=0):
    return pack(tid, 1, bytes((1, property_id[0])) + property_id[1].to_bytes(2, "little")
                + status.to_bytes(2, "little"))


class ParserTests(unittest.TestCase):
    def test_unsigned_uint32_packet_is_fixed_to_setting_properties(self):
        packet = control.uint32_set_packet(pack, 9, (5, 4), 0xFFFFFFFF)
        self.assertEqual(read.parse_frame(packet), (9, 0, b"\1\5\4\0\4\x50\xff\xff\xff\xff"))
        for value in (True, False, -1, 0x100000000, 1.0, "1", None):
            with self.subTest(value=value), self.assertRaises(read.ProbeError):
                control.uint32_set_packet(pack, 9, (5, 4), value)
        for identity in ((2, 1), (5, 1), (5, 7), (5, True), [5, 4]):
            with self.subTest(identity=identity), self.assertRaises(read.ProbeError):
                control.uint32_set_packet(pack, 9, identity, 1)

    def test_set_response_rejects_tid_opcode_target_status_and_trailing_data(self):
        valid = set_response(9)
        self.assertEqual(control.parse_set_response(valid, 9, (5, 4))["status"], 0)
        for packet in (set_response(8), set_response(9, (5, 5)), set_response(9, status=1),
                       valid[:-1], valid + b"x", pack(9, 0, valid[5:])):
            with self.subTest(packet=packet.hex()), self.assertRaises(read.ProbeError):
                control.parse_set_response(packet, 9, (5, 4))

    def test_codec_cannot_authorize_relay_timers_invalid_mask_or_read_scope(self):
        for identity, mask in (((2, 1), 1), ((5, 1), 1), ((5, 7), 1),
                               ((5, 4), 0), ((5, 4), True), ((5, 4), 1 << 32)):
            with self.subTest(identity=identity, mask=mask), patch.object(
                    CODEC, "validate_setting", return_value=(identity, mask)), self.assertRaises(read.ProbeError):
                control.make_control_client(UPSTREAM, "indicator_mode", "inverse")
        for plan in (PLAN[:-1], (*PLAN, (4, 1)), (*PLAN[:-1], PLAN[0])):
            with self.subTest(plan=plan), patch.object(CODEC, "READ_PROPERTIES", plan), self.assertRaises(read.ProbeError):
                control.make_control_client(UPSTREAM, "indicator_mode", "inverse")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, field="indicator_mode", value="inverse", initial=None,
                *, fail_at=None, observed=None, overrides=None, set_status=0,
                observed_relay=None, controller=control):
        self.words = {**BASE, **(initial or {})}
        self.result, self.raw = {}, FakeRaw()
        self.client = controller.make_control_client(UPSTREAM, field, value)(
            object(), b"x" * 32, self.raw, self.result)
        self.raw.owner = self.client
        self.client.authenticated = self.client.f0_verified = True
        self.client.sent = [(pack(1, 0xF0), 0)]
        self.records = []
        self.block_on = None
        self.started, self.release = asyncio.Event(), asyncio.Event()
        property_id, _mask = controller.codec.validate_setting(field, value)

        async def exchange(packet, tid, counter):
            self.assertGreater(self.client.next_tid, tid)
            self.assertGreater(self.client.next_counter, counter)
            self.client.authorize_send(packet, counter)
            self.client.sent.append((packet, counter))
            self.records.append((tid, counter, packet))
            index = len(self.records)
            if index == self.block_on:
                self.started.set()
                await self.release.wait()
            if index == fail_at:
                raise TimeoutError("must-not-log")
            if packet[4] == 0:
                self.words[property_id] = int.from_bytes(packet[-4:], "little")
                response = set_response(tid, property_id, set_status)
            else:
                batch = self.client.expected_responses[tid][1]
                words = self.words.copy()
                if self.client.stage == "verify":
                    words[(4, 2)] = 9
                    if observed is not None:
                        words[property_id] = observed
                    if observed_relay is not None:
                        words[(2, 1)] = observed_relay
                response = get_response(tid, batch, words, overrides)
            return self.client.parse_pending_response(response)

        self.client.exchange = exchange

    async def test_full_snapshots_one_exact_set_and_only_post_properties_returned(self):
        self.prepare()
        await self.client.control()
        self.assertEqual([packet[4] for _, _, packet in self.records], [2] * 4 + [0] + [2] * 4)
        self.assertEqual([counter for _, counter, _ in self.records], list(range(1, 10)))
        self.assertEqual([tid for tid, _, _ in self.records], list(range(read.READ_TID, read.READ_TID + 9)))
        self.assertEqual(self.result["initial"], 0x20420)
        self.assertEqual(self.result["target"], 0x24420)
        self.assertEqual(self.result["observed"], 0x24420)
        self.assertEqual(self.result["setting_write_attempts"], 1)
        self.assertEqual(self.result["relay_writes"], 0)
        self.assertTrue(self.result["maybe_executed"])
        self.assertTrue(self.result["verified"])
        self.assertTrue(self.result["changed"])
        self.assertTrue(self.result["initial_snapshot_complete"])
        self.assertTrue(self.result["verification_snapshot_complete"])
        self.assertIs(self.result["initial_relay_state"], True)
        self.assertIs(self.result["observed_relay_state"], True)
        self.assertIs(self.result["relay_state_unchanged"], True)
        self.assertEqual(self.result["status"], "setting_verified")
        self.assertEqual(len(self.result["properties"]), 13)
        self.assertEqual(next(item["value"] for item in self.result["properties"]
                              if (item["siid"], item["piid"]) == (4, 2)), 9)
        self.assertEqual(self.result["settings"]["indicator_mode"], "inverse")
        self.assertEqual(self.result["initial"] & 0x20, self.result["observed"] & 0x20)

    async def test_already_target_skips_set_but_reads_full_independent_snapshot(self):
        self.prepare(value="follow")
        await self.client.control()
        self.assertEqual([packet[4] for _, _, packet in self.records], [2] * 8)
        self.assertFalse(self.result["changed"])
        self.assertFalse(self.result["maybe_executed"])
        self.assertTrue(self.result["verified"])
        self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_real_codec_all_four_indicator_modes_integrate_with_controller(self):
        import settings_codec as real_codec

        real_spec = importlib.util.spec_from_file_location(
            "_real_codec_settings_control_test", HERE / "settings_control.py")
        real_control = importlib.util.module_from_spec(real_spec)
        with patch.dict(sys.modules, {"settings_codec": real_codec}):
            real_spec.loader.exec_module(real_control)
        for mode, bits in MODES.items():
            with self.subTest(mode=mode):
                self.prepare(value=mode, controller=real_control)
                await self.client.control()
                self.assertEqual(self.result["observed"], 0x20420 | bits)
                self.assertEqual(self.result["settings"]["indicator_mode"], mode)
                self.assertEqual(self.result["status"], "setting_verified")
                self.assertEqual(self.result["setting_write_attempts"], int(mode != "follow"))
                self.assertEqual(set(self.result["capabilities"]["indicator_mode"]["options"]), set(MODES))

    async def test_high_bit_values_stay_unsigned_without_touching_other_bits(self):
        self.prepare(field="fake_high_bit", value=True)
        await self.client.control()
        self.assertEqual(self.result["target"], 0xCB03C004)
        self.assertGreater(self.result["observed"], 0x7FFFFFFF)
        self.assertEqual(self.result["target"] ^ self.result["initial"], 0x80000000)

    async def test_real_codec_all_seven_fields_integrate_without_relay_changes(self):
        import settings_codec as real_codec

        real_spec = importlib.util.spec_from_file_location(
            "_real_fields_settings_control_test", HERE / "settings_control.py")
        real_control = importlib.util.module_from_spec(real_spec)
        with patch.dict(sys.modules, {"settings_codec": real_codec}):
            real_spec.loader.exec_module(real_control)
        cases = (
            ("indicator_mode", "inverse", (5, 4), 0x00024420),
            ("power_on_state", "memory", (5, 4), 0x00022420),
            ("indicator_dnd_enabled", True, (5, 6), 0xCB03C004),
            ("double_click_enabled", True, (5, 4), 0x00020430),
            ("indicator_dnd_start", "21:17", (5, 6), 0x4FD3C004),
            ("indicator_dnd_end", "23:43", (5, 6), 0x4B0B1E04),
            ("double_click_seconds", 86340, (5, 5), 0xBB400005),
        )
        for field, value, property_id, expected in cases:
            with self.subTest(field=field):
                self.prepare(field=field, value=value, controller=real_control)
                await self.client.control()
                self.assertEqual(self.result["observed"], expected)
                self.assertEqual(self.result["settings"][field], value)
                self.assertEqual(self.result["status"], "setting_verified")
                self.assertTrue(self.result["verified"])
                self.assertIs(self.result["relay_state_unchanged"], True)
                self.assertEqual(self.result["setting_write_attempts"], 1)
                self.assertEqual(self.result["relay_writes"], 0)
                self.assertEqual([packet[4] for _, _, packet in self.records], [2] * 4 + [0] + [2] * 4)
                self.assertEqual(self.words, {**BASE, property_id: expected})

    async def test_changed_relay_fails_without_retry_restore_or_success(self):
        for initial_relay in (False, True):
            for value in ("inverse", "follow"):
                with self.subTest(initial_relay=initial_relay, value=value):
                    self.prepare(value=value, initial={(2, 1): initial_relay},
                                 observed_relay=not initial_relay)
                    with patch.object(CODEC, "decode_settings") as decoder:
                        with self.assertRaisesRegex(read.ProbeError, "setting_relay_state_changed"):
                            await self.client.control()
                        decoder.assert_not_called()
                    expected_writes = int(value != "follow")
                    self.assertEqual(len(self.records), 8 + expected_writes)
                    self.assertEqual(self.result["setting_write_attempts"], expected_writes)
                    self.assertEqual(self.result["relay_writes"], 0)
                    self.assertIs(self.result["initial_relay_state"], initial_relay)
                    self.assertIs(self.result["observed_relay_state"], not initial_relay)
                    self.assertIs(self.result["relay_state_unchanged"], False)
                    self.assertEqual(self.result["relay_state_change_cause"], "unknown")
                    self.assertTrue(self.result["verification_snapshot_complete"])
                    self.assertFalse(self.result["verified"])
                    self.assertNotEqual(self.result.get("status"), "setting_verified")
                    self.assertTrue(self.client.failed)
                    for kind in ("get", "set"):
                        with self.assertRaises(read.ProbeError):
                            await self.client.request_reserved(kind)
                    with self.assertRaises(read.ProbeError):
                        await self.client.control()
                    self.assertEqual(len(self.records), 8 + expected_writes)

    async def test_failures_never_retry_restore_or_return_initial_as_post(self):
        for fail_at in (1, 4, 5, 6, 9):
            with self.subTest(fail_at=fail_at):
                self.prepare(fail_at=fail_at)
                with self.assertRaises(TimeoutError):
                    await self.client.control()
                self.assertEqual(len(self.records), fail_at)
                self.assertEqual(self.result["setting_write_attempts"], int(fail_at >= 5))
                self.assertEqual(self.result["maybe_executed"], fail_at >= 5)
                self.assertFalse(self.result["verified"])
                self.assertEqual(self.result["properties"], [])
                self.assertTrue(self.client.failed)
                self.assertIsNone(self.client.reserved_tx)
                self.assertEqual(self.client.expected_responses, {})
                self.assertNotIn("must-not-log", str(self.result))
                for kind in ("get", "set"):
                    with self.assertRaises(read.ProbeError):
                        await self.client.request_reserved(kind)
                self.assertEqual(len(self.records), fail_at)

    async def test_full_word_mismatch_and_failed_ack_do_not_cause_second_write(self):
        self.prepare(observed=0x24421)
        with self.assertRaisesRegex(read.ProbeError, "setting_readback_mismatch"):
            await self.client.control()
        self.assertEqual(len(self.records), 9)
        self.assertFalse(self.result["verified"])
        self.assertEqual(self.result["setting_write_attempts"], 1)
        self.prepare(set_status=1)
        with self.assertRaisesRegex(read.ProbeError, "setting_set_response_invalid"):
            await self.client.control()
        self.assertEqual(len(self.records), 5)

    async def test_codec_non_target_mutation_and_invalid_uint32_fail_before_set(self):
        for wrong in (0x24400, -1, 1 << 32, True, 1.0):
            with self.subTest(wrong=wrong):
                self.prepare()
                with patch.object(CODEC, "target_word", return_value=wrong), self.assertRaises(read.ProbeError):
                    await self.client.control()
                self.assertEqual(len(self.records), 4)
                self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_bad_snapshot_status_or_type_fails_closed_before_set(self):
        for override in ((0x5004, 0, 1), (1, 1, 0), (0x6004, 1, 0)):
            with self.subTest(override=override):
                self.prepare(overrides={(5, 4): override})
                with self.assertRaises(read.ProbeError):
                    await self.client.control()
                self.assertEqual(len(self.records), 2)
                self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_decoded_requested_field_must_match_with_exact_type(self):
        for wrong in ("follow", True, None):
            with self.subTest(wrong=wrong):
                self.prepare()
                with patch.object(CODEC, "decode_settings", return_value=(
                        {"indicator_mode": wrong}, {"indicator_mode": {"kind": "select"}})), self.assertRaisesRegex(
                            read.ProbeError, "setting_decoded_value_mismatch"):
                    await self.client.control()
                self.assertEqual(len(self.records), 9)
                self.assertFalse(self.result["verified"])

    async def test_cancelled_set_is_not_retried_and_concurrent_request_is_blocked(self):
        self.prepare()
        self.block_on = 5
        task = asyncio.create_task(self.client.control())
        await self.started.wait()
        with self.assertRaisesRegex(read.ProbeError, "setting_request_in_progress"):
            await self.client.request_reserved("set")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.records), 5)
        self.assertEqual(self.client.next_counter, 6)
        self.assertTrue(self.client.failed)
        self.assertTrue(self.result["maybe_executed"])
        self.assertEqual(self.client.expected_responses, {})
        with self.assertRaises(read.ProbeError):
            await self.client.request_reserved("set")
        self.assertEqual(len(self.records), 5)

    async def test_successful_client_cannot_be_reused_or_issue_more_requests(self):
        self.prepare()
        await self.client.control()
        with self.assertRaises(read.ProbeError):
            await self.client.control()
        for kind in ("get", "set"):
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved(kind)
        self.assertEqual(len(self.records), 9)

    async def test_authentication_and_f0_are_required_before_any_property_request(self):
        for attribute in ("authenticated", "f0_verified"):
            with self.subTest(attribute=attribute):
                self.prepare()
                setattr(self.client, attribute, False)
                with self.assertRaises(read.ProbeError):
                    await self.client.control()
                self.assertEqual(self.records, [])

    async def test_actual_transport_only_accepts_reserved_setting_not_other_writes(self):
        self.prepare()
        self.client.ch = types.SimpleNamespace(_task=None)
        self.client.initial, self.client.target = 0x20420, 0x24420
        self.client.write_attempts = 1
        tid, counter = read.READ_TID, 5
        self.client.expected_responses[tid] = ("set", (5, 4))
        for wrong in (pack(tid, 0, b"\1\2\1\0\1\0\1"), pack(tid, 5),
                      control.uint32_set_packet(pack, tid, (5, 5), 0x24420),
                      control.uint32_set_packet(pack, tid, (5, 4), 0x24421)):
            self.client.reserved_tx = (wrong, counter)
            with self.assertRaises(read.ProbeError):
                await self.client.send(wrong, counter)
        self.assertEqual(self.raw.writes, [])
        packet = control.uint32_set_packet(pack, tid, (5, 4), 0x24420)
        self.client.reserved_tx = (packet, counter)
        await self.client.send(packet, counter)
        self.assertEqual(len(self.raw.writes), 2)
        self.assertEqual(self.raw.writes[1][1][4:], packet)
        with self.assertRaises(read.ProbeError):
            await self.client.c.write_gatt_char(UPSTREAM.CMD, b"not-reserved")
        for method in (self.client.set_pkt, self.client.action_pkt):
            with self.assertRaises(read.ProbeError):
                method()


if __name__ == "__main__":
    unittest.main()

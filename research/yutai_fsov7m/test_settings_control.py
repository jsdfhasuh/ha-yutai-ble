"""Offline observed-setting tests; no Bluetooth, SSH, ADB, or cloud calls."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest

import read_probe as read
import settings_control as control
from test_read_probe import FakeRaw, UPSTREAM, pack


def get_response(tid, value, typelen=0x5004, status=0, siid=5, piid=4):
    body = bytes([1, siid]) + piid.to_bytes(2, "little") + status.to_bytes(2, "little")
    if not status:
        body += typelen.to_bytes(2, "little") + value.to_bytes(4, "little")
    return pack(tid, 3, body)


def set_response(tid, status=0, siid=5, piid=4):
    return pack(tid, 1, bytes([1, siid]) + piid.to_bytes(2, "little") + status.to_bytes(2, "little"))


class ParserTests(unittest.TestCase):
    def test_mode_target_preserves_all_other_bits(self):
        for value in (0, 0x20400, 0x28400, 0xFFFF3FFF, 0xFFFFBFFF):
            for mode in ("follow", "off"):
                target = control.indicator_target(value, mode)
                self.assertEqual((target ^ value) & ~0x8000, 0)
                self.assertEqual(target & 0xC000, control.MODES[mode])
        for value in (0x4000, 0xC000, -1, 0x100000000, True, 1.0):
            with self.subTest(value=value), self.assertRaises(read.ProbeError):
                control.indicator_target(value, "follow")
        for mode in ("on", "inverse", "FOLLOW", None, 0, []):
            with self.subTest(mode=mode), self.assertRaises(read.ProbeError):
                control.indicator_target(0, mode)

    def test_exact_target_input_and_explicit_mode_required(self):
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": "ab" * 32,
                "indicator_mode": "follow"}
        target, options = control.validated_input(data)
        self.assertEqual(target.key, b"\xab" * 32)
        self.assertEqual(options, {"indicator_mode": "follow"})
        for changed in ({k: v for k, v in data.items() if k != "indicator_mode"},
                        {**data, "operation": "cycle"}, {**data, "model": "wiing.plug.v7icm"},
                        {**data, "address": "00:00:00:00:00:00"}):
            with self.assertRaises(read.ProbeError):
                control.validated_input(changed)

    def test_power_target_all_observed_transitions_preserve_other_bits(self):
        for base in (0x20400, 0x28400, 0xFFFFCFFF):
            base &= ~control.POWER_FIELD
            for current in control.POWER_STATES.values():
                for state, target_bits in control.POWER_STATES.items():
                    initial = base | current
                    target = control.power_target(initial, state)
                    self.assertEqual(target & control.POWER_FIELD, target_bits)
                    self.assertEqual((initial ^ target) & ~control.POWER_FIELD, 0)
        for value in (0x23400, -1, 0x100000000, True, 0.0):
            with self.subTest(value=value), self.assertRaises(read.ProbeError):
                control.power_target(value, "off")
        for state in ("follow", "ON", None, 0, []):
            with self.subTest(state=state), self.assertRaises(read.ProbeError):
                control.power_target(0, state)

    def test_power_input_and_exactly_one_setting(self):
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": "ab" * 32,
                "power_on_state": "memory"}
        target, options = control.validated_input(data)
        self.assertEqual(target.key, b"\xab" * 32)
        self.assertEqual(options, {"power_on_state": "memory"})
        for changed in ({**data, "indicator_mode": "off"}, {**data, "power_on_state": "bad"},
                        {**data, "model": "wiing.plug.v7icm"}, {**data, "extra": 1}):
            with self.assertRaises(read.ProbeError):
                control.validated_input(changed)
        for options in ({}, {"indicator_mode": "off", "power_on_state": "memory"}):
            with self.assertRaises(read.ProbeError):
                control.make_settings_client(UPSTREAM, **options)

    def test_exact_uint32_packet_and_invalid_values(self):
        packet = control.uint32_set_packet(pack, 9, 0x28400)
        self.assertEqual(read.parse_frame(packet), (9, 0, b"\1\5\4\0\4\x50\0\x84\2\0"))
        for value in (-1, 0x100000000, True, 0.0):
            with self.assertRaises(read.ProbeError):
                control.uint32_set_packet(pack, 9, value)

    def test_get_response_strict_target_type_status_and_size(self):
        valid = get_response(9, 0x20400)
        self.assertEqual(control.parse_get_response(valid, 9), 0x20400)
        cases = (get_response(8, 0), get_response(9, 0, status=1),
                 get_response(9, 0, siid=2, piid=1), get_response(9, 0, typelen=0x6004),
                 valid[:-1], valid + b"x", pack(9, 1, valid[5:]))
        for packet in cases:
            with self.subTest(packet=packet.hex()), self.assertRaises(read.ProbeError):
                control.parse_get_response(packet, 9)

    def test_set_response_strict_tid_target_status_and_size(self):
        valid = set_response(9)
        self.assertEqual(control.parse_set_response(valid, 9), {"siid": 5, "piid": 4, "status": 0})
        cases = (set_response(8), set_response(9, status=1), set_response(9, siid=2, piid=1),
                 valid[:-1], valid + b"x", pack(9, 0, valid[5:]))
        for packet in cases:
            with self.subTest(packet=packet.hex()), self.assertRaises(read.ProbeError):
                control.parse_set_response(packet, 9)

    def test_invalid_cli_input_has_no_secret_or_traceback(self):
        secret = "ab" * 32
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": secret,
                "indicator_mode": "on"}
        process = subprocess.run([sys.executable, str(Path(control.__file__))], input=json.dumps(data),
                                 text=True, capture_output=True, timeout=5)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stderr, "")
        self.assertNotIn(secret, process.stdout)
        self.assertEqual(json.loads(process.stdout)["error_code"], "indicator_mode_not_supported")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, initial=0x20400, mode="off", error_on=None, observed=None, power_on_state=None):
        self.result = {}
        self.raw = FakeRaw()
        options = {"power_on_state": power_on_state} if power_on_state is not None else {"indicator_mode": mode}
        self.client = control.make_settings_client(UPSTREAM, **options)(object(), b"x" * 32, self.raw, self.result)
        self.raw.owner = self.client
        self.client.authenticated = self.client.f0_verified = True
        self.client.sent = [(pack(1, 0xF0), 0)]
        self.records = []
        self.current = initial
        self.started = asyncio.Event()
        self.release = asyncio.Event()
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
                self.current = int.from_bytes(packet[-4:], "little")
                response = set_response(tid)
            else:
                value = observed if observed is not None and len(self.records) > 1 else self.current
                response = get_response(tid, value)
            return self.client.parse_pending_response(response)

        self.client.exchange = exchange

    async def test_change_reads_sets_once_and_independently_reads_back(self):
        self.prepare()
        await self.client.control()
        self.assertEqual([packet[4] for _, _, packet in self.records], [2, 0, 2])
        self.assertEqual([counter for _, counter, _ in self.records], [1, 2, 3])
        self.assertEqual([tid for tid, _, _ in self.records], [read.READ_TID + i for i in range(3)])
        self.assertEqual(self.result["initial"], 0x20400)
        self.assertEqual(self.result["target"], 0x28400)
        self.assertEqual(self.result["observed"], 0x28400)
        self.assertTrue(self.result["changed"])
        self.assertTrue(self.result["verified"])
        self.assertEqual(self.result["setting_write_attempts"], 1)
        self.assertEqual(self.result["relay_writes"], 0)
        with self.assertRaises(read.ProbeError):
            await self.client.control()

    async def test_follow_restores_only_observed_bit(self):
        self.prepare(initial=0xF246A333, mode="follow")
        await self.client.control()
        self.assertEqual(self.result["target"], 0xF2462333)
        self.assertEqual(self.result["initial"] ^ self.result["observed"], 0x8000)

    async def test_no_change_skips_set_but_reads_back_independently(self):
        self.prepare(mode="follow")
        await self.client.control()
        self.assertEqual([packet[4] for _, _, packet in self.records], [2, 2])
        self.assertEqual(self.result["setting_write_attempts"], 0)
        self.assertFalse(self.result["maybe_executed"])
        self.assertFalse(self.result["changed"])
        self.assertTrue(self.result["verified"])

    async def test_power_all_observed_transitions_only_change_power_mask(self):
        for initial_bits in control.POWER_STATES.values():
            for state, target_bits in control.POWER_STATES.items():
                with self.subTest(initial=initial_bits, target=state):
                    initial = 0x28400 | initial_bits
                    self.prepare(initial=initial, power_on_state=state)
                    await self.client.control()
                    self.assertEqual(self.result["target"], 0x28400 | target_bits)
                    self.assertEqual(self.result["observed"], self.result["target"])
                    self.assertEqual((self.result["initial"] ^ self.result["observed"]) & ~0x3000, 0)
                    self.assertEqual(self.result["setting_write_attempts"], int(initial_bits != target_bits))
                    self.assertEqual(self.result["status"], "power_on_verified")
                    self.assertEqual(self.result["relay_writes"], 0)
                    self.assertEqual(self.result["power_on_state"], state)
                    self.assertNotIn("indicator_mode", self.result)

    async def test_unknown_power_field_never_writes(self):
        self.prepare(initial=0x23400, power_on_state="memory")
        with self.assertRaisesRegex(read.ProbeError, "power_current_state_not_observed"):
            await self.client.control()
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_power_uncertain_set_and_mismatched_readback_do_not_retry(self):
        self.prepare(power_on_state="on", error_on=2)
        with self.assertRaises(TimeoutError):
            await self.client.control()
        self.assertEqual(len(self.records), 2)
        self.assertTrue(self.result["maybe_executed"])
        self.assertFalse(self.result["verified"])
        self.prepare(initial=0x22400, power_on_state="off", observed=0x28400)
        with self.assertRaisesRegex(read.ProbeError, "indicator_readback_mismatch"):
            await self.client.control()
        self.assertEqual(len(self.records), 3)
        self.assertFalse(self.result["verified"])

    async def test_unobserved_current_mode_is_rejected_before_any_write(self):
        self.prepare(initial=0x24400)
        with self.assertRaises(read.ProbeError):
            await self.client.control()
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.result["setting_write_attempts"], 0)

    async def test_uncertain_set_failure_is_not_retried(self):
        self.prepare(error_on=2)
        with self.assertRaises(TimeoutError):
            await self.client.control()
        self.assertEqual(len(self.records), 2)
        self.assertEqual(self.client.next_counter, 3)
        self.assertEqual(self.result["setting_write_attempts"], 1)
        self.assertTrue(self.result["maybe_executed"])
        self.assertFalse(self.result["verified"])
        self.assertEqual(self.client.expected_responses, {})
        self.assertIsNone(self.client.reserved_tx)
        for request in ("get", "set"):
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved(request)
        with self.assertRaises(read.ProbeError):
            await self.client.control()
        self.assertEqual(len(self.records), 2)
        self.assertNotIn("must-not-log", json.dumps(self.result))

    async def test_readback_requires_entire_preserved_word(self):
        self.prepare(observed=0x28401)
        with self.assertRaisesRegex(read.ProbeError, "indicator_readback_mismatch"):
            await self.client.control()
        self.assertFalse(self.result["verified"])
        self.assertEqual(self.result["observed"], 0x28401)
        self.assertEqual(len(self.records), 3)

    async def test_cancel_and_concurrent_request_are_not_retried(self):
        self.prepare()
        self.block_on = 2
        task = asyncio.create_task(self.client.control())
        await self.started.wait()
        with self.assertRaises(read.ProbeError):
            await self.client.request_reserved("get")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.client.failed)
        self.assertEqual(len(self.records), 2)
        self.assertEqual(self.result["setting_write_attempts"], 1)

    def test_unreserved_wrong_target_action_and_raw_set_helpers_rejected(self):
        self.prepare()
        for packet in (pack(9, 0, b"\1\2\1\0\1\0\1"), pack(9, 5),
                       self.client.get_pkt(9, [(2, 1)]), pack(1, 0xF0)):
            with self.assertRaises(read.ProbeError):
                self.client.authorize_send(packet, 1)
        for method in (self.client.set_pkt, self.client.action_pkt):
            with self.assertRaises(read.ProbeError):
                method()

    async def test_actual_guarded_transport_accepts_only_reserved_indicator_packet(self):
        self.prepare()
        self.client.ch = type("Channel", (), {"_task": None})()
        self.client.started = True
        self.client.initial = 0x20400
        self.client.target = 0x28400
        self.client.write_attempts = 1
        tid, counter = read.READ_TID, 1
        packet = control.uint32_set_packet(pack, tid, self.client.target)
        self.client.expected_responses[tid] = "set"
        self.client.reserved_tx = (packet, counter)
        await self.client.send(packet, counter)
        self.assertEqual(len(self.raw.writes), 2)
        self.assertEqual(self.raw.writes[1][1][4:], packet)
        with self.assertRaises(read.ProbeError):
            await self.client.c.write_gatt_char(UPSTREAM.CMD, b"not-reserved")


if __name__ == "__main__":
    unittest.main()

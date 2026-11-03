"""Synthetic wire tests only; no commissioned fsov8m relay action is performed."""

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parents[2] / "custom_components" / "yutai_ble" / "protocol"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import read_probe as read
import relay_control as relay
from test_settings_control import BASE, PLAN, UPSTREAM, FakeRaw, get_response, pack


def set_response(tid, *, property_id=(2, 1), status=0):
    return pack(tid, 1, bytes((1, property_id[0])) + property_id[1].to_bytes(2, "little")
                + status.to_bytes(2, "little"))


class ParserTests(unittest.TestCase):
    def test_only_strict_bool_packet_for_fixed_output_property(self):
        for value in (False, True):
            self.assertEqual(read.parse_frame(relay.bool_set_packet(pack, 9, value)),
                             (9, 0, b"\1\2\1\0\1\0" + bytes((int(value),))))
        for value in (0, 1, -1, 1.0, "on", None, [], {}):
            with self.subTest(value=value), self.assertRaises(read.ProbeError):
                relay.bool_set_packet(pack, 9, value)
            with self.subTest(value=value), self.assertRaises(read.ProbeError):
                relay.make_relay_client(UPSTREAM, value)

    def test_set_ack_requires_exact_tid_opcode_target_status_and_length(self):
        valid = set_response(9)
        self.assertEqual(relay.parse_set_response(valid, 9), {"siid": 2, "piid": 1, "status": 0})
        for packet in (set_response(8), set_response(9, property_id=(7, 1)),
                       set_response(9, property_id=(5, 4)), set_response(9, status=1),
                       valid[:-1], valid + b"x", pack(9, 0, valid[5:])):
            with self.subTest(packet=packet.hex()), self.assertRaises(read.ProbeError):
                relay.parse_set_response(packet, 9)

    def test_complete_read_scope_cannot_be_changed(self):
        for plan in (PLAN[:-1], (*PLAN, (4, 1)), (*PLAN[:-1], PLAN[0])):
            with self.subTest(plan=plan), patch.object(relay.codec, "READ_PROPERTIES", plan):
                with self.assertRaises(read.ProbeError):
                    relay.make_relay_client(UPSTREAM, False)

    def test_snapshot_proof_rejects_wrong_status_type_raw_and_scope(self):
        items = relay.scan.parse_read_response(get_response(9, PLAN[:4], BASE), 9, PLAN[:4])
        self.assertIs(relay.snapshot_values(items, PLAN[:4])[(2, 1)], True)
        for mutation in (
            lambda item: item.update(value=1),
            lambda item: item.update(typelen=0x1001),
            lambda item: item.update(raw_hex="00"),
            lambda item: item.update(status=False),
            lambda item: item.update(piid=2),
        ):
            changed = [dict(item) for item in items]
            mutation(changed[0])
            with self.assertRaises(read.ProbeError):
                relay.snapshot_values(changed, PLAN[:4])
        with self.assertRaises(read.ProbeError):
            relay.snapshot_values(items[:-1], PLAN[:4])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, value=False, *, initial=True, fail_at=None, observed=None,
                changed_settings=None, initial_overrides=None, post_overrides=None, set_status=0):
        self.words = {**BASE, (2, 1): initial}
        self.result, self.raw = {}, FakeRaw()
        self.client = relay.make_relay_client(UPSTREAM, value)(
            object(), b"x" * 32, self.raw, self.result)
        self.raw.owner = self.client
        self.client.authenticated = self.client.f0_verified = True
        self.client.sent = [(pack(1, 0xF0), 0)]
        self.records = []
        self.block_on = None
        self.started, self.release = asyncio.Event(), asyncio.Event()

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
                self.words[(2, 1)] = bool(packet[-1])
                response = set_response(tid, status=set_status)
            else:
                batch = self.client.expected_responses[tid][1]
                words = self.words.copy()
                overrides = initial_overrides
                if self.client.stage == "verify":
                    words[(4, 2)] = 9
                    words[(7, 1)] = value
                    overrides = post_overrides
                    if observed is not None:
                        words[(2, 1)] = observed
                    words.update(changed_settings or {})
                response = get_response(tid, batch, words, overrides)
            return self.client.parse_pending_response(response)

        self.client.exchange = exchange

    async def assert_no_more_requests(self, count):
        for kind in ("get", "set"):
            with self.assertRaises(read.ProbeError):
                await self.client.request_reserved(kind)
        with self.assertRaises(read.ProbeError):
            await self.client.control()
        self.assertEqual(len(self.records), count)

    async def test_both_directions_have_one_bool_set_and_two_complete_snapshots(self):
        for value in (False, True):
            with self.subTest(value=value):
                self.prepare(value, initial=not value)
                await self.client.control()
                self.assertEqual([packet[4] for _, _, packet in self.records], [2] * 4 + [0] + [2] * 4)
                self.assertEqual([counter for _, counter, _ in self.records], list(range(1, 10)))
                self.assertEqual([tid for tid, _, _ in self.records], list(range(read.READ_TID, read.READ_TID + 9)))
                self.assertEqual(self.records[4][2], relay.bool_set_packet(pack, read.READ_TID + 4, value))
                self.assertIs(self.result["initial_relay_state"], not value)
                self.assertIs(self.result["observed_relay_state"], value)
                self.assertIs(self.result["requested_value"], value)
                self.assertEqual(self.result["relay_writes"], 1)
                self.assertEqual(self.result["relay_write_attempts"], 1)
                self.assertTrue(self.result["maybe_executed"])
                self.assertTrue(self.result["initial_snapshot_complete"])
                self.assertTrue(self.result["verification_snapshot_complete"])
                self.assertTrue(self.result["settings_unchanged"])
                self.assertTrue(self.result["verified"])
                self.assertTrue(self.result["changed"])
                self.assertEqual(self.result["status"], "relay_verified")
                self.assertEqual(len(self.result["properties"]), 13)
                by_id = {(item["siid"], item["piid"]): item["value"] for item in self.result["properties"]}
                self.assertIs(by_id[(2, 1)], value)
                self.assertEqual(by_id[(4, 2)], 9)
                self.assertEqual({pair: by_id[pair] for pair in relay.SETTING_PROPERTIES},
                                 {pair: BASE[pair] for pair in relay.SETTING_PROPERTIES})
                self.assertEqual(self.result["settings"]["indicator_mode"], "follow")
                self.assertEqual(len(self.result["capabilities"]), 7)
                await self.assert_no_more_requests(9)

    async def test_already_target_is_independently_read_without_any_set(self):
        for value in (False, True):
            with self.subTest(value=value):
                self.prepare(value, initial=value)
                await self.client.control()
                self.assertEqual([packet[4] for _, _, packet in self.records], [2] * 8)
                self.assertEqual(self.result["relay_writes"], 0)
                self.assertEqual(self.result["relay_write_attempts"], 0)
                self.assertFalse(self.result["maybe_executed"])
                self.assertFalse(self.result["changed"])
                self.assertTrue(self.result["verified"])
                self.assertTrue(self.result["settings_unchanged"])
                await self.assert_no_more_requests(8)

    async def test_every_settings_word_must_remain_identical_without_restore(self):
        for identity in relay.SETTING_PROPERTIES:
            with self.subTest(identity=identity):
                self.prepare(changed_settings={identity: BASE[identity] ^ 1})
                with self.assertRaisesRegex(read.ProbeError, "relay_settings_changed"):
                    await self.client.control()
                self.assertFalse(self.result["settings_unchanged"])
                self.assertFalse(self.result["verified"])
                self.assertEqual(self.result["relay_write_attempts"], 1)
                self.assertTrue(self.result["maybe_executed"])
                await self.assert_no_more_requests(9)

    async def test_noop_still_rejects_concurrent_settings_changes(self):
        self.prepare(False, initial=False, changed_settings={(5, 5): BASE[(5, 5)] ^ 1})
        with self.assertRaisesRegex(read.ProbeError, "relay_settings_changed"):
            await self.client.control()
        self.assertEqual(self.result["relay_write_attempts"], 0)
        self.assertFalse(self.result["maybe_executed"])
        await self.assert_no_more_requests(8)

    async def test_wrong_readback_does_not_repeat_or_restore(self):
        for value in (False, True):
            with self.subTest(value=value):
                self.prepare(value, initial=not value, observed=not value)
                with self.assertRaisesRegex(read.ProbeError, "relay_readback_mismatch"):
                    await self.client.control()
                self.assertFalse(self.result["verified"])
                self.assertEqual(self.result["relay_write_attempts"], 1)
                await self.assert_no_more_requests(9)

    async def test_all_failure_stages_fail_closed_without_retries(self):
        for fail_at in (1, 4, 5, 6, 9):
            with self.subTest(fail_at=fail_at):
                self.prepare(fail_at=fail_at)
                with self.assertRaises(TimeoutError):
                    await self.client.control()
                self.assertEqual(self.result["relay_write_attempts"], int(fail_at >= 5))
                self.assertEqual(self.result["relay_writes"], int(fail_at >= 5))
                self.assertIs(self.result["maybe_executed"], fail_at >= 5)
                self.assertFalse(self.result["verified"])
                self.assertEqual(self.result["properties"], [])
                self.assertIsNone(self.client.reserved_tx)
                self.assertEqual(self.client.expected_responses, {})
                self.assertNotIn("must-not-log", str(self.result))
                await self.assert_no_more_requests(fail_at)

    async def test_invalid_initial_properties_never_reach_set(self):
        for identity, override, count in (
            ((2, 1), (0x5004, 1, 0), 1),
            ((2, 1), (1, 2, 0), 1),
            ((5, 4), (0x5004, 1, 1), 2),
            ((5, 4), (0x6004, 1, 0), 2),
        ):
            with self.subTest(identity=identity, override=override):
                self.prepare(initial_overrides={identity: override})
                with self.assertRaises(read.ProbeError):
                    await self.client.control()
                self.assertEqual(self.result["relay_write_attempts"], 0)
                await self.assert_no_more_requests(count)

    async def test_bad_ack_or_post_type_never_succeeds_or_sends_another_set(self):
        self.prepare(set_status=1)
        with self.assertRaisesRegex(read.ProbeError, "relay_set_response_invalid"):
            await self.client.control()
        await self.assert_no_more_requests(5)
        self.prepare(post_overrides={(2, 1): (0x1001, 0, 0)})
        with self.assertRaises(read.ProbeError):
            await self.client.control()
        self.assertEqual(self.result["relay_write_attempts"], 1)
        await self.assert_no_more_requests(6)

    async def test_cancellation_after_reserved_set_consumes_nonce_and_stays_uncertain(self):
        self.prepare()
        self.block_on = 5
        task = asyncio.create_task(self.client.control())
        await self.started.wait()
        with self.assertRaisesRegex(read.ProbeError, "relay_request_in_progress"):
            await self.client.request_reserved("set")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client.next_counter, 6)
        self.assertTrue(self.result["maybe_executed"])
        self.assertEqual(self.result["relay_write_attempts"], 1)
        self.assertFalse(self.result["verified"])
        await self.assert_no_more_requests(5)

    async def test_authentication_and_f0_are_required_before_reads(self):
        for attribute in ("authenticated", "f0_verified"):
            with self.subTest(attribute=attribute):
                self.prepare()
                setattr(self.client, attribute, False)
                with self.assertRaises(read.ProbeError):
                    await self.client.control()
                self.assertEqual(self.records, [])

    async def test_guarded_transport_rejects_other_properties_actions_and_wrong_bool(self):
        self.prepare()
        self.client.ch = type("Channel", (), {"_task": None})()
        self.client.initial, self.client.initial_values = True, BASE.copy()
        self.client.write_attempts = 1
        tid, counter = read.READ_TID, 5
        self.client.expected_responses[tid] = ("set", (2, 1))
        valid = relay.bool_set_packet(pack, tid, False)
        for wrong in (relay.bool_set_packet(pack, tid, True),
                      pack(tid, 0, b"\1\7\1\0\1\0\0"),
                      pack(tid, 0, b"\1\5\4\0\4\x50\0\0\0\0"),
                      pack(tid, 5)):
            self.client.reserved_tx = (wrong, counter)
            with self.assertRaises(read.ProbeError):
                await self.client.send(wrong, counter)
        self.assertEqual(self.raw.writes, [])
        self.client.reserved_tx = (valid, counter)
        await self.client.send(valid, counter)
        self.assertEqual(len(self.raw.writes), 2)
        self.assertEqual(self.raw.writes[1][1][4:], valid)
        with self.assertRaises(read.ProbeError):
            await self.client.c.write_gatt_char(UPSTREAM.CMD, b"not-reserved")
        for method in (self.client.set_pkt, self.client.action_pkt):
            with self.assertRaises(read.ProbeError):
                method()


if __name__ == "__main__":
    unittest.main()

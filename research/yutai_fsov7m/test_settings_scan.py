"""Offline settings enumeration tests; no Bluetooth, SSH, ADB, or cloud calls."""
import asyncio
import json
from pathlib import Path
import struct
import subprocess
import sys
import unittest

import read_probe as read
import settings_scan as scan
from test_read_probe import FakeRaw, UPSTREAM, pack


def response(tid, items):
    body = bytes([len(items)])
    for siid, piid, status, type_code, raw in items:
        body += bytes([siid]) + piid.to_bytes(2, "little") + status.to_bytes(2, "little")
        if status == 0:
            body += ((type_code << 12) | len(raw)).to_bytes(2, "little") + raw
    return pack(tid, 3, body)


class ParserTests(unittest.TestCase):
    def test_default_fixed_scope_and_optional_subset(self):
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": "ab" * 32}
        key, properties = scan.validated_input(data)
        self.assertEqual(key.key, b"\xab" * 32)
        self.assertEqual(len(properties), 144)
        self.assertEqual(properties[0], (2, 1))
        self.assertEqual(properties[-1], (10, 16))
        self.assertEqual(scan.validated_input({**data, "properties": [[2, 1], [10, 16]]})[1], ((2, 1), (10, 16)))
        for field, value in (("operation", "write"), ("batch_size", 5), ("siid", 3)):
            with self.subTest(field=field), self.assertRaises(read.ProbeError):
                scan.validated_input({**data, field: value})
        with self.assertRaises(read.ProbeError):
            scan.validated_input({**data, "model": "wiing.plug.v7icm"})

    def test_invalid_or_duplicate_properties_rejected(self):
        cases = (None, [], [[2, 1], [2, 1]], [[1, 1]], [[11, 1]], [[2, 0]],
                 [[2, 17]], [[True, 1]], [[2, True]], [[2, 1.0]], [[2]], ["2,1"], {})
        for items in cases:
            with self.subTest(items=items), self.assertRaises(read.ProbeError):
                scan.fixed_properties(items)

    def test_supported_values_and_raw_hex(self):
        cases = [(0, b"\1", True), (0, b"\0", False), (1, b"\xff", 255),
                 (2, b"\xff", -1), (3, b"\xff\xff", 65535),
                 (4, b"\0\x80", -32768), (5, b"\xff" * 4, 4294967295),
                 (6, b"\xff" * 4, -1), (7, b"\xff" * 8, 18446744073709551615),
                 (8, b"\xff" * 8, -1), (9, struct.pack("<f", 1.5), 1.5),
                 (10, b"name\n\"\x1b", "name\n\"\x1b"), (10, b"", "")]
        for type_code, raw, expected in cases:
            with self.subTest(type_code=type_code, raw=raw):
                item = scan.parse_read_response(response(9, [(2, 1, 0, type_code, raw)]), 9, [(2, 1)])[0]
                self.assertEqual(item["value"], expected)
                self.assertEqual(item["raw_hex"], raw.hex())
                self.assertEqual(item["typelen"], type_code << 12 | len(raw))
                self.assertNotIn("\n", json.dumps(item, ensure_ascii=True))
                self.assertNotIn("\x1b", json.dumps(item, ensure_ascii=True))

    def test_unknown_type_is_kept_as_raw_without_guessing(self):
        item = scan.parse_read_response(response(9, [(2, 1, 0, 15, b"\xff\0")]), 9, [(2, 1)])[0]
        self.assertEqual(item["type_name"], "unknown")
        self.assertEqual(item["type_code"], 15)
        self.assertEqual(item["raw_hex"], "ff00")
        self.assertNotIn("value", item)

    def test_mixed_status_and_values(self):
        packet = response(9, [(2, 1, 0, 0, b"\1"), (2, 2, 0xFFFF, None, None),
                              (3, 1, 0, 3, b"\x34\x12"), (3, 2, 7, None, None)])
        items = scan.parse_read_response(packet, 9, [(2, 1), (2, 2), (3, 1), (3, 2)])
        self.assertEqual(items[1], {"siid": 2, "piid": 2, "status": 65535})
        self.assertEqual(items[2]["value"], 0x1234)
        self.assertEqual(items[3], {"siid": 3, "piid": 2, "status": 7})

    def test_strict_tid_opcode_count_target_order_and_trailing_bytes(self):
        valid = response(9, [(2, 1, 0, 0, b"\1"), (2, 2, 8, None, None)])
        body = valid[5:]
        cases = (b"", valid[:-1], valid + b"x", pack(8, 3, body), pack(9, 1, body),
                 pack(9, 3, b""), pack(9, 3, b"\1" + body[1:]),
                 response(9, [(2, 2, 8, None, None), (2, 1, 0, 0, b"\1")]),
                 response(9, [(2, 1, 0, 0, b"\1"), (2, 1, 8, None, None)]),
                 pack(9, 3, body + b"x"))
        for packet in cases:
            with self.subTest(packet=packet.hex()), self.assertRaises(read.ProbeError):
                scan.parse_read_response(packet, 9, [(2, 1), (2, 2)])
        with self.assertRaises(read.ProbeError):
            scan.parse_read_response(valid, 9, [(2, p) for p in range(1, 6)])

    def test_truncated_headers_types_values_and_invalid_known_encodings(self):
        bodies = (b"\1\2", b"\1\2\1\0\0\0", b"\1\2\1\0\0\0\1",
                  b"\1\2\1\0\0\0\4\x50\1\2")
        for body in bodies:
            with self.subTest(body=body.hex()), self.assertRaises(read.ProbeError):
                scan.parse_read_response(pack(9, 3, body), 9, [(2, 1)])
        cases = [(0, b""), (0, b"\2"), (1, b"\0\0"), (3, b"\0"),
                 (7, b"\0" * 7), (9, b"\0" * 3), (9, struct.pack("<f", float("nan"))),
                 (9, struct.pack("<f", float("inf"))), (10, b"\xff")]
        for type_code, raw in cases:
            with self.subTest(type_code=type_code, raw=raw), self.assertRaises(read.ProbeError):
                scan.parse_read_response(response(9, [(2, 1, 0, type_code, raw)]), 9, [(2, 1)])

    def test_cli_rejects_bad_scope_without_secret_or_traceback(self):
        secret = "ab" * 32
        data = {"address": "02:00:00:00:00:01", "model": read.MODEL, "gatt_ltmk": secret, "properties": [[2, 17]]}
        result = subprocess.run([sys.executable, str(Path(scan.__file__))], input=json.dumps(data),
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertNotIn(secret, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["error_code"], "settings_property_outside_scope")
        self.assertEqual(report["error_class"], "ProbeError")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.result = {"connection_attempted": True}
        self.plan = tuple((2, piid) for piid in range(1, 9))
        self.raw = FakeRaw()
        self.client = scan.make_settings_client(UPSTREAM, self.plan)(object(), b"x" * 32, self.raw, self.result)
        self.raw.owner = self.client
        self.client.authenticated = self.client.f0_verified = True
        self.client.sent = [(pack(1, 0xF0), 0)]
        self.records = []
        self.error_on_batch = None
        self.block_on_batch = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

        async def fake_exchange(packet, tid, counter):
            self.assertGreater(self.client.next_tid, tid)
            self.assertGreater(self.client.next_counter, counter)
            self.client.authorize_send(packet, counter)
            self.client.sent.append((packet, counter))
            self.records.append((tid, counter, packet[4]))
            if len(self.records) == self.block_on_batch:
                self.started.set()
                await self.release.wait()
            if len(self.records) == self.error_on_batch:
                raise TimeoutError("must-not-log")
            batch = self.client.expected_responses[tid]
            payload = response(tid, [(siid, piid, 0, 1, bytes([piid])) for siid, piid in batch])
            return self.client.parse_pending_response(payload)

        self.client.exchange = fake_exchange

    async def test_complete_scan_only_gets_with_monotonic_unique_identifiers(self):
        await self.client.scan()
        self.assertEqual(self.result["status"], "settings_scan_succeeded")
        self.assertEqual(self.result["completed_property_count"], 8)
        self.assertEqual(self.result["batches_completed"], 2)
        self.assertEqual(self.result["relay_writes"], 0)
        self.assertEqual([record[2] for record in self.records], [2, 2])
        self.assertEqual([record[0] for record in self.records], [read.READ_TID, read.READ_TID + 1])
        self.assertEqual([record[1] for record in self.records], [1, 2])
        self.assertEqual([item["piid"] for item in self.result["properties"]], list(range(1, 9)))
        self.assertEqual(self.raw.writes, [])
        with self.assertRaises(read.ProbeError):
            await self.client.request_next_batch()

    async def test_failure_stops_and_keeps_prior_batch_without_retry(self):
        self.error_on_batch = 2
        with self.assertRaises(TimeoutError):
            await self.client.scan()
        self.assertEqual(self.result["completed_property_count"], 4)
        self.assertEqual(self.result["batches_attempted"], 2)
        self.assertEqual([item["piid"] for item in self.result["properties"]], [1, 2, 3, 4])
        self.assertEqual(self.client.next_counter, 3)
        self.assertEqual(self.client.expected_responses, {})
        self.assertIsNone(self.client.reserved_tx)
        for operation in (self.client.scan, self.client.request_next_batch):
            with self.assertRaises(read.ProbeError):
                await operation()
        self.assertEqual(len(self.records), 2)
        self.assertNotIn("must-not-log", json.dumps(self.result))

    async def test_cancellation_preserves_completed_batch_and_stops(self):
        self.block_on_batch = 2
        task = asyncio.create_task(self.client.scan())
        await self.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.result["completed_property_count"], 4)
        self.assertTrue(self.client.scan_failed)
        self.assertEqual(self.client.expected_responses, {})
        self.assertEqual(self.result["relay_writes"], 0)

    async def test_parallel_request_is_rejected(self):
        self.block_on_batch = 1
        task = asyncio.create_task(self.client.request_next_batch())
        await self.started.wait()
        with self.assertRaises(read.ProbeError):
            await self.client.request_next_batch()
        self.release.set()
        await task
        self.assertEqual(len(self.records), 1)

    def test_allowlist_rejects_unreserved_get_set_action_and_repeated_f0(self):
        packets = (self.client.get_pkt(9, [(2, 1)]), pack(9, 0, b"\0"), pack(9, 5, b"\0"), pack(1, 0xF0))
        for packet in packets:
            with self.subTest(opcode=packet[4]), self.assertRaises(read.ProbeError):
                self.client.authorize_send(packet, 1)
        for method in (self.client.set_pkt, self.client.action_pkt):
            with self.assertRaises(read.ProbeError):
                method()
        tid = read.READ_TID
        packet = pack(tid, 0, b"\0")
        self.client.expected_responses[tid] = ((2, 1),)
        self.client.reserved_tx = (packet, 1)
        with self.assertRaises(read.ProbeError):
            self.client.authorize_send(packet, 1)

    async def test_handshake_f0_order_and_known_response_scope(self):
        self.client.sent = []
        self.client.f0_verified = False
        self.client.authorize_send(pack(1, 0xF0), 0)
        with self.assertRaises(read.ProbeError):
            self.client.authorize_send(self.client.get_pkt(9, [(2, 1)]), 1)
        with self.assertRaises(read.ProbeError):
            await self.client.request_next_batch()
        self.assertEqual(self.client.parse_pending_response(pack(1, 0xF0)), {"verified": True})
        with self.assertRaises(read.ProbeError):
            self.client.parse_pending_response(response(9, [(2, 1, 0, 0, b"\1")]))
        self.client.authenticated = False
        with self.assertRaises(read.ProbeError):
            self.client.authorize_send(pack(1, 0xF0), 0)

    async def test_actual_send_transport_rejects_unreserved_payloads(self):
        self.client.ch = type("Channel", (), {"_task": None})()
        with self.assertRaises(read.ProbeError):
            await self.client.send(pack(9, 5, b"\0"), 1)
        self.assertEqual(self.raw.writes, [])
        tid, counter = read.READ_TID, 1
        packet = self.client.get_pkt(tid, ((2, 1),))
        self.client.expected_responses[tid] = ((2, 1),)
        self.client.reserved_tx = (packet, counter)
        await self.client.send(packet, counter)
        self.assertEqual(len(self.raw.writes), 2)
        self.assertEqual(self.raw.writes[0], (UPSTREAM.CMD, b"\0\0\0\0\1\0"))
        self.assertEqual(self.raw.writes[1][1][4:], packet)
        with self.assertRaises(read.ProbeError):
            await self.client.c.write_gatt_char(UPSTREAM.CMD, b"not-reserved")


if __name__ == "__main__":
    unittest.main()

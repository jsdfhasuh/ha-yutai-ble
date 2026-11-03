"""Bounded read-only candidate-property enumeration for the new fsov8m plug."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import sys

import read_probe as read


MAX_BATCH = 4
DEFAULT_PROPERTIES = tuple((siid, piid) for siid in range(2, 11) for piid in range(1, 17))
TYPE_NAMES = {
    0: "bool", 1: "uint8", 2: "int8", 3: "uint16", 4: "int16",
    5: "uint32", 6: "int32", 7: "uint64", 8: "int64", 9: "float", 10: "string",
}
INTEGER_SIZES = {1: 1, 2: 1, 3: 2, 4: 2, 5: 4, 6: 4, 7: 8, 8: 8}


def fixed_properties(items):
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= len(DEFAULT_PROPERTIES):
        read.fail("settings_properties_invalid")
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            read.fail("settings_property_invalid")
        siid, piid = item
        if type(siid) is not int or type(piid) is not int or not (2 <= siid <= 10 and 1 <= piid <= 16):
            read.fail("settings_property_outside_scope")
        pair = (siid, piid)
        if pair in seen:
            read.fail("settings_property_duplicate")
        seen.add(pair)
        result.append(pair)
    return tuple(result)


def validated_input(data):
    required = {"address", "model", "gatt_ltmk"}
    if not isinstance(data, dict) or set(data) not in (required, required | {"properties"}):
        read.fail("input_fields_invalid")
    properties = fixed_properties(data["properties"]) if "properties" in data else DEFAULT_PROPERTIES
    key = read.validated_input({name: data[name] for name in required})
    return key, properties


def decode_value(type_code, raw):
    if type_code == 0:
        if len(raw) != 1 or raw[0] not in (0, 1):
            read.fail("settings_bool_value_invalid")
        return bool(raw[0])
    if type_code in INTEGER_SIZES:
        if len(raw) != INTEGER_SIZES[type_code]:
            read.fail("settings_integer_size_invalid")
        return int.from_bytes(raw, "little", signed=type_code in (2, 4, 6, 8))
    if type_code == 9:
        if len(raw) != 4:
            read.fail("settings_float_size_invalid")
        value = struct.unpack("<f", raw)[0]
        if not math.isfinite(value):
            read.fail("settings_float_value_invalid")
        return value
    if type_code == 10:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            read.fail("settings_string_encoding_invalid")
    return None


def parse_read_response(packet, expected_tid, expected_properties):
    expected = fixed_properties(expected_properties)
    if len(expected) > MAX_BATCH:
        read.fail("settings_batch_too_large")
    tid, opcode, body = read.parse_frame(packet)
    if tid != expected_tid or opcode != 3:
        read.fail("settings_response_mismatch")
    if not body or body[0] != len(expected):
        read.fail("settings_property_count_invalid")
    offset = 1
    result = []
    for siid, piid in expected:
        if len(body) - offset < 5:
            read.fail("settings_property_header_truncated")
        actual = (body[offset], int.from_bytes(body[offset + 1:offset + 3], "little"))
        if actual != (siid, piid):
            read.fail("settings_property_target_or_order_invalid")
        status = int.from_bytes(body[offset + 3:offset + 5], "little")
        offset += 5
        item = {"siid": siid, "piid": piid, "status": status}
        if status == 0:
            if len(body) - offset < 2:
                read.fail("settings_typelen_truncated")
            typelen = int.from_bytes(body[offset:offset + 2], "little")
            type_code, length = typelen >> 12, typelen & 0xFFF
            offset += 2
            if len(body) - offset < length:
                read.fail("settings_property_value_truncated")
            raw = body[offset:offset + length]
            offset += length
            item.update({"typelen": typelen, "type_code": type_code,
                         "type_name": TYPE_NAMES.get(type_code, "unknown"),
                         "value_length": length, "raw_hex": raw.hex()})
            if type_code in TYPE_NAMES:
                item["value"] = decode_value(type_code, raw)
        result.append(item)
    if offset != len(body):
        read.fail("settings_response_trailing_bytes")
    return result


def make_settings_client(upstream, properties=DEFAULT_PROPERTIES):
    plan = fixed_properties(properties)
    ReadClient = read.make_client_class(upstream)

    class SettingsClient(ReadClient):
        def __init__(self, device, key, raw_client, result):
            super().__init__(device, key, raw_client, result)
            self.properties = plan
            self.next_tid, self.next_counter = read.READ_TID, 1
            self.next_index = 0
            self.reserved_tx = None
            self.expected_responses = {}
            self.scan_failed = False
            result.pop("siid", None)
            result.pop("piid", None)
            result.update({"operation": "read_settings_properties_only", "relay_writes": 0,
                           "properties": [], "requested_property_count": len(plan),
                           "batches_attempted": 0, "batches_completed": 0,
                           "completed_property_count": 0, "max_batch_size": MAX_BATCH})

        def authorize_send(self, packet, counter):
            if not self.authenticated:
                read.fail("protocol_phase_invalid")
            if not self.sent:
                if (packet, counter) != (self.pack(1, 0xF0), 0):
                    read.fail("settings_f0_required")
                return
            if not self.f0_verified or self.reserved_tx != (packet, counter):
                read.fail("settings_read_not_authorized")
            tid, opcode, _body = read.parse_frame(packet)
            batch = self.expected_responses.get(tid)
            if opcode != 2 or not batch or packet != self.get_pkt(tid, batch):
                read.fail("settings_read_not_authorized")
            self.reserved_tx = None

        def parse_pending_response(self, plaintext):
            tid, _opcode, _body = read.parse_frame(plaintext)
            if tid == 1:
                return super().parse_pending_response(plaintext)
            batch = self.expected_responses.get(tid)
            if batch is None:
                read.fail("settings_response_not_expected")
            return parse_read_response(plaintext, tid, batch)

        async def request_next_batch(self):
            if self.scan_failed:
                read.fail("settings_scan_already_failed")
            if self.reserved_tx is not None or self.expected_responses:
                read.fail("settings_request_in_progress")
            if self.next_index >= len(self.properties):
                read.fail("settings_scan_already_complete")
            if not self.authenticated or not self.f0_verified or not self.sent:
                read.fail("protocol_phase_invalid")
            batch = self.properties[self.next_index:self.next_index + MAX_BATCH]
            tid, counter = self.next_tid, self.next_counter
            # Identifiers are consumed before awaiting; an uncertain send is never retried.
            self.next_tid += 1
            self.next_counter += 1
            self.next_index += len(batch)
            packet = self.get_pkt(tid, batch)
            self.reserved_tx = (packet, counter)
            self.expected_responses[tid] = batch
            self.result["batches_attempted"] += 1
            self.result["phase"] = "read_settings_properties"
            try:
                response = await self.exchange(packet, tid, counter)
                self.result["properties"].extend(response)
                self.result["batches_completed"] += 1
                self.result["completed_property_count"] += len(response)
                return response
            except BaseException:
                self.scan_failed = True
                raise
            finally:
                self.reserved_tx = None
                self.expected_responses.pop(tid, None)

        async def scan(self):
            if self.scan_failed:
                read.fail("settings_scan_already_failed")
            try:
                while self.next_index < len(self.properties):
                    await self.request_next_batch()
                    if self.next_index < len(self.properties):
                        await asyncio.sleep(0.05)
                self.result["status"] = "settings_scan_succeeded"
            except BaseException:
                self.scan_failed = True
                raise

    return SettingsClient


async def perform_scan(client, _result):
    await client.scan()


def main():
    logging.disable(logging.CRITICAL)
    try:
        if len(sys.argv) != 1:
            read.fail("command_line_arguments_not_allowed")
        raw_input = sys.stdin.buffer.read(16385)
        if len(raw_input) > 16384:
            read.fail("input_too_large")
        data = json.loads(raw_input, object_pairs_hook=read.no_duplicate_keys)
        key, properties = validated_input(data)
        del raw_input, data
        factory = lambda upstream: make_settings_client(upstream, properties)
        result = asyncio.run(read.run_probe(key, client_factory=factory, operation=perform_scan))
        key = b""
    except Exception as error:
        result = {"status": "error", "phase": "input_or_runtime",
                  "error_code": error.args[0] if isinstance(error, read.ProbeError) else "input_or_runtime_failed",
                  "error_class": type(error).__name__}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    return 0 if result.get("status") == "settings_scan_succeeded" and not result.get("cleanup_error_code") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Single-field native local timer writes with fresh snapshot guards.

Only (5.6) bit1 controls the observed local master. Disarming is allowed while
the relay is ON, but still requires both expected words. No retries or restore.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import local_settings as local
import read_probe as read
import settings_scan as scan


TYPELEN = 0x5004
RELAY_PROPERTY = (2, 1)
MASTER_PROPERTY = (5, 6)
LOCAL_PROPERTY = (5, 7)
MASTER_BIT = 0x2
GUARD_PROPERTIES = (RELAY_PROPERTY, MASTER_PROPERTY, LOCAL_PROPERTY)
FIELD_MASKS = {"master": MASTER_BIT, "start_min": local.START_MASK, "end_min": local.END_MASK,
               "start_enabled": local.START_ENABLED_BIT, "end_enabled": local.END_ENABLED_BIT,
               "repeat": local.REPEAT_BIT, "weekday_mask": local.WEEKDAY_MASK}


def uint32(value):
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        read.fail("native_local_uint32_required")
    return value


def selection(field):
    if type(field) is not str or field not in FIELD_MASKS:
        read.fail("native_local_field_not_supported")
    return (MASTER_PROPERTY if field == "master" else LOCAL_PROPERTY), FIELD_MASKS[field]


def field_target(initial, field, value):
    selection(field)
    uint32(initial)
    if field == "master":
        if type(value) is not bool:
            read.fail("native_local_master_bool_required")
        return (initial & ~MASTER_BIT) | (MASTER_BIT if value else 0)
    try:
        return local.patch_local_word(initial, **{field: value})
    except ValueError:
        read.fail("native_local_value_invalid")


def validated_input(data):
    identity = {"address", "model", "gatt_ltmk"}
    options = {"field", "value", "expected_local_word", "expected_master_word"}
    if not isinstance(data, dict) or set(data) != identity | options:
        read.fail("input_fields_invalid")
    selected = {name: data[name] for name in options}
    uint32(selected["expected_local_word"])
    uint32(selected["expected_master_word"])
    pair, _mask = selection(selected["field"])
    initial = selected["expected_master_word"] if pair == MASTER_PROPERTY else selected["expected_local_word"]
    field_target(initial, selected["field"], selected["value"])
    key = read.validated_input({name: data[name] for name in identity})
    return key, selected


def uint32_set_packet(pack, tid, pair, value):
    if pair not in (MASTER_PROPERTY, LOCAL_PROPERTY):
        read.fail("native_local_property_not_supported")
    return pack(tid, 0, bytes([1, pair[0]]) + pair[1].to_bytes(2, "little")
                + TYPELEN.to_bytes(2, "little") + uint32(value).to_bytes(4, "little"))


def parse_get_response(packet, expected_tid, properties):
    items = scan.parse_read_response(packet, expected_tid, properties)
    for item in items:
        pair = (item["siid"], item["piid"])
        if item["status"] != 0:
            read.fail("native_local_read_status_failed")
        expected_type, expected_typelen = (bool, 1) if pair == RELAY_PROPERTY else (int, TYPELEN)
        if item["typelen"] != expected_typelen or type(item.get("value")) is not expected_type:
            read.fail("native_local_read_type_invalid")
    return items


def parse_set_response(packet, expected_tid, pair):
    tid, opcode, body = read.parse_frame(packet)
    if tid != expected_tid or opcode != 1:
        read.fail("native_local_set_response_mismatch")
    if len(body) != 6 or body[:4] != bytes([1, pair[0]]) + pair[1].to_bytes(2, "little"):
        read.fail("native_local_set_response_target_invalid")
    if body[4:] != b"\0\0":
        read.fail("native_local_set_status_failed")
    return {"siid": pair[0], "piid": pair[1], "status": 0}


def make_native_local_client(upstream, *, field, value, expected_local_word, expected_master_word):
    pair, mask = selection(field)
    uint32(expected_local_word)
    uint32(expected_master_word)
    expected_initial = expected_master_word if pair == MASTER_PROPERTY else expected_local_word
    field_target(expected_initial, field, value)
    disarm = field in ("master", "start_enabled", "end_enabled") and value is False
    ReadClient = read.make_client_class(upstream)

    class NativeLocalClient(ReadClient):
        def __init__(self, device, key, raw_client, result):
            super().__init__(device, key, raw_client, result)
            self.next_tid, self.next_counter = read.READ_TID, 1
            self.reserved_tx = None
            self.expected_responses = {}
            self.initial = self.target = self.set_authorization = None
            self.started = self.failed = self.completed = self.guards_verified = False
            self.write_attempts = 0
            result.update({"operation": "set_native_local_field", "field": field, "value": value,
                           "siid": pair[0], "piid": pair[1], "expected_local_word": expected_local_word,
                           "expected_master_word": expected_master_word, "disarm": disarm,
                           "relay_writes": 0, "setting_write_attempts": 0, "maybe_executed": False,
                           "guards_verified": False, "changed": False, "verified": False})

        def computed_target(self):
            return field_target(self.initial, field, value)

        def authorize_send(self, packet, counter):
            if not self.authenticated:
                read.fail("protocol_phase_invalid")
            if not self.sent:
                if (packet, counter) != (self.pack(1, 0xF0), 0):
                    read.fail("native_local_f0_required")
                return
            if not self.f0_verified or self.reserved_tx != (packet, counter):
                read.fail("native_local_packet_not_authorized")
            tid, opcode, _body = read.parse_frame(packet)
            expected = self.expected_responses.get(tid)
            if expected is None:
                read.fail("native_local_packet_not_authorized")
            kind, properties = expected
            if kind in ("guards", "verify"):
                allowed = opcode == 2 and packet == self.get_pkt(tid, properties)
            else:
                allowed = (kind == "set" and opcode == 0 and self.guards_verified and self.write_attempts == 1
                           and self.initial == expected_initial and self.target == self.computed_target()
                           and self.initial != self.target and not (self.initial ^ self.target) & ~mask
                           and packet == uint32_set_packet(self.pack, tid, pair, self.target))
            if not allowed:
                read.fail("native_local_packet_not_authorized")
            self.reserved_tx = None

        def parse_pending_response(self, plaintext):
            tid, _opcode, _body = read.parse_frame(plaintext)
            if tid == 1:
                return super().parse_pending_response(plaintext)
            expected = self.expected_responses.get(tid)
            if expected is None:
                read.fail("native_local_response_not_expected")
            kind, properties = expected
            return (parse_set_response(plaintext, tid, pair) if kind == "set"
                    else parse_get_response(plaintext, tid, properties))

        async def request_reserved(self, kind):
            if self.failed or self.completed:
                read.fail("native_local_control_already_finished")
            if self.reserved_tx is not None or self.expected_responses:
                read.fail("native_local_request_in_progress")
            if not self.authenticated or not self.f0_verified or not self.sent:
                read.fail("protocol_phase_invalid")
            if not self.started or self.next_counter > 3:
                read.fail("native_local_request_budget_invalid")
            tid, counter = self.next_tid, self.next_counter
            properties = ()
            if kind == "guards":
                if counter != 1 or self.guards_verified:
                    read.fail("native_local_guards_not_authorized")
                properties = GUARD_PROPERTIES
                packet = self.get_pkt(tid, properties)
            elif kind == "set":
                if (not self.guards_verified or self.write_attempts != 0 or counter != 2
                        or self.initial != expected_initial or self.set_authorization != self.target
                        or self.target != self.computed_target() or self.initial == self.target
                        or (self.initial ^ self.target) & ~mask):
                    read.fail("native_local_set_not_authorized")
                self.set_authorization = None
                packet = uint32_set_packet(self.pack, tid, pair, self.target)
                self.write_attempts = 1
                self.result.update(setting_write_attempts=1, maybe_executed=True)
            elif kind == "verify":
                if (not self.guards_verified or self.initial is None or self.target is None
                        or self.write_attempts != int(self.initial != self.target)):
                    read.fail("native_local_verify_not_authorized")
                properties = (pair,)
                packet = self.get_pkt(tid, properties)
            else:
                read.fail("native_local_request_kind_invalid")
            # Consume identifiers before awaiting; uncertain sends are never retried.
            self.next_tid += 1
            self.next_counter += 1
            self.reserved_tx = (packet, counter)
            self.expected_responses[tid] = (kind, properties)
            try:
                return await self.exchange(packet, tid, counter)
            except BaseException:
                self.failed = True
                raise
            finally:
                self.reserved_tx = None
                self.expected_responses.pop(tid, None)

        async def control(self):
            if self.started:
                read.fail("native_local_control_already_started")
            self.started = True
            try:
                self.result["phase"] = "native_local_guard_read"
                items = await self.request_reserved("guards")
                self.result["initial_properties"] = items
                words = {(item["siid"], item["piid"]): item["value"] for item in items}
                if words[LOCAL_PROPERTY] != expected_local_word:
                    read.fail("native_local_snapshot_mismatch")
                if words[MASTER_PROPERTY] != expected_master_word:
                    read.fail("native_local_master_snapshot_mismatch")
                if not disarm and words[RELAY_PROPERTY] is not False:
                    read.fail("native_local_relay_must_be_off")
                if pair == LOCAL_PROPERTY and not disarm and words[MASTER_PROPERTY] & MASTER_BIT:
                    read.fail("native_local_master_must_be_off")
                self.initial = words[pair]
                self.target = self.computed_target()
                self.result.update(initial=self.initial, target=self.target, guards_verified=True)
                self.guards_verified = True
                if self.target != self.initial:
                    self.result["phase"] = "native_local_set"
                    self.set_authorization = self.target
                    await self.request_reserved("set")
                self.result["phase"] = "native_local_verify"
                observed = (await self.request_reserved("verify"))[0]["value"]
                self.result["observed"] = observed
                if observed != self.target:
                    read.fail("native_local_readback_mismatch")
                self.completed = True
                self.result.update(changed=observed != self.initial, verified=True, status="native_local_verified")
            except BaseException:
                self.failed = True
                raise

    return NativeLocalClient


async def perform_control(client, _result):
    await client.control()


def main():
    logging.disable(logging.CRITICAL)
    try:
        if len(sys.argv) != 1:
            read.fail("command_line_arguments_not_allowed")
        raw_input = sys.stdin.buffer.read(4097)
        if len(raw_input) > 4096:
            read.fail("input_too_large")
        data = json.loads(raw_input, object_pairs_hook=read.no_duplicate_keys)
        key, options = validated_input(data)
        del raw_input, data
        factory = lambda upstream: make_native_local_client(upstream, **options)
        result = asyncio.run(read.run_probe(key, client_factory=factory, operation=perform_control))
        key = b""
    except Exception as error:
        result = {"status": "error", "phase": "input_or_runtime",
                  "error_code": error.args[0] if isinstance(error, read.ProbeError) else "input_or_runtime_failed",
                  "error_class": type(error).__name__}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("status") == "native_local_verified" and not result.get("cleanup_error_code") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Guarded timer-property configuration and observed cycle master control.

Cycle master is (5.1) bit31; local master is (5.6) bit1.
No relay, clock, or unobserved bit writes are permitted.
Fresh snapshots are not a compare-and-swap.
Failed or uncertain writes are never retried or automatically restored here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import cycle_settings as cycle
import local_settings as local
import read_probe as read
import settings_scan as scan


TYPELEN = 0x5004
RELAY_PROPERTY = (2, 1)
LOCAL_PROPERTY = (5, 7)
MASTER_PROPERTY = (5, 6)
CYCLE_PROPERTY = (5, 1)
CYCLE_MASTER_BIT = 0x80000000
KNOWN_MASTER_SNAPSHOT = 0x4B03C004
COMPOUND_FIELDS = {
    "schedule_config": frozenset(("start_min", "end_min", "repeat", "weekday_mask")),
    "on_config": frozenset(("on_seconds", "start_state")),
    "off_config": frozenset(("off_seconds", "end_state")),
}
SELECTIONS = {
    ("local", "start_min"): (LOCAL_PROPERTY, local.START_MASK),
    ("local", "end_min"): (LOCAL_PROPERTY, local.END_MASK),
    ("cycle", "start_min"): ((5, 1), cycle.TIME_MASK << 11),
    ("cycle", "end_min"): ((5, 1), cycle.TIME_MASK),
    ("cycle", "on_seconds"): ((5, 2), cycle.SECONDS_MAX),
    ("cycle", "off_seconds"): ((5, 3), cycle.SECONDS_MAX),
    ("cycle", "start_state"): ((5, 2), cycle.STATE_BIT),
    ("cycle", "end_state"): ((5, 3), cycle.STATE_BIT),
    ("cycle", "repeat"): ((5, 1), cycle.REPEAT_BIT),
    ("cycle", "weekday_mask"): ((5, 1), cycle.WEEKDAY_MAX << 22),
    ("cycle", "master"): (CYCLE_PROPERTY, CYCLE_MASTER_BIT),
    ("cycle", "schedule_config"): (CYCLE_PROPERTY, 0x3FFFFFFF),
    ("cycle", "on_config"): ((5, 2), 0xFFFFFFFF),
    ("cycle", "off_config"): ((5, 3), 0xFFFFFFFF),
}


def uint32(value):
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        read.fail("timer_uint32_required")
    return value


def selection(target, field):
    if type(target) is not str or type(field) is not str or (target, field) not in SELECTIONS:
        read.fail("timer_field_not_supported")
    return SELECTIONS[target, field]


def authorized_master_snapshot(value):
    uint32(value)
    if value != KNOWN_MASTER_SNAPSHOT:
        read.fail("timer_master_snapshot_not_authorized")
    return value


def timer_target(initial, target, field, value):
    property_pair, _mask = selection(target, field)
    uint32(initial)
    if target == "cycle" and field == "master":
        if type(value) is not bool:
            read.fail("timer_master_bool_required")
        return (initial & ~CYCLE_MASTER_BIT) | (CYCLE_MASTER_BIT if value else 0)
    if field in COMPOUND_FIELDS:
        if type(value) is not dict or value.keys() != COMPOUND_FIELDS[field]:
            read.fail("timer_compound_value_invalid")
        changes = value
    else:
        changes = {field: value}
    try:
        if target == "local":
            return local.patch_local_word(initial, **changes)
        if property_pair == (5, 1):
            return cycle.patch_cycle_words(initial, 0, 0, **changes)[0]
        if property_pair == (5, 2):
            return cycle.patch_cycle_words(0, initial, 0, **changes)[1]
        return cycle.patch_cycle_words(0, 0, initial, **changes)[2]
    except ValueError:
        read.fail("timer_value_out_of_range")


def validated_input(data):
    required = {"address", "model", "gatt_ltmk"}
    options = {"target", "field", "value", "expected_initial", "expected_master_word", "expected_local_word", "expected_cycle_word"}
    if not isinstance(data, dict) or set(data) != required | options:
        read.fail("input_fields_invalid")
    selected = {name: data[name] for name in options}
    timer_target(selected["expected_initial"], selected["target"], selected["field"], selected["value"])
    disarm = selected["target"] == "cycle" and selected["field"] == "master" and selected["value"] is False
    uint32(selected["expected_master_word"])
    if not disarm:
        authorized_master_snapshot(selected["expected_master_word"])
    uint32(selected["expected_local_word"])
    uint32(selected["expected_cycle_word"])
    key = read.validated_input({name: data[name] for name in required})
    return key, selected


def uint32_set_packet(pack, tid, property_pair, value):
    if property_pair not in {pair for pair, _mask in SELECTIONS.values()}:
        read.fail("timer_property_not_supported")
    siid, piid = property_pair
    return pack(tid, 0, bytes([1, siid]) + piid.to_bytes(2, "little")
                + TYPELEN.to_bytes(2, "little") + uint32(value).to_bytes(4, "little"))


def parse_get_response(packet, expected_tid, properties):
    items = scan.parse_read_response(packet, expected_tid, properties)
    for item in items:
        pair = (item["siid"], item["piid"])
        if item["status"] != 0:
            read.fail("timer_read_status_failed")
        expected_type, expected_typelen = (bool, 1) if pair == RELAY_PROPERTY else (int, TYPELEN)
        if item["typelen"] != expected_typelen or type(item.get("value")) is not expected_type:
            read.fail("timer_read_type_invalid")
    return items


def parse_set_response(packet, expected_tid, property_pair):
    tid, opcode, body = read.parse_frame(packet)
    siid, piid = property_pair
    if tid != expected_tid or opcode != 1:
        read.fail("timer_set_response_mismatch")
    if len(body) != 6 or body[:4] != bytes([1, siid]) + piid.to_bytes(2, "little"):
        read.fail("timer_set_response_target_invalid")
    if body[4:] != b"\0\0":
        read.fail("timer_set_status_failed")
    return {"siid": siid, "piid": piid, "status": 0}


def make_timer_client(upstream, *, target, field, value, expected_initial, expected_master_word, expected_local_word, expected_cycle_word):
    property_pair, change_mask = selection(target, field)
    timer_target(expected_initial, target, field, value)
    if type(value) is dict:
        value = value.copy()
    disarm = target == "cycle" and field == "master" and value is False
    uint32(expected_master_word)
    if not disarm:
        authorized_master_snapshot(expected_master_word)
    uint32(expected_local_word)
    uint32(expected_cycle_word)
    guard_properties = (RELAY_PROPERTY, MASTER_PROPERTY, LOCAL_PROPERTY, CYCLE_PROPERTY)
    guard_batches = (guard_properties,) if property_pair in guard_properties else (guard_properties, (property_pair,))
    ReadClient = read.make_client_class(upstream)

    class TimerClient(ReadClient):
        def __init__(self, device, key, raw_client, result):
            super().__init__(device, key, raw_client, result)
            self.next_tid, self.next_counter = read.READ_TID, 1
            self.reserved_tx = None
            self.expected_responses = {}
            self.initial = self.target = self.set_authorization = None
            self.started = self.failed = self.completed = self.guards_verified = False
            self.write_attempts = 0
            self.guard_index = 0
            result.update({"operation": "set_guarded_timer_field", "timer_target": target,
                           "field": field, "value": value.copy() if type(value) is dict else value,
                           "siid": property_pair[0], "piid": property_pair[1],
                           "expected_initial": expected_initial, "expected_master_word": expected_master_word,
                           "expected_local_word": expected_local_word,
                           "expected_cycle_word": expected_cycle_word, "disarm": disarm,
                           "guard_batch_count": len(guard_batches), "guard_reads_completed": 0,
                           "relay_writes": 0, "setting_write_attempts": 0, "maybe_executed": False,
                           "guards_verified": False, "changed": False, "verified": False})

        def computed_target(self):
            return timer_target(self.initial, target, field, value)

        def authorize_send(self, packet, counter):
            if not self.authenticated:
                read.fail("protocol_phase_invalid")
            if not self.sent:
                if (packet, counter) != (self.pack(1, 0xF0), 0):
                    read.fail("timer_f0_required")
                return
            if not self.f0_verified or self.reserved_tx != (packet, counter):
                read.fail("timer_packet_not_authorized")
            tid, opcode, _body = read.parse_frame(packet)
            expected = self.expected_responses.get(tid)
            if expected is None:
                read.fail("timer_packet_not_authorized")
            kind, properties = expected
            if kind in ("guards", "verify"):
                allowed = opcode == 2 and packet == self.get_pkt(tid, properties)
            else:
                allowed = (kind == "set" and opcode == 0 and self.guards_verified
                           and self.write_attempts == 1 and self.initial == expected_initial
                           and self.target == self.computed_target() and self.initial != self.target
                           and not (self.initial ^ self.target) & ~change_mask
                           and packet == uint32_set_packet(self.pack, tid, property_pair, self.target))
            if not allowed:
                read.fail("timer_packet_not_authorized")
            self.reserved_tx = None

        def parse_pending_response(self, plaintext):
            tid, _opcode, _body = read.parse_frame(plaintext)
            if tid == 1:
                return super().parse_pending_response(plaintext)
            expected = self.expected_responses.get(tid)
            if expected is None:
                read.fail("timer_response_not_expected")
            kind, properties = expected
            if kind == "set":
                return parse_set_response(plaintext, tid, property_pair)
            return parse_get_response(plaintext, tid, properties)

        async def request_reserved(self, kind):
            if self.failed or self.completed:
                read.fail("timer_control_already_finished")
            if self.reserved_tx is not None or self.expected_responses:
                read.fail("timer_request_in_progress")
            if not self.authenticated or not self.f0_verified or not self.sent:
                read.fail("protocol_phase_invalid")
            if not self.started or self.next_counter > len(guard_batches) + 2:
                read.fail("timer_request_budget_invalid")
            tid, counter = self.next_tid, self.next_counter
            properties = ()
            if kind == "guards":
                if (self.guard_index >= len(guard_batches) or counter != self.guard_index + 1 or self.guards_verified):
                    read.fail("timer_guards_not_authorized")
                properties = guard_batches[self.guard_index]
                self.guard_index += 1
                packet = self.get_pkt(tid, properties)
            elif kind == "set":
                if (not self.guards_verified or self.write_attempts != 0 or counter != len(guard_batches) + 1
                        or self.initial != expected_initial or self.set_authorization != self.target
                        or self.target != self.computed_target() or self.initial == self.target
                        or (self.initial ^ self.target) & ~change_mask):
                    read.fail("timer_set_not_authorized")
                self.set_authorization = None
                packet = uint32_set_packet(self.pack, tid, property_pair, self.target)
                self.write_attempts = 1
                self.result["setting_write_attempts"] = 1
                self.result["maybe_executed"] = True
            elif kind == "verify":
                if (not self.guards_verified or self.initial is None or self.target is None
                        or self.write_attempts != int(self.initial != self.target)
                        or counter != len(guard_batches) + 1 + self.write_attempts):
                    read.fail("timer_verify_not_authorized")
                properties = (property_pair,)
                packet = self.get_pkt(tid, properties)
            else:
                read.fail("timer_request_kind_invalid")
            # Consume identifiers before awaiting; uncertain sends never get another attempt.
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
                read.fail("timer_control_already_started")
            self.started = True
            try:
                self.result["phase"] = "timer_guard_read"
                items = []
                self.result["initial_properties"] = items
                for _batch in guard_batches:
                    items.extend(await self.request_reserved("guards"))
                    self.result["guard_reads_completed"] += 1
                words = {(item["siid"], item["piid"]): item["value"] for item in items}
                if words[MASTER_PROPERTY] != expected_master_word:
                    read.fail("timer_master_snapshot_mismatch")
                if words[LOCAL_PROPERTY] != expected_local_word:
                    read.fail("timer_local_snapshot_mismatch")
                if words[CYCLE_PROPERTY] != expected_cycle_word:
                    read.fail("timer_cycle_snapshot_mismatch")
                if not disarm:
                    if words[RELAY_PROPERTY] is not False:
                        read.fail("timer_relay_must_be_off")
                    if words[MASTER_PROPERTY] & 2:
                        read.fail("timer_local_master_must_be_off")
                    if words[LOCAL_PROPERTY] & (local.START_ENABLED_BIT | local.END_ENABLED_BIT):
                        read.fail("timer_local_children_must_be_off")
                    if words[CYCLE_PROPERTY] & CYCLE_MASTER_BIT:
                        read.fail("timer_cycle_master_must_be_off")
                self.initial = words[property_pair]
                self.result["initial"] = self.initial
                if self.initial != expected_initial:
                    read.fail("timer_expected_initial_mismatch")
                self.target = self.computed_target()
                self.result["target"] = self.target
                self.guards_verified = True
                self.result["guards_verified"] = True
                if self.target != self.initial:
                    self.result["phase"] = "timer_set"
                    self.set_authorization = self.target
                    await self.request_reserved("set")
                self.result["phase"] = "timer_verify"
                observed = (await self.request_reserved("verify"))[0]["value"]
                self.result["observed"] = observed
                if observed != self.target:
                    read.fail("timer_readback_mismatch")
                self.completed = True
                self.result.update({"changed": observed != self.initial, "verified": True,
                                    "status": "timer_verified"})
            except BaseException:
                self.failed = True
                raise

    return TimerClient


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
        factory = lambda upstream: make_timer_client(upstream, **options)
        result = asyncio.run(read.run_probe(key, client_factory=factory, operation=perform_control))
        key = b""
    except Exception as error:
        result = {"status": "error", "phase": "input_or_runtime",
                  "error_code": error.args[0] if isinstance(error, read.ProbeError) else "input_or_runtime_failed",
                  "error_class": type(error).__name__}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("status") == "timer_verified" and not result.get("cleanup_error_code") else 1


if __name__ == "__main__":
    raise SystemExit(main())

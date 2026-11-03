"""Set one observed Yutai indicator or power-on field, then read it back."""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import read_probe as read
import settings_scan as scan


PROPERTY = (5, 4)
TYPELEN = 0x5004
INDICATOR_BIT = 0x8000
INDICATOR_FIELD = 0xC000
MODES = {"follow": 0, "off": INDICATOR_BIT}
POWER_FIELD = 0x3000
POWER_STATES = {"off": 0, "on": 0x1000, "memory": 0x2000}


def indicator_target(initial, mode):
    if type(mode) is not str or mode not in MODES:
        read.fail("indicator_mode_not_supported")
    if type(initial) is not int or not 0 <= initial <= 0xFFFFFFFF:
        read.fail("indicator_uint32_required")
    if initial & INDICATOR_FIELD not in (0, INDICATOR_BIT):
        read.fail("indicator_current_mode_not_observed")
    return (initial & ~INDICATOR_BIT) | MODES[mode]


def power_target(initial, state):
    if type(state) is not str or state not in POWER_STATES:
        read.fail("power_state_not_supported")
    if type(initial) is not int or not 0 <= initial <= 0xFFFFFFFF:
        read.fail("power_uint32_required")
    if initial & POWER_FIELD not in POWER_STATES.values():
        read.fail("power_current_state_not_observed")
    return (initial & ~POWER_FIELD) | POWER_STATES[state]


def validated_input(data):
    required = {"address", "model", "gatt_ltmk"}
    if not isinstance(data, dict) or set(data) not in (
            required | {"indicator_mode"}, required | {"power_on_state"}):
        read.fail("input_fields_invalid")
    if "indicator_mode" in data:
        indicator_target(0, data["indicator_mode"])
        options = {"indicator_mode": data["indicator_mode"]}
    else:
        power_target(0, data["power_on_state"])
        options = {"power_on_state": data["power_on_state"]}
    key = read.validated_input({name: data[name] for name in required})
    return key, options


def uint32_set_packet(pack, tid, value):
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        read.fail("indicator_uint32_required")
    return pack(tid, 0, b"\1\5\4\0\4\x50" + value.to_bytes(4, "little"))


def parse_get_response(packet, expected_tid):
    item = scan.parse_read_response(packet, expected_tid, [PROPERTY])[0]
    if item["status"] != 0:
        read.fail("indicator_read_status_failed")
    if item["typelen"] != TYPELEN or type(item.get("value")) is not int:
        read.fail("indicator_read_type_invalid")
    return item["value"]


def parse_set_response(packet, expected_tid):
    tid, opcode, body = read.parse_frame(packet)
    if tid != expected_tid or opcode != 1:
        read.fail("indicator_set_response_mismatch")
    if len(body) != 6 or body[:4] != b"\1\5\4\0":
        read.fail("indicator_set_response_target_invalid")
    if body[4:] != b"\0\0":
        read.fail("indicator_set_status_failed")
    return {"siid": 5, "piid": 4, "status": 0}


def make_settings_client(upstream, indicator_mode=None, *, power_on_state=None):
    if (indicator_mode is None) == (power_on_state is None):
        read.fail("one_setting_required")
    if indicator_mode is not None:
        indicator_target(0, indicator_mode)
        target_for = lambda value: indicator_target(value, indicator_mode)
        change_mask, setting_name, requested = INDICATOR_BIT, "indicator", {"indicator_mode": indicator_mode}
    else:
        power_target(0, power_on_state)
        target_for = lambda value: power_target(value, power_on_state)
        change_mask, setting_name, requested = POWER_FIELD, "power_on", {"power_on_state": power_on_state}
    ReadClient = read.make_client_class(upstream)

    class SettingsClient(ReadClient):
        def __init__(self, device, key, raw_client, result):
            super().__init__(device, key, raw_client, result)
            self.next_tid, self.next_counter = read.READ_TID, 1
            self.reserved_tx = None
            self.expected_responses = {}
            self.set_authorization = None
            self.initial = self.target = None
            self.started = self.failed = False
            self.write_attempts = 0
            result.update({"operation": "set_observed_" + setting_name + "_mode", "siid": 5, "piid": 4,
                           **requested, "relay_writes": 0,
                           "setting_write_attempts": 0, "maybe_executed": False,
                           "changed": False, "verified": False})

        def authorize_send(self, packet, counter):
            if not self.authenticated:
                read.fail("protocol_phase_invalid")
            if not self.sent:
                if (packet, counter) != (self.pack(1, 0xF0), 0):
                    read.fail("indicator_f0_required")
                return
            if not self.f0_verified or self.reserved_tx != (packet, counter):
                read.fail("indicator_packet_not_authorized")
            tid, opcode, _body = read.parse_frame(packet)
            expected = self.expected_responses.get(tid)
            if expected == "get":
                allowed = opcode == 2 and packet == self.get_pkt(tid, [PROPERTY])
            elif expected == "set":
                allowed = (opcode == 0 and self.write_attempts == 1
                           and self.initial is not None
                           and self.target == target_for(self.initial)
                           and self.initial != self.target
                           and not (self.initial ^ self.target) & ~change_mask
                           and packet == uint32_set_packet(self.pack, tid, self.target))
            else:
                allowed = False
            if not allowed:
                read.fail("indicator_packet_not_authorized")
            self.reserved_tx = None

        def parse_pending_response(self, plaintext):
            tid, _opcode, _body = read.parse_frame(plaintext)
            if tid == 1:
                return super().parse_pending_response(plaintext)
            kind = self.expected_responses.get(tid)
            if kind == "get":
                return parse_get_response(plaintext, tid)
            if kind == "set":
                return parse_set_response(plaintext, tid)
            read.fail("indicator_response_not_expected")

        async def request_reserved(self, kind):
            if self.failed:
                read.fail("indicator_control_already_failed")
            if self.reserved_tx is not None or self.expected_responses:
                read.fail("indicator_request_in_progress")
            if not self.authenticated or not self.f0_verified or not self.sent:
                read.fail("protocol_phase_invalid")
            if not self.started or self.next_counter > 3:
                read.fail("indicator_request_budget_invalid")
            tid, counter = self.next_tid, self.next_counter
            if kind == "get":
                packet = self.get_pkt(tid, [PROPERTY])
            elif kind == "set":
                if (self.write_attempts != 0 or self.initial is None
                        or self.set_authorization != self.target
                        or self.target != target_for(self.initial)
                        or self.initial == self.target
                        or (self.initial ^ self.target) & ~change_mask):
                    read.fail("indicator_set_not_authorized")
                self.set_authorization = None
                packet = uint32_set_packet(self.pack, tid, self.target)
                self.write_attempts = 1
                self.result["setting_write_attempts"] = 1
                self.result["maybe_executed"] = True
            else:
                read.fail("indicator_request_kind_invalid")
            # Consume identifiers before awaiting, including uncertain sends.
            self.next_tid += 1
            self.next_counter += 1
            self.reserved_tx = (packet, counter)
            self.expected_responses[tid] = kind
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
                read.fail("indicator_control_already_started")
            self.started = True
            try:
                self.result["phase"] = setting_name + "_initial_read"
                self.initial = await self.request_reserved("get")
                self.result["initial"] = self.initial
                self.target = target_for(self.initial)
                self.result["target"] = self.target
                if self.target != self.initial:
                    self.result["phase"] = setting_name + "_set"
                    self.set_authorization = self.target
                    await self.request_reserved("set")
                self.result["phase"] = setting_name + "_verify"
                observed = await self.request_reserved("get")
                self.result["observed"] = observed
                if observed != self.target:
                    read.fail("indicator_readback_mismatch")
                self.result.update({"changed": observed != self.initial, "verified": True,
                                    "status": setting_name + "_verified"})
            except BaseException:
                self.failed = True
                raise

    return SettingsClient


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
        factory = lambda upstream: make_settings_client(upstream, **options)
        result = asyncio.run(read.run_probe(key, client_factory=factory, operation=perform_control))
        key = b""
    except Exception as error:
        result = {"status": "error", "phase": "input_or_runtime",
                  "error_code": error.args[0] if isinstance(error, read.ProbeError) else "input_or_runtime_failed",
                  "error_class": type(error).__name__}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("status") in ("indicator_verified", "power_on_verified") and not result.get("cleanup_error_code") else 1


if __name__ == "__main__":
    raise SystemExit(main())

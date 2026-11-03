"""Absolute OFF only: fresh read, at most one False write, independent readback."""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import cycle_probe as wire
import read_probe as read


PROPERTY = (2, 1)


def off_set_packet(pack, tid):
    return wire.bool_set_packet(pack, tid, False)


def parse_get_response(packet, expected_tid):
    item = read.parse_read_response(packet, expected_tid)
    if item["typelen"] != 0x0001 or type(item["value"]) is not bool:
        read.fail("off_bool_typelen_required")
    return item["value"]


def make_off_client(upstream):
    ReadClient = read.make_client_class(upstream)

    class OffClient(ReadClient):
        def __init__(self, device, key, raw_client, result):
            super().__init__(device, key, raw_client, result)
            self.next_tid, self.next_counter = read.READ_TID, 1
            self.reserved_tx = None
            self.expected_responses = {}
            self.set_authorization = self.initial = None
            self.started = self.failed = self.completed = False
            self.write_attempts = 0
            result.update({"operation": "relay_off_only", "siid": 2, "piid": 1,
                           "target": False, "relay_writes": 0, "relay_write_attempts": 0,
                           "relay_write_count_semantics": "attempted_not_guaranteed_executed",
                           "maybe_executed": False, "changed": False, "verified": False})

        def authorize_send(self, packet, counter):
            if not self.authenticated:
                read.fail("protocol_phase_invalid")
            if not self.sent:
                if (packet, counter) != (self.pack(1, 0xF0), 0):
                    read.fail("off_f0_required")
                return
            if not self.f0_verified or self.reserved_tx != (packet, counter):
                read.fail("off_packet_not_authorized")
            tid, opcode, _body = read.parse_frame(packet)
            kind = self.expected_responses.get(tid)
            if kind in ("initial", "verify"):
                allowed = opcode == 2 and packet == self.get_pkt(tid, [PROPERTY])
            else:
                allowed = (kind == "off" and opcode == 0 and self.write_attempts == 1
                           and self.initial is True and packet == off_set_packet(self.pack, tid))
            if not allowed:
                read.fail("off_packet_not_authorized")
            self.reserved_tx = None

        def parse_pending_response(self, plaintext):
            tid, _opcode, _body = read.parse_frame(plaintext)
            if tid == 1:
                return super().parse_pending_response(plaintext)
            kind = self.expected_responses.get(tid)
            if kind in ("initial", "verify"):
                return parse_get_response(plaintext, tid)
            if kind == "off":
                return wire.parse_set_response(plaintext, tid)
            read.fail("off_response_not_expected")

        async def request_reserved(self, kind):
            if self.failed or self.completed:
                read.fail("off_control_already_finished")
            if self.reserved_tx is not None or self.expected_responses:
                read.fail("off_request_in_progress")
            if not self.authenticated or not self.f0_verified or not self.sent:
                read.fail("protocol_phase_invalid")
            if not self.started or self.next_counter > 3:
                read.fail("off_request_budget_invalid")
            tid, counter = self.next_tid, self.next_counter
            if kind == "initial":
                if counter != 1 or self.initial is not None:
                    read.fail("off_initial_read_not_authorized")
                packet = self.get_pkt(tid, [PROPERTY])
            elif kind == "off":
                if (self.initial is not True or self.write_attempts != 0 or counter != 2
                        or self.set_authorization is not False):
                    read.fail("off_set_not_authorized")
                self.set_authorization = None
                packet = off_set_packet(self.pack, tid)
                self.write_attempts = 1
                self.result.update(relay_writes=1, relay_write_attempts=1, maybe_executed=True)
            elif kind == "verify":
                if (type(self.initial) is not bool or self.write_attempts != int(self.initial)
                        or counter != 2 + int(self.initial)):
                    read.fail("off_verify_not_authorized")
                packet = self.get_pkt(tid, [PROPERTY])
            else:
                read.fail("off_request_kind_invalid")
            # Consume identifiers before awaiting, even if the send outcome is uncertain.
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
                read.fail("off_control_already_started")
            self.started = True
            try:
                self.result["phase"] = "off_initial_read"
                self.initial = await self.request_reserved("initial")
                self.result["initial"] = self.initial
                if self.initial:
                    self.result["phase"] = "off_set"
                    self.set_authorization = False
                    await self.request_reserved("off")
                self.result["phase"] = "off_verify"
                observed = await self.request_reserved("verify")
                self.result["observed"] = observed
                if observed is not False:
                    read.fail("off_readback_mismatch")
                self.completed = True
                self.result.update(changed=self.initial is True, verified=True, status="off_verified")
            except BaseException:
                self.failed = True
                raise

    return OffClient


async def perform_off(client, _result):
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
        key = read.validated_input(data)
        del raw_input, data
        result = asyncio.run(read.run_probe(key, client_factory=make_off_client, operation=perform_off))
        key = b""
    except Exception as error:
        result = {"status": "error", "phase": "input_or_runtime",
                  "error_code": error.args[0] if isinstance(error, read.ProbeError) else "input_or_runtime_failed",
                  "error_class": type(error).__name__}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("status") == "off_verified" and not result.get("cleanup_error_code") else 1


if __name__ == "__main__":
    raise SystemExit(main())

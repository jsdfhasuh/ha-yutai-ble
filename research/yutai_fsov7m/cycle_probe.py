"""One authorized empty-load switch cycle with bounded absolute restoration."""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import read_probe as read


def bool_set_packet(pack, tid, value):
    if type(value) is not bool:
        read.fail("cycle_bool_required")
    return pack(tid, 0, b"\1\2\1\0\1\0" + bytes([value]))


def parse_set_response(packet, expected_tid):
    tid, opcode, body = read.parse_frame(packet)
    if tid != expected_tid or opcode != 1:
        read.fail("set_response_mismatch")
    if len(body) != 6 or body[:4] != b"\1\2\1\0":
        read.fail("set_response_target_or_count_invalid")
    if body[4:] != b"\0\0":
        read.fail("set_response_status_failed")
    return {"siid": 2, "piid": 1, "status": 0}


def safe_error(error):
    return {"code": error.args[0] if isinstance(error, read.ProbeError) else (
        "cycle_cancelled" if isinstance(error, asyncio.CancelledError) else
        "cycle_timeout" if isinstance(error, TimeoutError) else "cycle_operation_failed"),
        "class": type(error).__name__}


def make_cycle_client(upstream):
    ReadClient = read.make_client_class(upstream)

    class CycleClient(ReadClient):
        def __init__(self, device, key, raw_client, result):
            super().__init__(device, key, raw_client, result)
            self.next_tid, self.next_counter = read.READ_TID, 1
            self.reserved_tx = None
            self.set_authorization = None
            self.expected_responses = {}
            self.initial_state = None
            self.test_attempted = self.restore_attempted = False
            self.write_attempts = 0
            result.update({"operation": "empty_load_cycle_and_restore", "relay_writes": 0,
                "relay_write_count_semantics": "attempted_not_guaranteed_executed",
                "maybe_executed": False, "switch_verified": False,
                "restore_verified": False, "restore_required": False})

        def authorize_send(self, packet, cnt):
            if not self.authenticated:
                read.fail("protocol_phase_invalid")
            if not self.sent:
                if (packet, cnt) != (self.pack(1, 0xF0), 0):
                    read.fail("cycle_f0_required")
                return
            if not self.f0_verified or self.reserved_tx != (packet, cnt):
                read.fail("cycle_packet_not_authorized")
            self.reserved_tx = None

        def parse_pending_response(self, plaintext):
            tid, _opcode, _body = read.parse_frame(plaintext)
            if tid == 1:
                return super().parse_pending_response(plaintext)
            kind = self.expected_responses.get(tid)
            if kind == "get":
                value = read.parse_read_response(plaintext, expected_tid=tid)
                if value["typelen"] != 0x0001 or value["type_code"] != 0:
                    read.fail("cycle_bool_typelen_required")
                return value
            if kind == "set":
                return parse_set_response(plaintext, tid)
            read.fail("cycle_response_not_expected")

        async def request_reserved(self, kind, value=None):
            if self.reserved_tx is not None:
                read.fail("cycle_request_already_reserved")
            # Consume identifiers before any await; uncertain sends never reuse a nonce.
            tid, counter = self.next_tid, self.next_counter
            self.next_tid += 1
            self.next_counter += 1
            if kind == "get":
                packet = self.get_pkt(tid, [(2, 1)])
            elif kind == "set":
                if self.set_authorization != (type(value), value) or not 1 <= self.write_attempts <= 2:
                    read.fail("cycle_set_not_authorized")
                self.set_authorization = None
                packet = bool_set_packet(self.pack, tid, value)
            else:
                read.fail("cycle_request_kind_invalid")
            self.reserved_tx = (packet, counter)
            self.expected_responses[tid] = kind
            try:
                return await self.exchange(packet, tid, counter)
            finally:
                self.reserved_tx = None
                self.expected_responses.pop(tid, None)

        async def get_current(self):
            response = await self.request_reserved("get")
            if response["typelen"] != 0x0001 or type(response["value"]) is not bool:
                read.fail("cycle_bool_typelen_required")
            return response["value"]

        async def set_cycle_state(self, role):
            if type(self.initial_state) is not bool or self.write_attempts >= 2:
                read.fail("cycle_set_budget_or_initial_state_invalid")
            if role == "test" and not self.test_attempted and not self.restore_attempted:
                self.test_attempted = True
                value = not self.initial_state
                self.result["maybe_executed"] = True
                self.result["restore_required"] = True
            elif role == "restore" and self.test_attempted and not self.restore_attempted:
                self.restore_attempted = True
                value = self.initial_state
            else:
                read.fail("cycle_set_role_or_repeat_invalid")
            self.write_attempts += 1
            self.result["relay_writes"] = self.write_attempts
            self.set_authorization = (bool, value)
            return await self.request_reserved("set", value)

        async def exercise(self):
            self.result["phase"] = "cycle_initial_read"
            self.initial_state = await self.get_current()
            self.result["initial_state"] = self.initial_state
            self.result["phase"] = "cycle_test_set"
            await self.set_cycle_state("test")
            self.result["phase"] = "cycle_test_verify"
            observed = await self.get_current()
            self.result["test_observed_state"] = observed
            if observed is not (not self.initial_state):
                read.fail("cycle_test_state_mismatch")
            self.result["switch_verified"] = True

        async def restore(self):
            self.result["phase"] = "cycle_restore_set"
            self.callback_error = None
            try:
                await self.set_cycle_state("restore")
            except Exception as error:
                self.result["restore_set_error"] = safe_error(error)
            self.result["phase"] = "cycle_restore_verify"
            self.callback_error = None
            try:
                observed = await self.get_current()
                self.result["final_observed_state"] = observed
                self.result["restore_verified"] = observed is self.initial_state
                if not self.result["restore_verified"]:
                    self.result["restore_error"] = {"code": "cycle_restore_state_mismatch"}
            except Exception as error:
                self.result["restore_error"] = safe_error(error)

        async def run_cycle(self):
            try:
                await asyncio.wait_for(self.exercise(), timeout=18)
            except (Exception, asyncio.CancelledError) as error:
                self.result["cycle_error"] = safe_error(error)
            finally:
                if self.result["maybe_executed"]:
                    recovery = asyncio.create_task(asyncio.wait_for(self.restore(), timeout=15))
                    while True:
                        try:
                            await asyncio.shield(recovery)
                            break
                        except asyncio.CancelledError as error:
                            self.result.setdefault("cycle_error", safe_error(error))
                            if recovery.cancelled():
                                self.result["restore_error"] = {"code": "cycle_restore_cancelled"}
                                break
                        except Exception as error:
                            self.result["restore_error"] = safe_error(error)
                            break
            if self.result["restore_verified"]:
                clean = self.result["switch_verified"] and not any(
                    name in self.result for name in ("cycle_error", "restore_set_error", "restore_error"))
                self.result["status"] = "cycle_verified_restored" if clean else "cycle_failed_restored"
            else:
                self.result["status"] = (
                    "cycle_failed_restore_unconfirmed" if self.result["maybe_executed"] else "cycle_failed_no_switch")

    return CycleClient


async def perform_cycle(client, _result):
    await client.run_cycle()


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
        result = asyncio.run(read.run_probe(key, client_factory=make_cycle_client, operation=perform_cycle))
        key = b""
    except Exception as error:
        result = {"status": "error", "phase": "input_or_runtime", **safe_error(error)}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("status") == "cycle_verified_restored" and not result.get("cleanup_error_code") else 1


if __name__ == "__main__":
    raise SystemExit(main())

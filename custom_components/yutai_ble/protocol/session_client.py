"""Guarded authenticated sessions for full reads and absolute relay commands."""

from __future__ import annotations

import asyncio
import time

import read_probe as read
import relay_control as relay
import settings_codec as codec
import settings_scan as scan
from device_identity import SUPPORTED_MODEL, validate_identity

MAX_COMMANDS = 64
SESSION_SECONDS = 300.0
ACK_SECONDS = 2.0
RELAY_PLAN = ((2, 1),)


def make_session_client(upstream):
    full_plan = scan.fixed_properties(codec.READ_PROPERTIES)
    if len(full_plan) != 13 or frozenset(full_plan) != relay.READ_SCOPE:
        read.fail("session_read_scope_invalid")
    ReadClient = read.make_client_class(upstream)

    class SessionClient(ReadClient):
        def __init__(self, device, key, raw_client, result, *, idle_timeout=90):
            try:
                address, model = validate_identity(result.get("address"), result.get("model"))
            except ValueError:
                read.fail("session_target_not_authorized")
            if (model != SUPPORTED_MODEL or getattr(device, "address", "").upper() != address
                    or type(idle_timeout) is not int or not 1 <= idle_timeout <= 120):
                read.fail("session_target_not_authorized")
            super().__init__(device, key, raw_client, result)
            self.address, self.model, self.idle_timeout = address, model, idle_timeout
            self.next_tid, self.next_counter = read.READ_TID, 1
            self.reserved_tx = None
            self.expected_responses = {}
            self.session_started_at = time.monotonic()
            self.last_completed_at = None
            self.permanently_failed = False
            self.active = False
            self.stage = "idle"
            self.requested = self.initial = None
            self.write_attempts = 0
            self.read_plan = ()
            self.read_index = 0

        def remaining_idle(self):
            now = time.monotonic()
            return max(0.0, min(
                self.session_started_at + SESSION_SECONDS - now,
                (self.last_completed_at or self.session_started_at) + self.idle_timeout - now,
            ))

        def reusable(self, command_budget):
            if self.active or self.next_counter + command_budget - 1 > MAX_COMMANDS:
                return False
            try:
                self._ready(check_idle=True)
            except read.ProbeError:
                return False
            return not (self.pending or self.expected_responses or self.reserved_tx is not None)

        def _ready(self, *, check_idle=False):
            if self.permanently_failed or self.closing:
                read.fail("session_unusable")
            if not self.c.raw.is_connected:
                read.fail("session_disconnected")
            if not self.authenticated or not self.f0_verified or not self.sent:
                read.fail("session_not_authenticated")
            now = time.monotonic()
            if now - self.session_started_at >= SESSION_SECONDS:
                read.fail("session_lifetime_expired")
            if (check_idle and self.last_completed_at is not None
                    and now - self.last_completed_at >= self.idle_timeout):
                read.fail("session_idle_expired")
            self.check_callbacks()

        def _ack_done(self, task):
            if task.cancelled() and not self.closing:
                self.callback_error = "transport_ack_cancelled"
            super()._ack_done(task)

        async def _drain_acks(self):
            async def drain():
                while self.ack_tasks:
                    outcomes = await asyncio.gather(*tuple(self.ack_tasks), return_exceptions=True)
                    if any(isinstance(item, BaseException) for item in outcomes):
                        read.fail("transport_ack_failed")
                    await asyncio.sleep(0)
            await asyncio.wait_for(drain(), ACK_SECONDS)
            self._ready()
            if self.pending or self.expected_responses or self.reserved_tx is not None:
                read.fail("session_request_in_progress")

        def authorize_send(self, packet, counter):
            if self.permanently_failed or self.closing or not self.authenticated:
                read.fail("session_unusable")
            if not self.sent:
                if (packet, counter) != (self.pack(1, 0xF0), 0) or self.next_counter != 1:
                    read.fail("session_f0_required")
                return
            if (not self.active or not self.f0_verified or self.reserved_tx != (packet, counter)
                    or counter != self.next_counter - 1 or not 1 <= counter <= MAX_COMMANDS):
                read.fail("session_packet_not_authorized")
            tid, opcode, _body = read.parse_frame(packet)
            pending = self.expected_responses.get(tid)
            allowed = False
            if pending is not None and pending[0] == "get":
                allowed = opcode == 2 and packet == self.get_pkt(tid, pending[1])
            elif pending == ("set", relay.PROPERTY):
                allowed = (self.stage == "set" and self.write_attempts == 1
                           and type(self.initial) is bool and self.initial is not self.requested
                           and opcode == 0
                           and packet == relay.bool_set_packet(self.pack, tid, self.requested))
            if not allowed:
                read.fail("session_packet_not_authorized")
            self.reserved_tx = None

        def parse_pending_response(self, plaintext):
            tid, _opcode, _body = read.parse_frame(plaintext)
            if tid == 1 and not self.f0_verified:
                return super().parse_pending_response(plaintext)
            pending = self.expected_responses.get(tid)
            if pending is not None and pending[0] == "get":
                items = scan.parse_read_response(plaintext, tid, pending[1])
                relay.snapshot_values(items, pending[1])
                return items
            if pending == ("set", relay.PROPERTY):
                return relay.parse_set_response(plaintext, tid)
            read.fail("session_response_not_expected")

        async def _request(self, kind):
            self._ready()
            if not self.active or self.reserved_tx is not None or self.expected_responses or self.pending:
                read.fail("session_request_in_progress")
            if self.next_counter > MAX_COMMANDS or self.next_tid > 0xFFFF:
                read.fail("session_command_budget_exhausted")
            tid, counter = self.next_tid, self.next_counter
            if kind == "get":
                if self.stage not in ("initial", "verify", "read") or self.read_index >= len(self.read_plan):
                    read.fail("session_read_not_authorized")
                batch = self.read_plan[self.read_index:self.read_index + scan.MAX_BATCH]
                if self.stage == "initial" and batch != RELAY_PLAN:
                    read.fail("session_initial_scope_invalid")
                packet, pending = self.get_pkt(tid, batch), ("get", batch)
                self.read_index += len(batch)
            elif kind == "set":
                if (self.stage != "set" or type(self.initial) is not bool
                        or self.initial is self.requested or self.write_attempts):
                    read.fail("session_set_not_authorized")
                packet = relay.bool_set_packet(self.pack, tid, self.requested)
                pending = ("set", relay.PROPERTY)
                self.write_attempts = 1
                self.result.update(relay_writes=1, relay_write_attempts=1, maybe_executed=True)
            else:
                read.fail("session_request_kind_invalid")
            # Consume both identifiers before awaiting; a failed write is never replayed.
            self.next_tid += 1
            self.next_counter += 1
            self.reserved_tx = (packet, counter)
            self.expected_responses[tid] = pending
            try:
                return await self.exchange(packet, tid, counter)
            except BaseException:
                self.permanently_failed = True
                raise
            finally:
                self.reserved_tx = None
                self.expected_responses.pop(tid, None)

        async def _read(self, stage, plan):
            self.stage, self.read_plan, self.read_index = stage, plan, 0
            properties = []
            while self.read_index < len(plan):
                properties.extend(await self._request("get"))
            return properties, relay.snapshot_values(properties, plan)

        async def operate(self, operation, value=None):
            if operation == "relay":
                relay.relay_value(value)
            elif operation != "read" or value is not None:
                read.fail("session_operation_invalid")
            if self.active:
                read.fail("session_operation_in_progress")
            self.active = True
            self.requested, self.initial, self.write_attempts = value, None, 0
            self.result.update(relay_writes=0, relay_write_attempts=0, maybe_executed=False)
            started = time.monotonic()
            first_counter = self.next_counter
            try:
                self._ready(check_idle=True)
                budget = 3 if operation == "relay" else 4
                if (self.pending or self.expected_responses or self.reserved_tx is not None):
                    read.fail("session_request_in_progress")
                if self.next_counter + budget - 1 > MAX_COMMANDS:
                    read.fail("session_command_budget_exhausted")
                if operation == "relay":
                    self.result["phase"] = "session_initial_relay"
                    _properties, values = await self._read("initial", RELAY_PLAN)
                    self.initial = values[relay.PROPERTY]
                    if self.initial is not value:
                        self.result["phase"], self.stage = "session_relay_set", "set"
                        await self._request("set")
                    self.result["phase"] = "session_verify"
                    properties, values = await self._read("verify", RELAY_PLAN)
                    if values[relay.PROPERTY] is not value:
                        read.fail("session_relay_readback_mismatch")
                    report = {
                        "status": "relay_session_verified", "operation": "relay_control",
                        "verification_scope": "relay_only", "verified": True,
                        "requested_value": value, "initial_relay_state": self.initial,
                        "observed_relay_state": values[relay.PROPERTY],
                        "initial_relay_verified": True, "settings_unchanged": None,
                    }
                else:
                    self.result["phase"] = "session_read"
                    properties, _values = await self._read("read", full_plan)
                    settings, capabilities = codec.decode_settings(properties)
                    report = {"status": "read_session_verified", "operation": "read",
                              "verification_scope": "full", "settings": settings,
                              "capabilities": capabilities}
                await self._drain_acks()
                self.last_completed_at = time.monotonic()
                self.stage = "idle"
                report.update(properties=properties, relay_writes=self.write_attempts,
                              relay_write_attempts=self.write_attempts,
                              maybe_executed=bool(self.write_attempts),
                              command_count=self.next_counter - first_counter,
                              operation_elapsed_seconds=round(self.last_completed_at - started, 6))
                return report
            except BaseException:
                self.permanently_failed = True
                self.stage = "failed"
                raise
            finally:
                self.active = False

    return SessionClient

"""Absolute relay control: one bool SET between complete independent snapshots."""

from __future__ import annotations

import read_probe as read
import settings_codec as codec
import settings_scan as scan


PROPERTY = (2, 1)
READ_SCOPE = frozenset(((2, 1), (4, 2), (4, 3), (4, 4),
                        *((5, piid) for piid in range(1, 8)), (7, 1), (7, 2)))
BOOL_PROPERTIES = frozenset(((2, 1), (7, 1), (7, 2)))
SETTING_PROPERTIES = tuple((5, piid) for piid in range(1, 8))


def relay_value(value):
    if type(value) is not bool:
        read.fail("relay_bool_required")
    return value


def bool_set_packet(pack, tid, value):
    return pack(tid, 0, b"\1\2\1\0\1\0" + bytes((int(relay_value(value)),)))


def parse_set_response(packet, expected_tid):
    tid, opcode, body = read.parse_frame(packet)
    if tid != expected_tid or opcode != 1 or body != b"\1\2\1\0\0\0":
        read.fail("relay_set_response_invalid")
    return {"siid": 2, "piid": 1, "status": 0}


def snapshot_values(items, expected):
    if not isinstance(items, list) or len(items) != len(expected):
        read.fail("relay_snapshot_invalid")
    values = {}
    for item, identity in zip(items, expected):
        if (not isinstance(item, dict) or (item.get("siid"), item.get("piid")) != identity
                or type(item.get("status")) is not int or item["status"] != 0):
            read.fail("relay_snapshot_property_invalid")
        value = item.get("value")
        if identity in BOOL_PROPERTIES:
            if (type(value) is not bool or item.get("typelen") != 1
                    or item.get("type_name") != "bool"
                    or item.get("raw_hex") != ("01" if value else "00")):
                read.fail("relay_snapshot_type_invalid")
        elif (type(value) is not int or not 0 <= value <= 0xFFFFFFFF
              or item.get("typelen") != 0x5004 or item.get("type_name") != "uint32"
              or item.get("raw_hex") != value.to_bytes(4, "little").hex()):
            read.fail("relay_snapshot_type_invalid")
        values[identity] = value
    return values


def make_relay_client(upstream, value, *, codec_module=None):
    selected_codec = codec if codec_module is None else codec_module
    value = relay_value(value)
    plan = scan.fixed_properties(selected_codec.READ_PROPERTIES)
    if len(plan) != len(READ_SCOPE) or frozenset(plan) != READ_SCOPE:
        read.fail("relay_read_scope_invalid")
    batch_count = (len(plan) + scan.MAX_BATCH - 1) // scan.MAX_BATCH
    ReadClient = read.make_client_class(upstream)

    class RelayClient(ReadClient):
        def __init__(self, device, key, raw_client, result):
            super().__init__(device, key, raw_client, result)
            self.next_tid, self.next_counter = read.READ_TID, 1
            self.reserved_tx = None
            self.expected_responses = {}
            self.snapshot_index = 0
            self.stage = "idle"
            self.started = self.failed = self.completed = False
            self.write_attempts = 0
            self.set_authorization = self.initial = self.initial_values = None
            result.pop("siid", None)
            result.pop("piid", None)
            result.update(operation="relay_control", requested_value=value,
                          relay_writes=0, relay_write_attempts=0,
                          relay_write_count_semantics="attempted_not_guaranteed_executed",
                          maybe_executed=False, changed=False, verified=False,
                          initial_relay_state=None, observed_relay_state=None,
                          initial_snapshot_complete=False, verification_snapshot_complete=False,
                          settings_unchanged=None, properties=[])

        def authorize_send(self, packet, counter):
            if not self.authenticated:
                read.fail("protocol_phase_invalid")
            if not self.sent:
                if (packet, counter) != (self.pack(1, 0xF0), 0):
                    read.fail("relay_f0_required")
                return
            if not self.f0_verified or self.reserved_tx != (packet, counter):
                read.fail("relay_packet_not_authorized")
            tid, opcode, _body = read.parse_frame(packet)
            pending = self.expected_responses.get(tid)
            allowed = False
            if pending is not None and pending[0] == "get":
                allowed = opcode == 2 and packet == self.get_pkt(tid, pending[1])
            elif pending == ("set", PROPERTY):
                allowed = (opcode == 0 and self.write_attempts == 1
                           and type(self.initial) is bool and self.initial is not value
                           and self.initial_values is not None
                           and packet == bool_set_packet(self.pack, tid, value))
            if not allowed:
                read.fail("relay_packet_not_authorized")
            self.reserved_tx = None

        def parse_pending_response(self, plaintext):
            tid, _opcode, _body = read.parse_frame(plaintext)
            if tid == 1:
                return super().parse_pending_response(plaintext)
            pending = self.expected_responses.get(tid)
            if pending is not None and pending[0] == "get":
                items = scan.parse_read_response(plaintext, tid, pending[1])
                snapshot_values(items, pending[1])
                return items
            if pending == ("set", PROPERTY):
                return parse_set_response(plaintext, tid)
            read.fail("relay_response_not_expected")

        async def request_reserved(self, kind):
            if self.failed or self.completed or not self.started:
                read.fail("relay_control_not_active")
            if self.reserved_tx is not None or self.expected_responses:
                read.fail("relay_request_in_progress")
            if not self.authenticated or not self.f0_verified or not self.sent:
                read.fail("protocol_phase_invalid")
            if self.next_counter > 2 * batch_count + 1:
                read.fail("relay_request_budget_invalid")
            tid, counter = self.next_tid, self.next_counter
            if kind == "get":
                if self.stage not in ("initial", "verify") or self.snapshot_index >= len(plan):
                    read.fail("relay_snapshot_not_active")
                offset = 0 if self.stage == "initial" else batch_count + self.write_attempts
                if counter != offset + 1 + self.snapshot_index // scan.MAX_BATCH:
                    read.fail("relay_snapshot_sequence_invalid")
                batch = plan[self.snapshot_index:self.snapshot_index + scan.MAX_BATCH]
                packet, pending = self.get_pkt(tid, batch), ("get", batch)
                self.snapshot_index += len(batch)
            elif kind == "set":
                if (self.stage != "set" or counter != batch_count + 1
                        or self.initial_values is None or type(self.initial) is not bool
                        or self.initial is value or self.write_attempts != 0
                        or self.set_authorization is not value):
                    read.fail("relay_set_not_authorized")
                packet, pending = bool_set_packet(self.pack, tid, value), ("set", PROPERTY)
                self.set_authorization = None
                self.write_attempts = 1
                self.result.update(relay_writes=1, relay_write_attempts=1, maybe_executed=True)
            else:
                read.fail("relay_request_kind_invalid")
            # Consume identifiers before awaiting; uncertain sends are never retried.
            self.next_tid += 1
            self.next_counter += 1
            self.reserved_tx = (packet, counter)
            self.expected_responses[tid] = pending
            try:
                return await self.exchange(packet, tid, counter)
            except BaseException:
                self.failed = True
                raise
            finally:
                self.reserved_tx = None
                self.expected_responses.pop(tid, None)

        async def read_snapshot(self, stage):
            if stage not in ("initial", "verify"):
                read.fail("relay_snapshot_stage_invalid")
            self.stage, self.snapshot_index = stage, 0
            items = []
            while self.snapshot_index < len(plan):
                items.extend(await self.request_reserved("get"))
            return items, snapshot_values(items, plan)

        async def control(self):
            if self.started:
                read.fail("relay_control_already_started")
            self.started = True
            try:
                self.result["phase"] = "relay_initial_snapshot"
                _properties, self.initial_values = await self.read_snapshot("initial")
                self.initial = self.initial_values[PROPERTY]
                self.result.update(initial_relay_state=self.initial, initial_snapshot_complete=True)
                if self.initial is not value:
                    self.stage = "set"
                    self.result["phase"] = "relay_set"
                    self.set_authorization = value
                    await self.request_reserved("set")
                self.result["phase"] = "relay_verify_snapshot"
                properties, observed_values = await self.read_snapshot("verify")
                observed = observed_values[PROPERTY]
                unchanged = all(observed_values[prop] == self.initial_values[prop]
                                for prop in SETTING_PROPERTIES)
                self.result.update(properties=properties, observed_relay_state=observed,
                                   verification_snapshot_complete=True, settings_unchanged=unchanged)
                if observed is not value:
                    read.fail("relay_readback_mismatch")
                if not unchanged:
                    # Concurrent setting changes are not attributed or restored.
                    read.fail("relay_settings_changed")
                settings, capabilities = selected_codec.decode_settings(properties)
                self.completed = True
                self.stage = "complete"
                self.result.update(settings=settings, capabilities=capabilities,
                                   changed=self.initial is not observed,
                                   verified=True, status="relay_verified")
            except BaseException:
                self.failed = True
                raise

    return RelayClient


async def perform_relay(client, _result):
    await client.control()

"""One codec-authorized uint32 setting write between two complete snapshots."""

from __future__ import annotations

import read_probe as read
import settings_codec as codec
import settings_scan as scan


TYPELEN = 0x5004
UINT32_MAX = 0xFFFFFFFF
WRITE_PROPERTIES = frozenset(((5, 4), (5, 5), (5, 6)))
READ_SCOPE = frozenset(((2, 1), (4, 2), (4, 3), (4, 4),
                        *((5, piid) for piid in range(1, 8)), (7, 1), (7, 2)))
BOOL_PROPERTIES = frozenset(((2, 1), (7, 1), (7, 2)))


def uint32(value):
    if type(value) is not int or not 0 <= value <= UINT32_MAX:
        read.fail("setting_uint32_required")
    return value


def write_property(value):
    if (type(value) is not tuple or len(value) != 2
            or any(type(part) is not int for part in value)
            or value not in WRITE_PROPERTIES):
        read.fail("setting_property_not_authorized")
    return value


def uint32_set_packet(pack, tid, property_id, value):
    siid, piid = write_property(property_id)
    return pack(tid, 0, bytes((1, siid)) + piid.to_bytes(2, "little")
                + TYPELEN.to_bytes(2, "little") + uint32(value).to_bytes(4, "little"))


def parse_set_response(packet, expected_tid, property_id):
    siid, piid = write_property(property_id)
    tid, opcode, body = read.parse_frame(packet)
    expected = bytes((1, siid)) + piid.to_bytes(2, "little") + b"\0\0"
    if tid != expected_tid or opcode != 1 or body != expected:
        read.fail("setting_set_response_invalid")
    return {"siid": siid, "piid": piid, "status": 0}


def snapshot_word(properties, property_id):
    matching = [item for item in properties if (item["siid"], item["piid"]) == property_id]
    if len(matching) != 1:
        read.fail("setting_snapshot_property_invalid")
    item = matching[0]
    if item.get("status") != 0 or item.get("typelen") != TYPELEN:
        read.fail("setting_snapshot_type_invalid")
    return uint32(item.get("value"))


def snapshot_relay_state(properties):
    matching = [item for item in properties if (item["siid"], item["piid"]) == (2, 1)]
    if len(matching) != 1:
        read.fail("setting_snapshot_property_invalid")
    item = matching[0]
    if (type(item.get("status")) is not int or item["status"] != 0
            or item.get("typelen") != 1 or type(item.get("value")) is not bool):
        read.fail("setting_snapshot_type_invalid")
    return item["value"]


def validate_snapshot_items(items):
    for item in items:
        if item["status"] != 0:
            read.fail("setting_snapshot_status_failed")
        property_id = item["siid"], item["piid"]
        if property_id in BOOL_PROPERTIES:
            if item.get("typelen") != 1 or type(item.get("value")) is not bool:
                read.fail("setting_snapshot_type_invalid")
        elif item.get("typelen") != TYPELEN:
            read.fail("setting_snapshot_type_invalid")
        else:
            uint32(item.get("value"))


def make_control_client(upstream, field, value):
    property_id, mask = codec.validate_setting(field, value)
    property_id = write_property(property_id)
    if uint32(mask) == 0:
        read.fail("setting_change_mask_invalid")
    plan = scan.fixed_properties(codec.READ_PROPERTIES)
    if len(plan) != len(READ_SCOPE) or frozenset(plan) != READ_SCOPE:
        read.fail("setting_read_scope_invalid")
    batch_count = (len(plan) + scan.MAX_BATCH - 1) // scan.MAX_BATCH
    ReadClient = read.make_client_class(upstream)

    def target_for(initial):
        target = uint32(codec.target_word(field, value, uint32(initial)))
        if (initial ^ target) & ~mask:
            read.fail("setting_non_target_bits_changed")
        return target

    class ControlClient(ReadClient):
        def __init__(self, device, key, raw_client, result):
            super().__init__(device, key, raw_client, result)
            self.next_tid, self.next_counter = read.READ_TID, 1
            self.reserved_tx = None
            self.expected_responses = {}
            self.snapshot_index = 0
            self.stage = "idle"
            self.started = self.failed = self.completed = False
            self.write_attempts = 0
            self.set_authorization = None
            self.initial = self.target = None
            self.initial_properties = None
            result.pop("siid", None)
            result.pop("piid", None)
            result.update(operation="set_observed_setting", field=field,
                          requested_value=value, setting_property=list(property_id),
                          change_mask=mask, relay_writes=0, setting_write_attempts=0,
                          maybe_executed=False, changed=False, verified=False,
                          initial=None, target=None, observed=None, properties=[],
                          initial_relay_state=None, observed_relay_state=None,
                          relay_state_unchanged=None,
                          initial_snapshot_complete=False, verification_snapshot_complete=False)

        def authorize_send(self, packet, counter):
            if not self.authenticated:
                read.fail("protocol_phase_invalid")
            if not self.sent:
                if (packet, counter) != (self.pack(1, 0xF0), 0):
                    read.fail("setting_f0_required")
                return
            if not self.f0_verified or self.reserved_tx != (packet, counter):
                read.fail("setting_packet_not_authorized")
            tid, opcode, _body = read.parse_frame(packet)
            pending = self.expected_responses.get(tid)
            allowed = False
            if pending is not None and pending[0] == "get":
                allowed = opcode == 2 and packet == self.get_pkt(tid, pending[1])
            elif pending == ("set", property_id):
                allowed = (opcode == 0 and self.write_attempts == 1
                           and self.initial is not None and self.initial != self.target
                           and self.target == target_for(self.initial)
                           and packet == uint32_set_packet(self.pack, tid, property_id, self.target))
            if not allowed:
                read.fail("setting_packet_not_authorized")
            self.reserved_tx = None

        def parse_pending_response(self, plaintext):
            tid, _opcode, _body = read.parse_frame(plaintext)
            if tid == 1:
                return super().parse_pending_response(plaintext)
            pending = self.expected_responses.get(tid)
            if pending is not None and pending[0] == "get":
                items = scan.parse_read_response(plaintext, tid, pending[1])
                validate_snapshot_items(items)
                return items
            if pending == ("set", property_id):
                return parse_set_response(plaintext, tid, property_id)
            read.fail("setting_response_not_expected")

        async def request_reserved(self, kind):
            if self.failed or self.completed or not self.started:
                read.fail("setting_control_not_active")
            if self.reserved_tx is not None or self.expected_responses:
                read.fail("setting_request_in_progress")
            if not self.authenticated or not self.f0_verified or not self.sent:
                read.fail("protocol_phase_invalid")
            if self.next_counter > 2 * batch_count + 1:
                read.fail("setting_request_budget_invalid")
            tid, counter = self.next_tid, self.next_counter
            if kind == "get":
                if self.stage not in ("initial", "verify") or self.snapshot_index >= len(plan):
                    read.fail("setting_snapshot_not_active")
                offset = 0 if self.stage == "initial" else batch_count + self.write_attempts
                if counter != offset + 1 + self.snapshot_index // scan.MAX_BATCH:
                    read.fail("setting_snapshot_sequence_invalid")
                batch = plan[self.snapshot_index:self.snapshot_index + scan.MAX_BATCH]
                packet = self.get_pkt(tid, batch)
                pending = ("get", batch)
                self.snapshot_index += len(batch)
            elif kind == "set":
                if (self.stage != "set" or counter != batch_count + 1
                        or self.initial_properties is None or self.write_attempts != 0
                        or self.initial == self.target or self.set_authorization != self.target
                        or self.target != target_for(self.initial)):
                    read.fail("setting_set_not_authorized")
                packet = uint32_set_packet(self.pack, tid, property_id, self.target)
                pending = ("set", property_id)
                self.set_authorization = None
                self.write_attempts = 1
                self.result.update(setting_write_attempts=1, maybe_executed=True)
            else:
                read.fail("setting_request_kind_invalid")
            # Consume identifiers before awaiting; uncertain sends never reuse a nonce.
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
                read.fail("setting_snapshot_stage_invalid")
            self.stage, self.snapshot_index = stage, 0
            items = []
            while self.snapshot_index < len(plan):
                items.extend(await self.request_reserved("get"))
            return items

        async def control(self):
            if self.started:
                read.fail("setting_control_already_started")
            self.started = True
            try:
                self.result["phase"] = "setting_initial_snapshot"
                self.initial_properties = await self.read_snapshot("initial")
                self.initial = snapshot_word(self.initial_properties, property_id)
                self.target = target_for(self.initial)
                self.result.update(initial=self.initial, target=self.target,
                                   initial_relay_state=snapshot_relay_state(self.initial_properties),
                                   initial_snapshot_complete=True)
                if self.target != self.initial:
                    self.stage = "set"
                    self.result["phase"] = "setting_set"
                    self.set_authorization = self.target
                    await self.request_reserved("set")
                self.result["phase"] = "setting_verify_snapshot"
                properties = await self.read_snapshot("verify")
                observed = snapshot_word(properties, property_id)
                observed_relay_state = snapshot_relay_state(properties)
                relay_unchanged = observed_relay_state == self.result["initial_relay_state"]
                self.result.update(properties=properties, observed=observed,
                                   observed_relay_state=observed_relay_state,
                                   relay_state_unchanged=relay_unchanged,
                                   verification_snapshot_complete=True)
                if not relay_unchanged:
                    # A concurrent external change cannot be attributed or safely restored.
                    self.result["relay_state_change_cause"] = "unknown"
                    read.fail("setting_relay_state_changed")
                if observed != self.target:
                    read.fail("setting_readback_mismatch")
                settings, capabilities = codec.decode_settings(properties)
                if (not isinstance(settings, dict) or not isinstance(capabilities, dict)
                        or field not in settings or type(settings[field]) is not type(value)
                        or settings[field] != value or field not in capabilities):
                    read.fail("setting_decoded_value_mismatch")
                self.completed = True
                self.stage = "complete"
                self.result.update(settings=settings, capabilities=capabilities,
                                   changed=observed != self.initial,
                                   verified=True, status="setting_verified")
            except BaseException:
                self.failed = True
                raise

    return ControlClient


async def perform_control(client, _result):
    await client.control()

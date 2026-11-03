"""Read-only candidate MIoT protocol validation for the new fsov8m plug."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import re
import sys
import zlib

from device_identity import Target, validate_identity

READ_TID = 0x7201
WORK_SECONDS = 62
CLEANUP_SECONDS = 8
VENDOR_HASHES = {
    "channel.py": "31b4804e20ea771c833193fc213ab6835386703a5bd857f16824705b460a490f",
    "miot_ble_specv2.py": "42220ea35bf9c84120a413f9c77af870eec082d16556149fe20a74b1f9ed095e",
    "LICENSE": "43f216b99fe98dc2910cac37ef1a5c9233662538fd01efc2e6b83b8e7b43dd7b",
}


class ProbeError(Exception):
    """Fixed error codes only; never retain protocol payloads in exceptions."""


def fail(code):
    raise ProbeError(code)


def validated_input(data):
    if not isinstance(data, dict) or set(data) != {"address", "model", "gatt_ltmk"}:
        fail("input_fields_invalid")
    try:
        address, model = validate_identity(data["address"], data["model"])
    except ValueError:
        fail("target_not_authorized")
    key = data["gatt_ltmk"]
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        fail("key_format_invalid")
    return Target(address, model, bytes.fromhex(key))


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("input_duplicate_key")
        result[key] = value
    return result


def parse_frame(packet):
    if len(packet) < 5:
        fail("frame_truncated")
    header = int.from_bytes(packet[:2], "little")
    if header >> 12 != 2 or header & 0xFFF != len(packet):
        fail("frame_header_invalid")
    return int.from_bytes(packet[2:4], "little"), packet[4], packet[5:]


def parse_read_response(packet, expected_tid=READ_TID):
    tid, opcode, body = parse_frame(packet)
    if tid != expected_tid or opcode != 3:
        fail("read_response_mismatch")
    if len(body) < 6 or body[0] != 1:
        fail("property_count_invalid")
    if body[1] != 2 or int.from_bytes(body[2:4], "little") != 1:
        fail("property_target_mismatch")
    if int.from_bytes(body[4:6], "little") != 0:
        fail("property_status_failed")
    if len(body) != 9:
        fail("property_size_invalid")
    typelen = int.from_bytes(body[6:8], "little")
    if typelen & 0xFFF != 1 or body[8] not in (0, 1):
        fail("property_value_invalid")
    return {
        "siid": 2, "piid": 1, "status": 0,
        "value": bool(body[8]), "typelen": typelen,
        "typelen_hex": "%04x" % typelen,
        "type_code": typelen >> 12, "value_length": 1,
    }


def load_vendor():
    root = Path(__file__).resolve().parent / "vendor"
    for name, expected in VENDOR_HASHES.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            fail("vendor_hash_mismatch")
    # The pinned source imports channel by its original name.
    for name, filename in (("channel", "channel.py"), ("_audited_miot", "miot_ble_specv2.py")):
        spec = importlib.util.spec_from_file_location(name, root / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules["_audited_miot"]


def make_client_class(upstream):
    """Reuse pinned cryptography/framing, replace every command/lifecycle path."""
    auth = "00000016-0000-1000-8000-00805f9b34fb"

    class GuardedTransport:
        def __init__(self, raw, owner):
            self.raw = raw
            self.owner = owner
            self.mcu_index = 0
            self.ctrl_index = 0
            self.auth_payload = None
            self.auth_headers = set()
            self.command_writes = []

        @property
        def is_connected(self):
            return self.raw.is_connected

        async def connect(self):
            await self.raw.connect()

        async def disconnect(self):
            await self.raw.disconnect()

        async def start_notify(self, uuid, callback):
            if uuid not in (upstream.CH_CTRL, auth, upstream.CMD, upstream.DAT, upstream.MCU):
                fail("notification_not_allowed")
            if uuid == upstream.CH_CTRL:
                def on_control(sender, data):
                    value = bytes(data)
                    self.owner.record_control_frame(value)
                    if value == b"\x51\0\0\0":
                        self.owner.authenticated = True
                        self.owner.auth_event.set()
                    elif value[:1] in (b"\x51", b"\x52"):
                        self.owner.callback_error = "authentication_rejected_or_invalid"
                        self.owner.auth_event.set()
                    callback(sender, data)
                await self.raw.start_notify(uuid, on_control)
            elif uuid == auth:
                def on_auth(sender, data):
                    self.owner.record_auth_frame(bytes(data))
                    callback(sender, data)
                await self.raw.start_notify(uuid, on_auth)
            else:
                await self.raw.start_notify(uuid, callback)

        async def write_gatt_char(self, uuid, data, response=False):
            packet = bytes(data)
            allowed = False
            if response is not False:
                fail("write_mode_not_allowed")
            if uuid == upstream.MCU:
                sequence = (b"\0", b"\x08\x01\0", b"\x03")
                if self.mcu_index < len(sequence) and packet == sequence[self.mcu_index]:
                    self.mcu_index += 1
                    allowed = True
            elif uuid == upstream.CH_CTRL:
                sequence = (b"\xa4", b"\x50\0\0\0")
                if self.ctrl_index < len(sequence) and packet == sequence[self.ctrl_index]:
                    if self.ctrl_index == 1 and not self.owner.mng1_ack_sent.is_set():
                        fail("negotiation_not_complete")
                    if self.ctrl_index == 0:
                        self.owner.mng1_ack_sent.clear()
                    self.ctrl_index += 1
                    allowed = True
            elif uuid == auth:
                # Only channel negotiation, acknowledgements, public key and proof.
                if packet == b"\0\0\x03\0":
                    allowed = True
                elif len(packet) == 6 and packet[:4] == b"\0\0\x05\0" and packet[4] == 6:
                    allowed = True
                elif 4 <= len(packet) <= 244 and packet[:4] == b"\0\0\x05\x01":
                    allowed = True
                elif len(packet) == 6 and packet[:3] == b"\0\0\x01" and packet[4:] == b"\x01\x01":
                    allowed = True
                elif packet in (b"\0\0\0\x03\x01\0", b"\0\0\0\x05\x01\0"):
                    kind = packet[3]
                    if kind not in self.auth_headers and self.auth_payload is None:
                        self.auth_headers.add(kind)
                        self.auth_payload = 66 if kind == 3 else 10
                        self.owner.auth_ready.clear()
                        self.owner.auth_complete.clear()
                        allowed = True
                elif self.auth_payload is not None and len(packet) == self.auth_payload and packet[:2] == b"\x01\0":
                    if not self.owner.auth_ready.is_set():
                        fail("auth_receiver_not_ready")
                    self.auth_payload = None
                    allowed = True
            elif uuid == upstream.DAT:
                allowed = packet == b"\0\0\x03\0"
            elif uuid == upstream.CMD and self.command_writes:
                allowed = packet == self.command_writes[0]
                if allowed:
                    self.command_writes.pop(0)
            if not allowed:
                fail("gatt_write_not_allowed")
            size = self.owner.handshake.get("effective_auth_write_capacity")
            if uuid == auth and isinstance(size, int) and len(packet) > size:
                fail("auth_frame_exceeds_capacity")
            await self.raw.write_gatt_char(uuid, packet, response=False)
            if uuid == auth and packet[:4] == b"\0\0\5\1":
                self.owner.handshake["mng1_ack_sent"] = True
                self.owner.mng1_ack_sent.set()

    class ReadClient(upstream.Client):
        def __init__(self, device, key, raw_client, result):
            super().__init__(device, key, log=lambda _message: None)
            self.c = GuardedTransport(raw_client, self)
            self.result = result
            self.auth_event = asyncio.Event()
            self.command_ready = asyncio.Event()
            self.authenticated = False
            self.f0_verified = False
            self.callback_error = None
            self.ack_tasks = set()
            self.sent = []
            self.closing = False
            self.public_key_window = False
            self.device_public_keys = set()
            self.public_key_event = asyncio.Event()
            self.mng1_ack_sent = asyncio.Event()
            self.auth_ready = asyncio.Event()
            self.auth_complete = asyncio.Event()
            self.handshake = result.setdefault("handshake", {
                "control_frames": {}, "auth_frames": {}, "public_key_ctr_announcements": {},
                "public_key_frames": 0, "public_key_distinct": 0,
            })

        def record_control_frame(self, packet):
            kind = "other"
            if packet == b"\x51\0\0\0":
                kind = "login_success_0x51"
            elif packet[:1] == b"\x51":
                kind = "invalid_login_success_0x51"
            elif packet[:1] == b"\x52":
                kind = "login_rejected_0x52"
            label = "%s:length_%d" % (kind, len(packet))
            counts = self.handshake["control_frames"]
            counts[label] = counts.get(label, 0) + 1

        def record_auth_frame(self, packet):
            kind = "data_or_unknown"
            if len(packet) >= 4 and packet[:2] == b"\0\0":
                kind = {0: "ctr", 1: "ack", 2: "single_ctr", 3: "single_ack",
                        4: "mng", 5: "mng_ack"}.get(packet[2], "unknown_control")
            elif len(packet) >= 2 and packet[:2] != b"\0\0":
                kind = "sequence_or_unknown"
            label = "%s:length_%d" % (kind, len(packet))
            counts = self.handshake["auth_frames"]
            counts[label] = counts.get(label, 0) + 1
            if packet == b"\0\0\1\1":
                self.auth_ready.set()
            elif packet == b"\0\0\1\0":
                self.auth_complete.set()
                self.public_key_event.set()
            if len(packet) == 6 and packet[:4] == b"\0\0\0\3":
                frame_count = str(int.from_bytes(packet[4:6], "little"))
                counts = self.handshake["public_key_ctr_announcements"]
                counts[frame_count] = counts.get(frame_count, 0) + 1
            if self.public_key_window and len(packet) == 68 and packet[:4] == b"\0\0\2\3":
                self.device_public_keys.add(packet[4:])
                self.handshake["public_key_frames"] += 1
                self.handshake["public_key_distinct"] = len(self.device_public_keys)
                if len(self.device_public_keys) > 1:
                    self.callback_error = "device_public_key_ambiguous"
                    self.auth_event.set()
                self.public_key_event.set()

        def capture_transport_metadata(self):
            try:
                mtu = self.c.raw.mtu_size
                if isinstance(mtu, int):
                    self.handshake["bleak_mtu_size"] = mtu
            except Exception as error:
                self.handshake["mtu_metadata_error_class"] = type(error).__name__
            try:
                characteristic = self.c.raw.services.get_characteristic(auth)
                size = characteristic.max_write_without_response_size
                if isinstance(size, int):
                    self.handshake["auth_max_write_without_response_size"] = size
            except Exception as error:
                self.handshake["transport_metadata_error_class"] = type(error).__name__

        def configure_channel_capacity(self):
            size = self.handshake.get("effective_auth_write_capacity")
            if not isinstance(size, int):
                fail("auth_write_capacity_unknown")
            dmtu = min(242, size - 2)
            if dmtu < 64:
                fail("auth_write_capacity_too_small")
            self.ch.dmtu = dmtu
            self.handshake["initial_channel_dmtu"] = dmtu

        async def acquire_transport_mtu(self, timeout=5.0):
            loop = asyncio.get_running_loop()
            started = loop.time()
            self.handshake.pop("effective_auth_write_capacity", None)
            try:
                backend = getattr(self.c.raw, "_backend", None)
                acquire = getattr(backend, "_acquire_mtu", None)
                if (type(backend).__module__ != "bleak.backends.bluezdbus.client"
                        or type(backend).__name__ != "BleakClientBlueZDBus" or not callable(acquire)):
                    fail("mtu_acquisition_backend_unsupported")
                # Audited Bleak path acquires and immediately closes a transport FD.
                await asyncio.wait_for(acquire(), min(max(timeout, 0), 5.0))
                mtu = self.c.raw.mtu_size
                if type(mtu) is not int or not 23 <= mtu <= 517:
                    fail("acquired_mtu_invalid")
                self.handshake["actual_att_mtu"] = mtu
                self.handshake["auth_capacity_source"] = "bluez_acquire_mtu"
                self.handshake["effective_auth_write_capacity"] = mtu - 3
                self.capture_transport_metadata()
                if mtu - 3 < 66:
                    fail("auth_write_capacity_too_small")
            finally:
                self.handshake["mtu_acquisition_seconds"] = round(loop.time() - started, 3)

        def capture_channel_status(self):
            task = self.ch._task if self.ch else None
            dmtu = getattr(self.ch, "dmtu", None)
            if isinstance(dmtu, int):
                self.handshake["channel_dmtu"] = dmtu
            if task is None:
                self.handshake["channel_task_state"] = "not_started"
            elif task.cancelled():
                self.handshake["channel_task_state"] = "cancelled"
            elif task.done():
                error = task.exception()
                self.handshake["channel_task_state"] = "failed" if error else "finished"
                if error is not None:
                    self.handshake["channel_task_error_class"] = type(error).__name__
            else:
                self.handshake["channel_task_state"] = "running"

        async def wait_handshake_event(self, event, error_code, timeout=5.0):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                self.check_callbacks()
                if event.is_set():
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    fail(error_code)
                try:
                    await asyncio.wait_for(event.wait(), min(remaining, 0.1))
                except TimeoutError:
                    pass

        async def send_auth_single(self, package, payload, timeout=5.0):
            expected = {3: 64, 5: 8}.get(package)
            if expected is None or len(payload) != expected:
                fail("auth_single_payload_invalid")
            if self.ch.dmtu < expected:
                fail("auth_single_payload_exceeds_dmtu")
            await self.ch._send(bytes([0, 0, 0, package, 1, 0]))
            await self.wait_handshake_event(self.auth_ready, "auth_ready_timeout", timeout)
            await self.ch._send(b"\x01\0" + payload)

        async def wait_device_public_key(self, timeout=5.0, require_complete=False):
            loop = asyncio.get_running_loop()
            started = loop.time()
            deadline = started + min(max(timeout, 0), 5.0)
            try:
                while True:
                    self.check_callbacks()
                    if len(self.device_public_keys) > 1:
                        fail("device_public_key_ambiguous")
                    if self.device_public_keys and (not require_complete or self.auth_complete.is_set()):
                        return next(iter(self.device_public_keys))
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        fail("public_key_complete_ack_missing" if self.device_public_keys else "device_public_key_missing")
                    self.public_key_event.clear()
                    try:
                        await asyncio.wait_for(self.public_key_event.wait(), min(remaining, 0.1))
                    except TimeoutError:
                        pass
            finally:
                self.handshake["public_key_wait_seconds"] = round(loop.time() - started, 3)

        def set_pkt(self, *args, **kwargs):
            fail("read_only_operation")

        def action_pkt(self, *args, **kwargs):
            fail("read_only_operation")

        def _on_cmd(self, _sender, data):
            if bytes(data) == b"\0\0\x01\x01":
                self.command_ready.set()

        def _ack_done(self, task):
            self.ack_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                self.callback_error = "transport_ack_failed"

        def parse_pending_response(self, plaintext):
            tid, opcode, body = parse_frame(plaintext)
            if tid == 1:
                if opcode != 0xF0 or body:
                    fail("f0_response_invalid")
                self.f0_verified = True
                return {"verified": True}
            if tid == READ_TID:
                return parse_read_response(plaintext)
            fail("response_transaction_invalid")

        def _on_dat(self, _sender, data):
            if self.closing:
                return
            packet = bytes(data)
            if packet[:4] != b"\0\0\x02\0":
                return
            if len(packet) < 15:
                self.callback_error = "data_frame_invalid"
                return
            task = asyncio.create_task(self.c.write_gatt_char(upstream.DAT, b"\0\0\x03\0"))
            self.ack_tasks.add(task)
            task.add_done_callback(self._ack_done)
            try:
                _counter, plaintext = self.dec_frame(packet[4:])
                tid, opcode, body = parse_frame(plaintext)
                # Unsolicited notifications can reuse a pending request's TID.
                if opcode in (0x04, 0x07):  # PropertyChanged / Event
                    return
                future = self.pending.get(tid)
                if future is None or future.done():
                    return
                future.set_result(self.parse_pending_response(plaintext))
            except Exception:
                self.callback_error = "response_decrypt_or_validation_failed"
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ProbeError(self.callback_error))

        def check_callbacks(self):
            self.capture_channel_status()
            if self.callback_error:
                fail(self.callback_error)
            if self.ch and self.ch._task and self.ch._task.done():
                fail("channel_task_stopped")

        def authorize_send(self, packet, cnt):
            allowed = [
                (self.pack(1, 0xF0), 0),
                (self.get_pkt(READ_TID, [(2, 1)]), 1),
            ]
            index = len(self.sent)
            if index >= len(allowed) or (packet, cnt) != allowed[index]:
                fail("read_only_operation")
            if not self.authenticated or (index == 1 and not self.f0_verified):
                fail("protocol_phase_invalid")

        async def send(self, packet, cnt):
            self.authorize_send(packet, cnt)
            self.check_callbacks()
            self.sent.append((packet, cnt))
            self.command_ready.clear()
            header = b"\0\0\0\0\x01\0"
            encrypted = b"\x01\0" + self.enc_frame(packet, cnt)
            self.c.command_writes = [header, encrypted]
            await self.c.write_gatt_char(upstream.CMD, header)
            await asyncio.wait_for(self.command_ready.wait(), 2)
            await self.c.write_gatt_char(upstream.CMD, encrypted)

        async def exchange(self, packet, tid, counter, timeout=3):
            future = asyncio.get_running_loop().create_future()
            self.pending[tid] = future
            try:
                await self.send(packet, counter)
                response = await asyncio.wait_for(future, timeout)
                self.check_callbacks()
                return response
            finally:
                self.pending.pop(tid, None)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

        async def connect(self):
            self.result["phase"] = "ble_connect"
            self.result["connection_attempted"] = True
            await self.c.connect()
            required = (upstream.CH_CTRL, auth, upstream.CMD, upstream.DAT, upstream.MCU)
            for uuid in required:
                if self.c.raw.services.get_characteristic(uuid) is None:
                    fail("required_characteristic_missing")
            self.capture_transport_metadata()
            self.result["phase"] = "mtu_acquisition"
            await self.acquire_transport_mtu()
            self.result["phase"] = "authentication_subscribe"
            self.ch = upstream.Channel(self.c, log=lambda _message: None)
            self.configure_channel_capacity()
            await self.ch.start()
            await self.c.start_notify(upstream.DAT, self._on_dat)
            await self.c.start_notify(upstream.CMD, self._on_cmd)
            await self.c.start_notify(upstream.MCU, lambda _sender, _data: None)
            self.result["phase"] = "authentication_mcu"
            for packet in (b"\0", b"\x08\x01\0", b"\x03"):
                await self.c.write_gatt_char(upstream.MCU, packet)
                await asyncio.sleep(0.25)
            self.check_callbacks()
            self.result["phase"] = "authentication_a4"
            await self.c.write_gatt_char(upstream.CH_CTRL, b"\xa4")
            await self.wait_handshake_event(self.mng1_ack_sent, "mng1_ack_timeout")
            await asyncio.sleep(0.1)
            self.check_callbacks()
            if self.ch.dmtu < 64:
                fail("negotiated_channel_dmtu_too_small")
            self.result["phase"] = "authentication_pubkey"
            self.public_key_window = True
            private = upstream.ec.generate_private_key(upstream.ec.SECP256R1())
            public = private.public_key().public_bytes(
                upstream.serialization.Encoding.X962,
                upstream.serialization.PublicFormat.UncompressedPoint,
            )[1:]
            await self.c.write_gatt_char(upstream.CH_CTRL, b"\x50\0\0\0")
            await asyncio.sleep(0.2)
            self.ch.rxlog.clear()
            await self.send_auth_single(3, public)
            device_public = await self.wait_device_public_key(require_complete=True)
            await self.ch._send(b"\0\0\x03\0")
            await asyncio.sleep(0.25)
            self.check_callbacks()
            self.result["phase"] = "authentication_proof"
            shared = private.exchange(upstream.ec.ECDH(), upstream.ec.EllipticCurvePublicKey.from_encoded_point(
                upstream.ec.SECP256R1(), b"\x04" + device_public,
            ))
            self.k = upstream.hkdf(shared + self.ltmk, upstream.SALT, upstream.INFO, 64)
            proof = upstream.AESCCM(self.k[16:32], tag_length=4).encrypt(
                bytes(range(0x10, 0x1C)), zlib.crc32(device_public).to_bytes(4, "little"), None,
            )
            self.ch.rxlog.clear()
            self.check_callbacks()
            await self.send_auth_single(5, proof)
            await asyncio.wait_for(self.auth_event.wait(), 3)
            self.check_callbacks()
            if not self.authenticated:
                fail("authentication_not_confirmed")
            self.result["authentication_verified"] = True
            self.result["phase"] = "protocol_exchange"
            await self.exchange(self.pack(1, 0xF0), 1, 0)
            self.result["f0_verified"] = self.f0_verified

        async def read_switch(self):
            self.result["phase"] = "read_property"
            return await self.exchange(self.get_pkt(READ_TID, [(2, 1)]), READ_TID, 1)

        async def close(self):
            self.closing = True
            self.capture_channel_status()
            tasks = list(self.ack_tasks)
            if self.ch and self.ch._task:
                tasks.append(self.ch._task)
            for task in tasks:
                task.cancel()
            for future in self.pending.values():
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()
            self.pending.clear()
            try:
                if self.result["connection_attempted"]:
                    self.result["disconnect_attempted"] = True
                    await self.c.disconnect()
            finally:
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self.ltmk = b""
                self.k = None
                self.device_public_keys.clear()
                self.c.command_writes.clear()
                self.raw.clear()
                self.notify.clear()
                if self.ch:
                    self.ch.rxlog.clear()

    return ReadClient


def find_target(objects, address):
    matches = [(path, interfaces["org.bluez.Device1"])
               for path, interfaces in objects.items()
               if interfaces.get("org.bluez.Device1", {}).get("Address", "").upper() == address]
    if len(matches) > 1:
        fail("target_adapter_ambiguous")
    if matches and matches[0][1].get("Connected"):
        fail("target_already_connected")
    return matches[0] if matches else None


def active_scan(objects):
    adapters = [(path, interfaces["org.bluez.Adapter1"])
                for path, interfaces in objects.items()
                if interfaces.get("org.bluez.Adapter1", {}).get("Powered")]
    return adapters[0][0] if len(adapters) == 1 and adapters[0][1].get("Discovering") else None


async def run_probe(target_config, *, client_factory=None, operation=None):
    key = target_config.key
    result = {
        "operation": "read_property_only", "address": target_config.address, "model": target_config.model,
        "siid": 2, "piid": 1, "connection_attempted": False,
        "pairing_requested": False, "relay_writes": 0,
        "authentication_verified": False, "f0_verified": False,
        "cache_wait_limit_seconds": 30, "phase": "dependencies",
    }
    bus = None
    client = None
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _context: None)

    async def work():
        nonlocal bus, client
        from bleak import BleakClient
        from bleak.backends.device import BLEDevice
        from dbus_fast import Message, MessageType, Variant
        from dbus_fast.aio import MessageBus

        def unwrap(value):
            if isinstance(value, Variant):
                return unwrap(value.value)
            if isinstance(value, dict):
                return {name: unwrap(item) for name, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [unwrap(item) for item in value]
            return value

        async def objects():
            reply = await bus.call(Message(destination="org.bluez", path="/",
                interface="org.freedesktop.DBus.ObjectManager", member="GetManagedObjects"))
            if reply.message_type == MessageType.ERROR:
                fail("bluez_query_failed")
            return unwrap(reply.body[0])

        upstream = load_vendor()
        result["phase"] = "bluez_cache"
        bus = await MessageBus(bus_address="unix:path=/run/dbus/system_bus_socket").connect()
        snapshot = await objects()
        target = find_target(snapshot, target_config.address)
        if target is None:
            scan = active_scan(snapshot)
            if scan is None:
                fail("target_absent_no_existing_discovery")
            deadline = loop.time() + 30
            while target is None and loop.time() < deadline:
                await asyncio.sleep(min(1, max(0, deadline - loop.time())))
                snapshot = await objects()
                if active_scan(snapshot) != scan:
                    fail("existing_discovery_stopped_or_changed")
                target = find_target(snapshot, target_config.address)
            if target is None:
                fail("target_not_seen")
        # Recheck immediately before connecting; an existing connection is never adopted.
        target = find_target(await objects(), target_config.address)
        if target is None:
            fail("target_disappeared")
        path, props = target
        result["adapter_path"] = props.get("Adapter")
        if type(props.get("RSSI")) is int:
            result["cached_rssi_before_connect_dbm"] = props["RSSI"]
        device = BLEDevice(props["Address"], props.get("Name"), {"path": path, "props": props})
        raw = BleakClient(device, pair=False, timeout=20)
        client = (client_factory or make_client_class)(upstream)(device, key, raw, result)
        connect_started = loop.time()
        await client.connect()
        result["connect_auth_elapsed_seconds"] = round(loop.time() - connect_started, 3)
        if operation is None:
            read_started = loop.time()
            result["property"] = await client.read_switch()
            result["property_read_elapsed_seconds"] = round(loop.time() - read_started, 3)
            result["status"] = "read_succeeded"
        else:
            await operation(client, result)

    try:
        await asyncio.wait_for(work(), WORK_SECONDS)
    except Exception as error:
        result["status"] = "error"
        result["error_code"] = error.args[0] if isinstance(error, ProbeError) else (
            "phase_timeout" if isinstance(error, TimeoutError) else "probe_failed")
        result["error_class"] = type(error).__name__
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), CLEANUP_SECONDS)
                result["connected_after_cleanup"] = client.c.is_connected
                if result["connected_after_cleanup"]:
                    result["cleanup_error_code"] = "still_connected"
            except Exception as error:
                result["cleanup_error_code"] = "disconnect_failed_or_timed_out"
                result["cleanup_error_class"] = type(error).__name__
        if bus is not None:
            bus.disconnect()
        loop.set_exception_handler(old_handler)
        key = b""
    return result


def main():
    logging.disable(logging.CRITICAL)
    try:
        if len(sys.argv) != 1:
            fail("command_line_arguments_not_allowed")
        raw_input = sys.stdin.buffer.read(4097)
        if len(raw_input) > 4096:
            fail("input_too_large")
        data = json.loads(raw_input, object_pairs_hook=no_duplicate_keys)
        key = validated_input(data)
        del raw_input, data
        result = asyncio.run(run_probe(key))
        key = b""
    except Exception as error:
        result = {"status": "error", "phase": "input_or_runtime",
                  "error_code": error.args[0] if isinstance(error, ProbeError) else "input_or_runtime_failed",
                  "error_class": type(error).__name__}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("status") == "read_succeeded" and not result.get("cleanup_error_code") else 1


if __name__ == "__main__":
    raise SystemExit(main())

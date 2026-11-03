"""Offline tests. No Bluetooth, SSH, ADB, or cloud calls."""

import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest

import read_probe as probe


def pack(tid, opcode, body=b""):
    return (0x2000 | (5 + len(body))).to_bytes(2, "little") + tid.to_bytes(2, "little") + bytes([opcode]) + body


def response(value=1, typelen=0x1001, status=0, siid=2, piid=1):
    body = bytes([1, siid]) + piid.to_bytes(2, "little") + status.to_bytes(2, "little")
    body += typelen.to_bytes(2, "little") + bytes([value])
    return pack(probe.READ_TID, 3, body)


class FakeBase:
    def __init__(self, device, key, log):
        self.ltmk, self.k, self.ch = key, None, None
        self.pending, self.raw, self.notify = {}, [], []

    pack = staticmethod(pack)

    def get_pkt(self, tid, props):
        return pack(tid, 2, bytes([len(props)]) + b"".join(bytes([s]) + p.to_bytes(2, "little") for s, p in props))

    def enc_frame(self, packet, counter):
        return counter.to_bytes(2, "little") + packet

    def dec_frame(self, frame):
        return 0, self.decoded


UPSTREAM = types.SimpleNamespace(Client=FakeBase, CH_CTRL="CTRL", CMD="CMD", DAT="DAT", MCU="MCU")


class FakeRaw:
    is_connected = True

    def __init__(self):
        self.writes, self.callbacks = [], {}
        self.owner = None

    async def write_gatt_char(self, uuid, packet, response=False):
        self.writes.append((uuid, packet))
        if uuid == "CMD" and packet == b"\0\0\0\0\x01\0":
            self.owner.command_ready.set()

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def disconnect(self):
        self.is_connected = False


class ParseTests(unittest.TestCase):
    def test_input_exact_scope(self):
        data = {"address": "02:00:00:00:00:01", "model": probe.MODEL, "gatt_ltmk": "ab" * 32}
        self.assertEqual(len(probe.validated_input(data).key), 32)
        self.assertEqual(probe.validated_input(data).address, data["address"])
        for field, value in (("operation", "write"), ("operation", "read"), ("siid", 2)):
            with self.assertRaises(probe.ProbeError):
                probe.validated_input({**data, field: value})
        for field, value in (("model", "other"), ("address", "00:00:00:00:00:00"), ("gatt_ltmk", "ab" * 31)):
            with self.assertRaises(probe.ProbeError):
                probe.validated_input({**data, field: value})

    def test_duplicate_fields_rejected(self):
        with self.assertRaises(probe.ProbeError):
            json.loads('{"model":"a","model":"b"}', object_pairs_hook=probe.no_duplicate_keys)

    def test_raw_type_preserved(self):
        for typelen in (0x0001, 0x1001, 0xF001):
            for value in (0, 1):
                result = probe.parse_read_response(response(value, typelen))
                self.assertEqual(result["typelen"], typelen)
                self.assertEqual(result["type_code"], typelen >> 12)
                self.assertIs(result["value"], bool(value))

    def test_invalid_read_responses(self):
        cases = [response(2), response(1, 0x1002), response(status=1), response(siid=3), response(piid=2),
                 response()[:-1], response() + b"x", b"", pack(1, 3, response()[5:]),
                 pack(probe.READ_TID, 1, response()[5:]), pack(probe.READ_TID, 3, b"\x02" + response()[6:])]
        for packet in cases:
            with self.subTest(size=len(packet)), self.assertRaises(probe.ProbeError):
                probe.parse_read_response(packet)

    def test_cache_refuses_existing_or_ambiguous(self):
        device = {"org.bluez.Device1": {"Address": "02:00:00:00:00:01", "Connected": False}}
        self.assertEqual(probe.find_target({"/a": device}, "02:00:00:00:00:01")[0], "/a")
        self.assertIsNone(probe.find_target({}, "02:00:00:00:00:01"))
        with self.assertRaises(probe.ProbeError):
            probe.find_target({"/a": device, "/b": device}, "02:00:00:00:00:01")
        with self.assertRaises(probe.ProbeError):
            probe.find_target({"/a": {"org.bluez.Device1": {"Address": "02:00:00:00:00:01", "Connected": True}}}, "02:00:00:00:00:01")

    def test_request_targets_and_keys_do_not_share_state(self):
        first = probe.validated_input({"address": "02:00:00:00:00:01", "model": probe.MODEL, "gatt_ltmk": "ab" * 32})
        second = probe.validated_input({"address": "02:00:00:00:00:02", "model": probe.MODEL, "gatt_ltmk": "cd" * 32})
        self.assertNotEqual(first.key, second.key)
        self.assertNotIn(first.key.hex(), repr(first))
        objects = {"/first": {"org.bluez.Device1": {"Address": first.address}},
                   "/second": {"org.bluez.Device1": {"Address": second.address}}}
        self.assertEqual(probe.find_target(objects, first.address)[0], "/first")
        self.assertEqual(probe.find_target(objects, second.address)[0], "/second")

    def test_vendor_hashes(self):
        for name, expected in probe.VENDOR_HASHES.items():
            self.assertEqual(hashlib.sha256((Path(__file__).parent / "vendor" / name).read_bytes()).hexdigest(), expected)

    def test_invalid_input_output_contains_no_secret(self):
        secret = "ba" * 32
        data = {"address": "02:00:00:00:00:01", "model": probe.MODEL, "gatt_ltmk": secret, "operation": "write"}
        result = subprocess.run([sys.executable, str(Path(probe.__file__))], input=json.dumps(data),
                                capture_output=True, text=True, timeout=5)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["error_code"], "input_fields_invalid")
        self.assertEqual(result.returncode, 1)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.raw = FakeRaw()
        self.result = {"connection_attempted": True}
        self.client = probe.make_client_class(UPSTREAM)(object(), b"x" * 32, self.raw, self.result)
        self.raw.owner = self.client

    def prepare_auth_channel(self):
        auth = "00000016-0000-1000-8000-00805f9b34fb"
        async def send(packet):
            await self.client.c.write_gatt_char(auth, packet)
        self.client.ch = types.SimpleNamespace(_task=None, dmtu=111, rxlog=[], _send=send)
        self.client.handshake["auth_max_write_without_response_size"] = 113
        self.client.handshake["effective_auth_write_capacity"] = 113
        return auth

    async def test_dmtu_uses_auth_write_capacity_and_refuses_small_or_unknown(self):
        self.prepare_auth_channel()
        self.client.handshake["bleak_mtu_size"] = 23
        self.client.configure_channel_capacity()
        self.assertEqual(self.client.ch.dmtu, 111)
        for size in (None, 20, 65):
            self.client.handshake["effective_auth_write_capacity"] = size
            with self.assertRaises(probe.ProbeError):
                self.client.configure_channel_capacity()
        self.assertEqual(self.raw.writes, [])

    def install_fake_bluez_backend(self, acquire):
        backend_class = type("BleakClientBlueZDBus", (), {
            "__module__": "bleak.backends.bluezdbus.client", "_acquire_mtu": acquire})
        self.raw._backend = backend_class()

    async def test_acquired_mtu_overrides_stale_characteristic_capacity(self):
        auth = self.prepare_auth_channel()
        characteristic = types.SimpleNamespace(max_write_without_response_size=20)
        self.raw.services = types.SimpleNamespace(get_characteristic=lambda _uuid: characteristic)
        async def acquire(_backend):
            self.raw.mtu_size = 116
        self.install_fake_bluez_backend(acquire)
        await self.client.acquire_transport_mtu()
        self.client.configure_channel_capacity()
        self.assertEqual(self.client.ch.dmtu, 111)
        self.assertEqual(self.client.handshake["auth_max_write_without_response_size"], 20)
        self.assertEqual(self.client.handshake["effective_auth_write_capacity"], 113)
        self.assertEqual(self.client.handshake["actual_att_mtu"], 116)
        self.assertEqual(self.client.handshake["auth_capacity_source"], "bluez_acquire_mtu")
        self.assertEqual(self.raw.writes, [])
        await self.client.c.write_gatt_char(auth, b"\0\0\5\1" + b"x" * 109)
        with self.assertRaisesRegex(probe.ProbeError, "^auth_frame_exceeds_capacity$"):
            await self.client.c.write_gatt_char(auth, b"\0\0\5\1" + b"x" * 110)

    async def test_acquired_mtu_refuses_small_or_invalid_values(self):
        self.prepare_auth_channel()
        async def acquire(_backend):
            pass
        self.install_fake_bluez_backend(acquire)
        for mtu, code in ((23, "auth_write_capacity_too_small"), (22, "acquired_mtu_invalid"),
                          (518, "acquired_mtu_invalid"), (True, "acquired_mtu_invalid")):
            self.raw.mtu_size = mtu
            with self.assertRaisesRegex(probe.ProbeError, "^" + code + "$"):
                await self.client.acquire_transport_mtu()
        self.assertEqual(self.raw.writes, [])

    async def test_mtu_acquisition_rejects_unsupported_backend_and_is_bounded(self):
        with self.assertRaisesRegex(probe.ProbeError, "^mtu_acquisition_backend_unsupported$"):
            await self.client.acquire_transport_mtu()
        cancelled = asyncio.Event()
        async def acquire(_backend):
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
        self.install_fake_bluez_backend(acquire)
        with self.assertRaises(TimeoutError):
            await self.client.acquire_transport_mtu(timeout=0.01)
        self.assertTrue(cancelled.is_set())
        self.assertNotIn("effective_auth_write_capacity", self.client.handshake)
        self.assertEqual(self.raw.writes, [])

    async def test_mng1_event_requires_successful_write_and_gates_login(self):
        auth = self.prepare_auth_channel()
        await self.client.c.write_gatt_char("CTRL", b"\xa4")
        with self.assertRaisesRegex(probe.ProbeError, "^negotiation_not_complete$"):
            await self.client.c.write_gatt_char("CTRL", b"\x50\0\0\0")
        original = self.raw.write_gatt_char
        async def failed_write(_uuid, _packet, response=False):
            raise RuntimeError("not-output")
        self.raw.write_gatt_char = failed_write
        ack = b"\0\0\5\1" + bytes([111]) * 109
        with self.assertRaises(RuntimeError):
            await self.client.c.write_gatt_char(auth, ack)
        self.assertFalse(self.client.mng1_ack_sent.is_set())
        self.raw.write_gatt_char = original
        await self.client.c.write_gatt_char(auth, ack)
        self.assertTrue(self.client.mng1_ack_sent.is_set())
        await self.client.c.write_gatt_char("CTRL", b"\x50\0\0\0")

    async def test_each_auth_package_needs_fresh_ready(self):
        auth = self.prepare_auth_channel()
        self.client.auth_ready.set()
        for package, payload in ((3, b"p" * 64), (5, b"r" * 8)):
            task = asyncio.create_task(self.client.send_auth_single(package, payload, timeout=0.2))
            await asyncio.sleep(0)
            self.assertFalse(self.client.auth_ready.is_set())
            self.assertEqual(self.raw.writes[-1], (auth, bytes([0, 0, 0, package, 1, 0])))
            self.client.record_auth_frame(b"\0\0\1\1")
            await task
            self.assertEqual(self.raw.writes[-1], (auth, b"\1\0" + payload))
        self.assertEqual(len(self.raw.writes), 4)

    async def test_stale_or_wrong_ready_never_sends_payload(self):
        auth = self.prepare_auth_channel()
        self.client.auth_ready.set()
        task = asyncio.create_task(self.client.send_auth_single(3, b"p" * 64, timeout=0.02))
        await asyncio.sleep(0)
        self.client.record_auth_frame(b"\0\0\1\0")
        with self.assertRaisesRegex(probe.ProbeError, "^auth_ready_timeout$"):
            await task
        self.assertEqual(self.raw.writes, [(auth, b"\0\0\0\3\1\0")])

    async def test_public_key_requires_complete_ack_and_single_reply(self):
        self.client.public_key_window = True
        self.client.record_auth_frame(b"\0\0\2\3" + b"p" * 64)
        task = asyncio.create_task(self.client.wait_device_public_key(timeout=0.2, require_complete=True))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.client.record_auth_frame(b"\0\0\1\0")
        self.assertEqual(await task, b"p" * 64)
        self.assertEqual(self.raw.writes, [])

    async def test_write_and_action_paths_rejected(self):
        for operation in (self.client.set_pkt, self.client.action_pkt):
            with self.assertRaises(probe.ProbeError):
                operation()
        self.client.authenticated = True
        for packet in (pack(1, 0), pack(1, 5), pack(1, 2, b"\1\3\1\0")):
            with self.assertRaises(probe.ProbeError):
                await self.client.send(packet, 0)
        self.assertEqual(self.raw.writes, [])

    async def test_only_exact_f0_and_one_read_can_send(self):
        self.client.authenticated = True
        await self.client.send(pack(1, 0xF0), 0)
        with self.assertRaises(probe.ProbeError):
            await self.client.send(self.client.get_pkt(probe.READ_TID, [(2, 1)]), 1)
        self.client.f0_verified = True
        await self.client.send(self.client.get_pkt(probe.READ_TID, [(2, 1)]), 1)
        with self.assertRaises(probe.ProbeError):
            await self.client.send(self.client.get_pkt(probe.READ_TID, [(2, 1)]), 2)
        self.assertEqual(len(self.raw.writes), 4)
        self.assertTrue(all(uuid == "CMD" for uuid, _ in self.raw.writes))

    async def test_transport_rejects_unqueued_commands(self):
        for uuid, packet in (("CMD", b"arbitrary"), ("MCU", b"\x99"), ("CTRL", b"\x99"), ("DAT", b"\x99")):
            with self.assertRaises(probe.ProbeError):
                await self.client.c.write_gatt_char(uuid, packet)
        self.assertEqual(self.raw.writes, [])

    async def test_auth_result_must_be_exact(self):
        await self.client.c.start_notify("CTRL", lambda _sender, _data: None)
        self.raw.callbacks["CTRL"](None, b"\x51\x01")
        self.assertFalse(self.client.authenticated)
        self.assertIsNotNone(self.client.callback_error)
        self.client.callback_error = None
        self.raw.callbacks["CTRL"](None, b"\x51\0\0\0")
        self.assertTrue(self.client.authenticated)

    async def test_handshake_diagnostics_contain_only_metadata(self):
        auth = "00000016-0000-1000-8000-00805f9b34fb"
        await self.client.c.start_notify(auth, lambda _sender, _data: None)
        await self.client.c.start_notify("CTRL", lambda _sender, _data: None)
        self.client.public_key_window = True
        secret = b"unlogged-public-key".ljust(64, b"q")
        frames = [b"\0\0\4\0" + b"hidden", b"\0\0\0\3\4\0",
                  b"\1\0" + b"hidden-fragment", b"\0\0\2\3" + secret]
        for frame in frames:
            self.raw.callbacks[auth](None, frame)
        self.raw.callbacks["CTRL"](None, b"private-control")
        self.raw.callbacks["CTRL"](None, b"\x51\0\0\0")
        diagnostic = self.result["handshake"]
        self.assertEqual(diagnostic["auth_frames"], {
            "mng:length_10": 1, "ctr:length_6": 1,
            "sequence_or_unknown:length_17": 1, "single_ctr:length_68": 1,
        })
        self.assertEqual(diagnostic["public_key_ctr_announcements"], {"4": 1})
        self.assertEqual(diagnostic["control_frames"], {
            "other:length_15": 1, "login_success_0x51:length_4": 1,
        })
        serialized = json.dumps(self.result)
        for forbidden in (secret.hex(), secret.decode(), "private-control", "hidden-fragment"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(self.raw.writes, [])

    async def test_mtu_diagnostics(self):
        self.raw.mtu_size = 23
        self.raw.services = types.SimpleNamespace(get_characteristic=lambda _uuid:
            types.SimpleNamespace(max_write_without_response_size=20))
        self.client.ch = types.SimpleNamespace(_task=None, dmtu=18, rxlog=[])
        self.client.capture_transport_metadata()
        self.client.capture_channel_status()
        diagnostic = self.result["handshake"]
        self.assertEqual(diagnostic["bleak_mtu_size"], 23)
        self.assertEqual(diagnostic["auth_max_write_without_response_size"], 20)
        self.assertEqual(diagnostic["channel_dmtu"], 18)
        self.assertEqual(self.raw.writes, [])

    async def test_public_key_wait_accepts_delayed_response(self):
        self.client.public_key_window = True
        key = b"p" * 64
        asyncio.get_running_loop().call_later(0.02, self.client.record_auth_frame, b"\0\0\2\3" + key)
        self.assertEqual(await self.client.wait_device_public_key(timeout=0.2), key)
        self.assertGreaterEqual(self.result["handshake"]["public_key_wait_seconds"], 0.015)
        self.assertEqual(self.raw.writes, [])

    async def test_public_key_retransmission_is_deduplicated(self):
        self.client.public_key_window = True
        frame = b"\0\0\2\3" + b"p" * 64
        self.client.record_auth_frame(frame)
        self.client.record_auth_frame(frame)
        self.assertEqual(await self.client.wait_device_public_key(timeout=0.1), b"p" * 64)
        self.assertEqual(self.result["handshake"]["public_key_frames"], 2)
        self.assertEqual(self.result["handshake"]["public_key_distinct"], 1)
        self.assertIsNone(self.client.callback_error)

    async def test_distinct_public_keys_rejected_even_after_first_wait(self):
        self.client.public_key_window = True
        self.client.record_auth_frame(b"\0\0\2\3" + b"p" * 64)
        await self.client.wait_device_public_key(timeout=0.1)
        self.client.record_auth_frame(b"\0\0\2\3" + b"q" * 64)
        with self.assertRaisesRegex(probe.ProbeError, "^device_public_key_ambiguous$"):
            self.client.check_callbacks()
        with self.assertRaisesRegex(probe.ProbeError, "^device_public_key_ambiguous$"):
            await self.client.wait_device_public_key(timeout=0.1)
        self.assertEqual(self.result["handshake"]["public_key_distinct"], 2)
        self.assertEqual(self.raw.writes, [])

    async def test_missing_public_key_times_out_without_writes(self):
        with self.assertRaisesRegex(probe.ProbeError, "^device_public_key_missing$"):
            await self.client.wait_device_public_key(timeout=0.01)
        self.assertLess(self.result["handshake"]["public_key_wait_seconds"], 0.2)
        self.assertEqual(self.raw.writes, [])

    async def test_channel_failure_reports_class_not_message(self):
        async def failed_channel():
            raise ValueError("secret-session-material")
        task = asyncio.create_task(failed_channel())
        self.client.ch = types.SimpleNamespace(_task=task, dmtu=18, rxlog=[])
        await asyncio.sleep(0)
        with self.assertRaisesRegex(probe.ProbeError, "^channel_task_stopped$"):
            await self.client.wait_device_public_key(timeout=0.1)
        self.assertEqual(self.result["handshake"]["channel_task_state"], "failed")
        self.assertEqual(self.result["handshake"]["channel_task_error_class"], "ValueError")
        self.assertNotIn("secret-session-material", json.dumps(self.result))
        await self.client.close()

    async def test_f0_timeout_never_succeeds(self):
        self.client.authenticated = True
        with self.assertRaises(TimeoutError):
            await self.client.exchange(pack(1, 0xF0), 1, 0, timeout=0.001)
        self.assertFalse(self.client.f0_verified)
        self.assertEqual(self.client.pending, {})

    async def test_wrong_f0_response_rejected(self):
        future = asyncio.get_running_loop().create_future()
        self.client.pending[1] = future
        self.client.decoded = pack(1, 0xF0, b"unexpected")
        self.client._on_dat(None, b"\0\0\2\0" + b"x" * 11)
        with self.assertRaises(probe.ProbeError):
            await future
        self.assertFalse(self.client.f0_verified)
        await self.client.close()

    async def test_async_tid_collision_does_not_complete_f0(self):
        future = asyncio.get_running_loop().create_future()
        self.client.pending[1] = future
        for opcode, body in ((4, b"\1\2\1\0\1\x10\1"), (7, b"\2\1\0")):
            with self.subTest(opcode=opcode):
                self.client.decoded = pack(1, opcode, body)
                self.client._on_dat(None, b"\0\0\2\0" + b"x" * 11)
                await asyncio.sleep(0)
                self.assertFalse(future.done())
                self.assertFalse(self.client.f0_verified)
                self.assertIsNone(self.client.callback_error)
        self.assertEqual(self.raw.writes, [("DAT", b"\0\0\3\0")] * 2)
        self.client.decoded = pack(1, 0xF0)
        self.client._on_dat(None, b"\0\0\2\0" + b"x" * 11)
        self.assertEqual(await future, {"verified": True})
        self.assertTrue(self.client.f0_verified)
        await self.client.close()

    async def test_async_tid_collision_does_not_complete_read(self):
        future = asyncio.get_running_loop().create_future()
        self.client.pending[probe.READ_TID] = future
        for opcode, body in ((4, b"\1\2\1\0\1\x10\1"), (7, b"\2\1\0")):
            with self.subTest(opcode=opcode):
                self.client.decoded = pack(probe.READ_TID, opcode, body)
                self.client._on_dat(None, b"\0\0\2\0" + b"x" * 11)
                await asyncio.sleep(0)
                self.assertFalse(future.done())
                self.assertIsNone(self.client.callback_error)
        self.assertEqual(self.raw.writes, [("DAT", b"\0\0\3\0")] * 2)
        self.client.decoded = response(value=0)
        self.client._on_dat(None, b"\0\0\2\0" + b"x" * 11)
        self.assertEqual(await future, probe.parse_read_response(response(value=0)))
        self.assertIsNone(self.client.callback_error)
        await self.client.close()

    async def test_cleanup_cancels_and_disconnects(self):
        channel_task = asyncio.create_task(asyncio.sleep(30))
        ack_task = asyncio.create_task(asyncio.sleep(30))
        self.client.ch = types.SimpleNamespace(_task=channel_task, rxlog=[b"private"])
        self.client.ack_tasks.add(ack_task)
        await self.client.close()
        self.assertTrue(channel_task.cancelled())
        self.assertTrue(ack_task.cancelled())
        self.assertFalse(self.raw.is_connected)
        self.assertTrue(self.result["disconnect_attempted"])
        self.assertEqual(self.client.ltmk, b"")
        self.assertEqual(self.client.ch.rxlog, [])


if __name__ == "__main__":
    unittest.main()

"""MIoT BLE Spec-v2 client for the Xiaomi Mijia smart diffuser XW002.

Protocol, reverse engineered from the Mi Home Android app (11.7.707):

    login  0x50 + CTR package -> ECDH + HKDF(gatt_ltmk) -> 0x51
    data   AES-CCM, RX key = okm[0:16]  IV = okm[32:36]
                   TX key = okm[16:32] IV = okm[36:40]
    nonce  iv(4) || 00 00 00 00 || counter(2 LE) || roll(2 LE)
    TX     00 00 00 00 01 00  -> RCV_RDY -> 01 00 || counter || ciphertext
    RX     00 00 02 00 || counter || ciphertext
    spec   [u16 0x2000|len][u16 tid][u8 opcode][body]

Two timing rules are mandatory and are handled by ``connect()``:

    1. Right after login, send the F0 protocol-exchange packet.
    2. ACK every RX frame with ``00 00 03 00`` on the data channel.

Usage:

    key = bytes.fromhex(os.environ["LTMK_HEX"])
    client = Client(address, key)
    await client.connect()
    reply = await client.req(client.get_pkt(0x7201, [(3, 1)]), 0x7201, 1)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bleak import BleakClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from channel import CH_CTRL, Channel


def _uuid(short: int) -> str:
    return "0000%04x-0000-1000-8000-00805f9b34fb" % short


MCU = _uuid(0x001c)
CMD = _uuid(0x001a)
DAT = _uuid(0x001b)

SALT = b"miot-mesh-login-salt"
INFO = b"miot-mesh-login-info"

SPEC_VALUE_TYPES = {
    0: "bool",
    1: "uint8",
    2: "int8",
    3: "uint16",
    4: "int16",
    5: "uint32",
    6: "int32",
    7: "uint64",
    8: "int64",
    9: "float",
    10: "string",
}


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256 (extract + expand) as used by the login handshake."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


class Client:
    """One BLE connection to an XW002 (or compatible MIoT BLE v2 device)."""

    def __init__(self, address: str, ltmk: bytes, log=print):
        if len(ltmk) != 32:
            raise ValueError("gatt_ltmk must be exactly 32 bytes")
        self.addr = address
        self.ltmk = ltmk
        self.log = log
        self.k: bytes | None = None
        self.roll = 0
        self.cnt = 1
        self.pending: dict[int, asyncio.Future] = {}
        self.on_props = None  # callback(siid, piid, type_name, value)
        self.notify: list[tuple[str, bytes]] = []
        self.raw: list[bytes] = []
        self.c: BleakClient | None = None
        self.ch: Channel | None = None

    # ------------------------------------------------------------- framing
    def _nonce(self, iv: bytes, cnt: int, roll: int) -> bytes:
        return iv + b"\x00" * 4 + cnt.to_bytes(2, "little") + roll.to_bytes(2, "little")

    def dec_frame(self, frame: bytes):
        cnt = int.from_bytes(frame[0:2], "little")
        ct = frame[2:]
        pt = AESCCM(self.k[0:16], tag_length=4).decrypt(
            self._nonce(self.k[32:36], cnt, 0), ct, None
        )
        return cnt, pt

    def enc_frame(self, pt: bytes, cnt: int) -> bytes:
        ct = AESCCM(self.k[16:32], tag_length=4).encrypt(
            self._nonce(self.k[36:40], cnt, self.roll), pt, None
        )
        return cnt.to_bytes(2, "little") + ct

    # ------------------------------------------------------------- messages
    @staticmethod
    def pack(tid: int, opcode: int, body: bytes = b"") -> bytes:
        length = 5 + len(body)
        return (
            (0x2000 | length).to_bytes(2, "little")
            + tid.to_bytes(2, "little")
            + bytes([opcode])
            + body
        )

    @staticmethod
    def parse(pt: bytes) -> dict:
        ver = int.from_bytes(pt[0:2], "little") >> 12
        length = int.from_bytes(pt[0:2], "little") & 0xFFF
        tid = int.from_bytes(pt[2:4], "little")
        opcode = pt[4]
        body = pt[5:]
        out = {
            "ver": ver,
            "len": length,
            "tid": tid,
            "opcode": opcode,
            "raw": pt.hex(),
        }
        if opcode == 4 and body:  # PropertyChanged
            count = body[0]
            i = 1
            props = []
            while len(props) < count and i + 4 < len(body):
                siid = body[i]
                piid = int.from_bytes(body[i + 1:i + 3], "little")
                typelen = int.from_bytes(body[i + 3:i + 5], "little")
                vtype = (typelen >> 12) & 0xF
                vlen = typelen & 0xFFF
                value = body[i + 5:i + 5 + vlen]
                props.append(
                    (
                        siid,
                        piid,
                        SPEC_VALUE_TYPES.get(vtype, vtype),
                        int.from_bytes(value, "little") if 0 < vlen <= 4 else value.hex(),
                    )
                )
                i += 5 + vlen
            out["props"] = props
            out["name"] = "PropertyChanged"
        elif opcode == 3 and body:  # GetPropertyResp
            count = body[0]
            i = 1
            props = []
            for _ in range(count):
                siid = body[i]
                piid = int.from_bytes(body[i + 1:i + 3], "little")
                status = int.from_bytes(body[i + 3:i + 5], "little")
                i += 5
                if status == 0:
                    typelen = int.from_bytes(body[i:i + 2], "little")
                    vtype = (typelen >> 12) & 0xF
                    vlen = typelen & 0xFFF
                    value = body[i + 2:i + 2 + vlen]
                    i += 2 + vlen
                    props.append(
                        (
                            siid,
                            piid,
                            "ok",
                            SPEC_VALUE_TYPES.get(vtype, vtype),
                            int.from_bytes(value, "little") if 0 < vlen <= 4 else value.hex(),
                        )
                    )
                else:
                    props.append((siid, piid, "err code=%d" % status))
            out["props"] = props
            out["name"] = "GetPropertyResp"
        elif opcode == 1 and body:  # SetPropertyResp
            count = body[0]
            i = 1
            props = []
            for _ in range(count):
                siid = body[i]
                piid = int.from_bytes(body[i + 1:i + 3], "little")
                code = int.from_bytes(body[i + 3:i + 5], "little")
                i += 5
                props.append((siid, piid, "code=%d" % code))
            out["props"] = props
            out["name"] = "SetPropertyResp"
        elif opcode == 0xF0:
            out["name"] = "ProtocolExchange(ver=%d)" % ver
        else:
            out["name"] = "opcode%d" % opcode
        return out

    def get_pkt(self, tid: int, props) -> bytes:
        body = bytes([len(props)]) + b"".join(
            bytes([s]) + p.to_bytes(2, "little") for s, p in props
        )
        return self.pack(tid, 2, body)

    def action_pkt(self, tid: int, siid: int, aiid: int, args=None) -> bytes:
        """DoAction (opcode 5): siid + aiid + count + [aiid(2) typelen(2) value]."""
        args = args or []
        body = bytes([siid, aiid, len(args)])
        for aid, typelen, value in args:
            body += aid.to_bytes(2, "little") + typelen.to_bytes(2, "little") + value
        return self.pack(tid, 5, body)

    def set_pkt(self, tid: int, items) -> bytes:
        """items: iterable of (siid, piid, typelen, value_bytes)."""
        items = list(items)
        body = bytes([len(items)])
        for siid, piid, typelen, value in items:
            body += (
                bytes([siid])
                + piid.to_bytes(2, "little")
                + typelen.to_bytes(2, "little")
                + value
            )
        return self.pack(tid, 0, body)

    @staticmethod
    def bool_val(value: bool):
        return 0x1001, bytes([1 if value else 0])

    @staticmethod
    def uint8_val(value: int):
        return 0x2001, bytes([value & 0xFF])

    # -------------------------------------------------------------- callbacks
    def _on_cmd(self, _sender, data) -> None:
        b = bytes(data)
        self.notify.append(("CMD", b))
        if len(b) > 4:
            self.log("  <<< CMD %s" % b.hex())

    def _on_dat(self, _sender, data) -> None:
        b = bytes(data)
        self.notify.append(("DAT", b))
        self.log("  <<< DAT %s" % b.hex())
        if len(b) > 6 and b[:4] == b"\x00\x00\x02\x00":
            self.raw.append(b[4:])
            asyncio.get_event_loop().create_task(
                self.c.write_gatt_char(DAT, bytes([0, 0, 3, 0]), response=False)
            )
            try:
                _cnt, pt = self.dec_frame(b[4:])
                info = self.parse(pt)
                self.log("      -> %s tid=%d %s" % (info["name"], info["tid"], info.get("props", "")))
                future = self.pending.pop(info["tid"], None)
                if future and not future.done():
                    future.set_result(info)
                if info["name"] == "PropertyChanged" and self.on_props:
                    for prop in info.get("props", []):
                        try:
                            self.on_props(*prop)
                        except Exception:
                            pass
            except Exception as exc:  # noqa: BLE001 - reference implementation
                self.log("      -> decrypt/parse failed: %s" % exc)

    # -------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        self.c = BleakClient(self.addr, timeout=25)
        await self.c.connect()
        self.ch = Channel(self.c, lambda _m: None)
        await self.ch.start()
        await self.c.start_notify(DAT, self._on_dat)
        await self.c.start_notify(CMD, self._on_cmd)
        await self.c.start_notify(MCU, lambda _sd, _d: None)

        # 1. MCU information exchange
        for opcode in (bytes([0]), bytes([8, 1, 0]), bytes([3])):
            await self.c.write_gatt_char(MCU, opcode, response=False)
            await asyncio.sleep(0.25)

        # 2. A4 + MNG negotiation
        await self.c.write_gatt_char(CH_CTRL, bytes([0xA4]), response=False)
        await asyncio.sleep(2.2)

        # 3. login request + ECDH public key
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )[1:]
        await self.c.write_gatt_char(CH_CTRL, bytes.fromhex("50000000"), response=False)
        await asyncio.sleep(0.2)

        self.ch.rxlog.clear()
        await self.ch._send(bytes([0, 0, 0, 3, 1, 0]))
        await asyncio.sleep(0.4)
        await self.ch._send((1).to_bytes(2, "little") + public_key)
        await asyncio.sleep(0.9)

        device_key = None
        for b in self.ch.rxlog:
            if len(b) >= 68 and b[2] == 2 and b[3] == 3:
                device_key = b[4:68]
        if device_key is None:
            raise RuntimeError("device public key not received")
        await self.ch._send(bytes([0, 0, 3, 0]))
        await asyncio.sleep(0.25)

        # 4. derive session key and send the login proof
        shared = private_key.exchange(
            ec.ECDH(),
            ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), b"\x04" + device_key
            ),
        )
        self.k = hkdf(shared + self.ltmk, SALT, INFO, 64)
        proof = AESCCM(self.k[16:32], tag_length=4).encrypt(
            bytes(range(0x10, 0x1C)), zlib.crc32(device_key).to_bytes(4, "little"), None
        )

        self.ch.rxlog.clear()
        await self.ch._send(bytes([0, 0, 0, 5, 1, 0]))
        await asyncio.sleep(0.35)
        await self.ch._send((1).to_bytes(2, "little") + proof)

        ok = False
        for _ in range(100):
            await asyncio.sleep(0.01)
            for b in self.ch.rxlog:
                if b[:1] == b"\x51":
                    ok = True
                if b[:1] == b"\x52":
                    raise RuntimeError("login rejected by device")
            if ok:
                break
        if not ok:
            raise RuntimeError("login timeout")

        # 5. F0 protocol exchange has to happen immediately after login
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending[1] = future
        await self.send(self.pack(1, 0xF0), 0)
        try:
            result = await asyncio.wait_for(future, 2.0)
            self.log("  F0 exchange done: %s" % result["name"])
        except asyncio.TimeoutError:
            self.log("  !! F0 got no response")

    async def send(self, packet: bytes, cnt: int) -> None:
        start = len(self.notify)
        await self.c.write_gatt_char(CMD, bytes([0, 0, 0, 0, 1, 0]), response=False)
        deadline = asyncio.get_event_loop().time() + 1.0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.005)
            if any(
                x[0] == "CMD" and x[1][:4] == b"\x00\x00\x01\x01"
                for x in self.notify[start:]
            ):
                break
        await self.c.write_gatt_char(
            CMD, b"\x01\x00" + self.enc_frame(packet, cnt), response=False
        )

    async def req(self, packet: bytes, tid: int, cnt: int, timeout: float = 3.0):
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending[tid] = future
        await self.send(packet, cnt)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(tid, None)
            return None

    async def close(self) -> None:
        if self.c is not None:
            await self.c.disconnect()

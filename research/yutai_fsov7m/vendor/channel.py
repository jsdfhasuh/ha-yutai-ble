"""MIoT BLE security-chip channel layer.

Reverse engineered from the Mi Home Android app (11.7.707). This module
implements the small transport that sits *below* MIoT Spec-v2 messages:

    control char 0x0010  (CH_CTRL)  A4 / login request / login result
    auth    char 0x0016  (CH_AUTH)  MNG negotiation + CTR package stream

Frame summary (all multi-byte integers little-endian):

    MNG     00 00 04 <id> <payload>
    MNG_ACK 00 00 05 <id> <payload>
    CTR     00 00 00 <pkg_id> <frames_le16>      header
    ACK     00 00 01 <id> [seq_le16 ...]         RCV_RDY = 00 00 01 01
    SINGLE  00 00 02 <pkg_id> <payload>
    S_ACK   00 00 03 <id>

The security-chip channel does not append a CRC32; that belongs to the older
RC4 token channel which this device does not use.
"""

from __future__ import annotations

import asyncio


def _uuid(short: int) -> str:
    return "0000%04x-0000-1000-8000-00805f9b34fb" % short


CH_CTRL = _uuid(0x0010)
CH_AUTH = _uuid(0x0016)

MNG = 4
CTR = 0
ACK = 1
SINGLE_CTR = 2
SINGLE_ACK = 3
MNG_ACK = 5


class Channel:
    """Drives the MNG handshake and the CTR package stream."""

    def __init__(self, client, log=print):
        self.c = client
        self.log = log
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.acks: asyncio.Queue[bytes] = asyncio.Queue()
        self.recv_pkts: asyncio.Queue[bytes] = asyncio.Queue()
        self.dmtu = 242
        self.frames: dict[int, dict[int, bytes]] = {}
        # Every raw notification is kept here. Callers clear this list to
        # delimit a request/response window instead of matching callbacks.
        self.rxlog: list[bytes] = []
        self._pending_ctr = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        for ch in (CH_CTRL, CH_AUTH):
            await self.c.start_notify(ch, self._on_notify)
        self._task = asyncio.create_task(self._loop())

    def _on_notify(self, _sender, data) -> None:
        b = bytes(data)
        self.log("  <<< %s" % b.hex())
        self.rxlog.append(b)
        self.q.put_nowait(b)

    # ------------------------------------------------------------------- loop
    async def _loop(self) -> None:
        while True:
            b = await self.q.get()
            if len(b) < 4:
                continue
            mtype, pid = b[2], b[3]
            payload = b[4:]
            if mtype == MNG:
                await self._on_mng(pid, payload)
            elif mtype == CTR:
                frames = int.from_bytes(payload[:2], "little") if len(payload) >= 2 else 1
                self.frames[pid] = {}
                self._pending_ctr = (pid, frames)
                await self._send(bytes([0, 0, ACK, pid, 1, 1]))
                self.log("    (ctr pkg=%d frames=%d announced)" % (pid, frames))
            elif mtype in (ACK, SINGLE_CTR, SINGLE_ACK):
                self.recv_pkts.put_nowait(b)
                if len(b) == 4:
                    self.acks.put_nowait(bytes([b[2], b[3]]))

    async def _on_mng(self, pid: int, payload: bytes) -> None:
        if pid == 0:
            dev = payload[1] if len(payload) > 1 else 242
            self.dmtu = min(self.dmtu, dev)
            self.log("    MNG0: device dmtu=%d -> ack %d" % (dev, self.dmtu))
            await self._send(bytes([0, 0, MNG_ACK, 0, 6, self.dmtu]))
        elif pid == 1:
            n = len(payload) + 2
            size = n if n == self.dmtu else 18
            self.dmtu = size
            self.log("    MNG1: mtu test len=%d -> dmtu=%d" % (len(payload), size))
            await self._send(bytes([0, 0, MNG_ACK, 1]) + bytes([size]) * (size - 2))

    # ------------------------------------------------------------------ send
    async def _send(self, data: bytes) -> None:
        self.log("  >>> %s" % data.hex())
        await self.c.write_gatt_char(CH_AUTH, data, response=False)

    async def wait_mng_done(self, timeout: float = 3.0) -> None:
        """MNG1 is ~240 bytes; give the device time to finish the exchange."""
        await asyncio.sleep(0.6)

    async def send_data(self, pkg_id: int, payload: bytes, timeout: float = 6.0) -> None:
        """Send a payload as a CTR package stream."""
        count = max(1, (len(payload) + self.dmtu - 1) // self.dmtu)
        await self._send(bytes([0, 0, CTR, pkg_id]) + count.to_bytes(2, "little"))
        await asyncio.sleep(0.35)
        for i in range(count):
            chunk = payload[i * self.dmtu:(i + 1) * self.dmtu]
            await self._send((i + 1).to_bytes(2, "little") + chunk)
            await asyncio.sleep(0.12)
        await asyncio.sleep(0.5)

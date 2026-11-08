"""Cancellation-safe priority gate; active transactions are never preempted."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class RequestInterrupted(Exception):
    """A waiting request was interrupted before entering the protected section."""


class RequestQueue:
    """Serve manual requests first, retaining FIFO order within each priority."""

    def __init__(self) -> None:
        self._waiting = []
        self._owner = None
        self._sequence = 0

    def _wake_next(self) -> None:
        if self._owner is not None:
            return
        while self._waiting:
            ticket = min(self._waiting, key=lambda item: item[:2])
            self._waiting.remove(ticket)
            future = ticket[2]
            if not future.done():
                self._owner = ticket
                future.set_result(None)
                return

    @property
    def manual_waiting(self) -> bool:
        return any(ticket[0] == 0 and not ticket[2].done() for ticket in self._waiting)

    @asynccontextmanager
    async def hold(self, *, manual: bool = False, interrupt: asyncio.Event | None = None):
        self._sequence += 1
        ticket = (0 if manual else 1, self._sequence, asyncio.get_running_loop().create_future())
        self._waiting.append(ticket)
        self._wake_next()
        try:
            if interrupt is not None and not ticket[2].done():
                interrupted = asyncio.create_task(interrupt.wait())
                try:
                    done, _ = await asyncio.wait((ticket[2], interrupted),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if interrupted in done:
                        raise RequestInterrupted
                finally:
                    # Listen only while acquiring, never during an active transaction.
                    interrupted.cancel()
                    await asyncio.gather(interrupted, return_exceptions=True)
            await ticket[2]
            if interrupt is not None and interrupt.is_set():
                raise RequestInterrupted
            yield
        finally:
            ticket[2].cancel()
            if ticket in self._waiting:
                self._waiting.remove(ticket)
            if self._owner is ticket:
                self._owner = None
            self._wake_next()

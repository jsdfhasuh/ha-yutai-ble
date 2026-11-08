"""Cancellation-safe priority gate; active transactions are never preempted."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


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
    async def hold(self, *, manual: bool = False):
        self._sequence += 1
        ticket = (0 if manual else 1, self._sequence, asyncio.get_running_loop().create_future())
        self._waiting.append(ticket)
        self._wake_next()
        try:
            await ticket[2]
            yield
        finally:
            if ticket in self._waiting:
                self._waiting.remove(ticket)
            if self._owner is ticket:
                self._owner = None
            self._wake_next()

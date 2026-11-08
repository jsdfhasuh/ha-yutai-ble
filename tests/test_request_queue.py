"""Scheduling and cancellation tests without hardware or HA."""

import asyncio
import unittest

from _load import integration

RequestQueue = integration("request_queue").RequestQueue
RequestInterrupted = integration("request_queue").RequestInterrupted


class ObservedEvent(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def wait(self):
        self.started.set()
        try:
            return await super().wait()
        finally:
            self.finished.set()


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_withdraws_waiter_even_if_event_is_cleared(self):
        queue = RequestQueue()
        interrupt = ObservedEvent()
        entered = []

        async def request():
            async with queue.hold(interrupt=interrupt):
                entered.append("read")

        async with queue.hold():
            owner = queue._owner
            read = asyncio.create_task(request())
            await interrupt.started.wait()
            interrupt.set()
            interrupt.clear()
            with self.assertRaises(RequestInterrupted):
                await asyncio.wait_for(read, 0.5)
            self.assertEqual(entered, [])
            self.assertIs(queue._owner, owner)
            self.assertEqual(queue._waiting, [])
            self.assertTrue(interrupt.finished.is_set())
        self.assertIsNone(queue._owner)

    async def test_interrupt_after_grant_releases_slot_before_request_enters(self):
        queue = RequestQueue()
        interrupt = ObservedEvent()
        entered = []

        async def request(name, signal=None):
            async with queue.hold(interrupt=signal):
                entered.append(name)

        async with queue.hold():
            read = asyncio.create_task(request("read", interrupt))
            await interrupt.started.wait()
            following = asyncio.create_task(request("following"))
            await asyncio.sleep(0)
        # The read has been granted the gate, but has not resumed its acquisition.
        interrupt.set()
        results = await asyncio.wait_for(asyncio.gather(read, following, return_exceptions=True), 0.5)
        self.assertIsInstance(results[0], RequestInterrupted)
        self.assertEqual(entered, ["following"])
        self.assertTrue(interrupt.finished.is_set())
        self.assertIsNone(queue._owner)
        self.assertEqual(queue._waiting, [])

    async def test_cancellation_racing_interrupt_cleans_waiting_or_granted_ticket(self):
        for granted in (False, True):
            for interrupt_first in (False, True):
                with self.subTest(granted=granted, interrupt_first=interrupt_first):
                    queue = RequestQueue()
                    interrupt = ObservedEvent()
                    entered = []

                    async def request(name, signal=None):
                        async with queue.hold(interrupt=signal):
                            entered.append(name)

                    holder = queue.hold()
                    await holder.__aenter__()
                    owner = queue._owner
                    read = asyncio.create_task(request("read", interrupt))
                    await interrupt.started.wait()
                    following = asyncio.create_task(request("following"))
                    await asyncio.sleep(0)
                    if granted:
                        await holder.__aexit__(None, None, None)
                    if interrupt_first:
                        interrupt.set()
                    read.cancel()
                    if not interrupt_first:
                        interrupt.set()
                    await asyncio.sleep(0)
                    read.cancel()
                    result = await asyncio.wait_for(asyncio.gather(read, return_exceptions=True), 0.5)
                    self.assertIsInstance(result[0], asyncio.CancelledError)
                    if not granted:
                        self.assertIs(queue._owner, owner)
                        await holder.__aexit__(None, None, None)
                    await asyncio.wait_for(following, 0.5)
                    self.assertEqual(entered, ["following"])
                    self.assertTrue(interrupt.finished.is_set())
                    self.assertIsNone(queue._owner)
                    self.assertEqual(queue._waiting, [])

    async def test_interrupt_listener_is_drained_before_active_transaction(self):
        queue = RequestQueue()
        interrupt = ObservedEvent()
        entered, release, following_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def request():
            async with queue.hold(interrupt=interrupt):
                entered.set()
                await release.wait()

        async def following_request():
            async with queue.hold():
                following_entered.set()

        async with queue.hold():
            active = asyncio.create_task(request())
            await interrupt.started.wait()
            following = asyncio.create_task(following_request())
            await asyncio.sleep(0)
        await asyncio.wait_for(entered.wait(), 0.5)
        self.assertTrue(interrupt.finished.is_set())
        interrupt.set()
        await asyncio.sleep(0)
        self.assertFalse(active.done())
        self.assertFalse(following_entered.is_set())
        release.set()
        await asyncio.wait_for(asyncio.gather(active, following), 0.5)
        self.assertIsNone(queue._owner)
        self.assertEqual(queue._waiting, [])

    async def test_manual_fifo_precedes_waiting_reads_without_interrupting_owner(self):
        queue = RequestQueue()
        order = []

        async def request(name, manual=False):
            async with queue.hold(manual=manual):
                order.append(name)

        async with queue.hold():
            tasks = [asyncio.create_task(request("read1")), asyncio.create_task(request("on", True)),
                     asyncio.create_task(request("read2")), asyncio.create_task(request("off", True))]
            await asyncio.sleep(0)
            self.assertEqual(order, [])
        await asyncio.gather(*tasks)
        self.assertEqual(order, ["on", "off", "read1", "read2"])
        self.assertIsNone(queue._owner)
        self.assertEqual(queue._waiting, [])

    async def test_cancelled_waiter_and_just_granted_owner_release_queue(self):
        queue = RequestQueue()
        entered = []

        async def request(name):
            async with queue.hold(manual=True):
                entered.append(name)

        async with queue.hold():
            cancelled = asyncio.create_task(request("cancelled"))
            granted = asyncio.create_task(request("granted"))
            next_one = asyncio.create_task(request("next"))
            await asyncio.sleep(0)
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
        # It owns the gate now but has not resumed from its acquire future.
        granted.cancel()
        await asyncio.gather(granted, next_one, return_exceptions=True)
        self.assertEqual(entered, ["next"])
        self.assertIsNone(queue._owner)
        self.assertEqual(queue._waiting, [])

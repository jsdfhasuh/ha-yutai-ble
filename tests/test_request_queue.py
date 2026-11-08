"""Scheduling and cancellation tests without hardware or HA."""

import asyncio
import unittest

from _load import integration

RequestQueue = integration("request_queue").RequestQueue


class QueueTests(unittest.IsolatedAsyncioTestCase):
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

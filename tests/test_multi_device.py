"""Multiple entries never share identity, credentials, state or backoff."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_coordinator import coordinator_module, UpdateFailed
from test_models import config_data, report


class MultiDeviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.first = coordinator_module.YutaiCoordinator(None, SimpleNamespace(data=config_data(), options={}))
        other = {**config_data(), "address": "02:00:00:00:00:02", "gatt_ltmk": "cd" * 32}
        self.second = coordinator_module.YutaiCoordinator(None, SimpleNamespace(data=other, options={}))

    async def asyncTearDown(self):
        await self.first.async_shutdown()
        await self.second.async_shutdown()

    async def test_crossed_reports_fail_only_the_affected_entry(self):
        self.first.bridge.read = AsyncMock(return_value=report())
        self.second.bridge.read = AsyncMock(return_value=report())
        await self.first.async_request_refresh()
        with self.assertRaises(UpdateFailed):
            await self.second._async_update_data()
        self.assertEqual(self.first.consecutive_failures, 0)
        self.assertEqual(self.first.update_interval.total_seconds(), 60)
        self.assertEqual(self.second.consecutive_failures, 1)
        self.assertEqual(self.second.update_interval.total_seconds(), 120)
        self.assertEqual(self.first.data.measurements["power"], 7)
        self.assertIsNone(self.second.data)
        self.first.bridge.read.assert_awaited_once_with(
            self.first.device_config, interrupt=self.first._manual_requested)
        self.second.bridge.read.assert_awaited_once_with(
            self.second.device_config, interrupt=self.second._manual_requested)
        self.second.bridge.read.return_value = {**report(), "address": self.second.device_config.address}
        await self.second.async_request_refresh()
        self.assertEqual(self.second.consecutive_failures, 0)
        self.assertIsNot(self.first.data, self.second.data)

    async def test_unloading_one_entry_does_not_close_another(self):
        await self.first.async_shutdown()
        self.second.bridge.read = AsyncMock(return_value={**report(), "address": self.second.device_config.address})
        await self.second.async_request_refresh()
        self.assertTrue(self.second.last_update_success)
        self.assertFalse(self.second._closing)

"""Worker target lookup tests with synthetic BlueZ objects."""

import unittest
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "yutai_ble" / "protocol"))
import bridge_worker as worker
import read_probe as read


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    def target(self, number):
        return read.validated_input({"address": f"02:00:00:00:00:{number:02X}",
                                     "model": "yutai.plug.fsov8m", "gatt_ltmk": f"{number:02x}" * 32})

    def test_lookup_uses_request_address_without_global_mutation(self):
        first, second = self.target(1), self.target(2)
        objects = {
            "/one": {"org.bluez.Device1": {"Address": first.address, "Connected": False}},
            "/two": {"org.bluez.Device1": {"Address": second.address, "Connected": False}},
        }
        self.assertEqual(read.find_target(objects, first.address)[0], "/one")
        self.assertEqual(read.find_target(objects, second.address)[0], "/two")
        self.assertNotEqual(first.key, second.key)
        self.assertNotIn(first.key.hex(), repr(first))
        objects["/duplicate"] = objects["/two"]
        with self.assertRaises(read.ProbeError):
            read.find_target(objects, second.address)
        self.assertEqual(read.find_target(objects, first.address)[0], "/one")

    async def test_worker_rejects_crossed_success_report(self):
        first, second = self.target(1), self.target(2)
        result = {"address": second.address, "model": second.model,
                  "status": "settings_scan_succeeded", "relay_writes": 0,
                  "authentication_verified": True, "f0_verified": True,
                  "connected_after_cleanup": False}
        with patch.object(read, "run_probe", AsyncMock(return_value=result)) as probe:
            self.assertEqual((await worker.run_request(first, None))["status"], "error")
        self.assertIs(probe.call_args.args[0], first)

"""Worker dispatch and attestations using synthetic reports, never BLE or keys."""

import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch


HERE = Path(__file__).resolve().parents[2] / "custom_components" / "yutai_ble" / "protocol"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bridge_worker as worker
import read_probe as read
import settings_scan as scan
from test_settings_control import BASE, PLAN, get_response


def input_data(operation="relay", **extra):
    return {"address": "02:00:00:00:00:01", "model": "yutai.plug.fsov8m", "gatt_ltmk": "ab" * 32,
            "operation": operation, **extra}


def relay_report(value=False, initial=True):
    words = {**BASE, (2, 1): value}
    properties = []
    for offset in range(0, len(PLAN), 4):
        batch = PLAN[offset:offset + 4]
        properties.extend(scan.parse_read_response(get_response(9, batch, words), 9, batch))
    count = int(initial is not value)
    return {
        "address": "02:00:00:00:00:01", "model": "yutai.plug.fsov8m", "status": "relay_verified",
        "operation": "relay_control", "authentication_verified": True, "f0_verified": True,
        "connected_after_cleanup": False, "relay_writes": count, "relay_write_attempts": count,
        "requested_value": value, "initial_relay_state": initial, "observed_relay_state": value,
        "maybe_executed": bool(count), "verified": True, "initial_snapshot_complete": True,
        "verification_snapshot_complete": True, "settings_unchanged": True, "properties": properties,
    }


class InputTests(unittest.TestCase):
    def test_relay_dispatch_requires_exact_boolean_and_exact_fields(self):
        for value in (False, True):
            key, request = worker.validated_input(input_data(value=value))
            self.assertEqual(len(key.key), 32)
            self.assertIs(request, value)
        for value in (0, 1, 1.0, "off", None, [], {}):
            with self.subTest(value=value), self.assertRaises(read.ProbeError):
                worker.validated_input(input_data(value=value))
        for data in (input_data(), input_data(value=False, field="indicator_mode"),
                     input_data(value=False, properties=[[2, 1]])):
            with self.assertRaises(read.ProbeError):
                worker.validated_input(data)

    def test_existing_read_and_setting_schemas_are_unchanged(self):
        self.assertIsNone(worker.validated_input(input_data("read"))[1])
        self.assertEqual(worker.validated_input(input_data(
            "setting", field="indicator_mode", value="follow"))[1], ("indicator_mode", "follow"))
        with self.assertRaises(read.ProbeError):
            worker.validated_input(input_data("read", value=False))
        with self.assertRaises(read.ProbeError):
            worker.validated_input(input_data("setting", field="relay", value=False))

    def test_main_accepts_verified_relay_exit_status_without_running_protocol(self):
        stdin = type("Input", (), {"buffer": io.BytesIO(json.dumps(input_data(value=False)).encode())})()
        stdout = io.StringIO()

        def run(coroutine):
            coroutine.close()
            return relay_report()

        with patch.object(worker.sys, "stdin", stdin), patch.object(worker.sys, "argv", ["worker"]), patch.object(
                worker.sys, "stdout", stdout), patch.object(worker.asyncio, "run", run), patch.object(
                worker.logging, "disable"):
            self.assertEqual(worker.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "relay_verified")
        self.assertNotIn("ab" * 32, stdout.getvalue())


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_set_and_noop_reports_preserve_all_required_proofs(self):
        for initial in (False, True):
            for value in (False, True):
                with self.subTest(initial=initial, value=value):
                    with patch.object(worker.read, "run_probe", AsyncMock(
                            return_value=relay_report(value, initial))) as probe:
                        result = await worker.run_request(worker.validated_input(input_data("read"))[0], value)
                    self.assertEqual(result["status"], "relay_verified")
                    self.assertIs(result["verified"], True)
                    self.assertEqual(result["relay_writes"], int(initial is not value))
                    self.assertEqual(len(result["properties"]), 13)
                    self.assertEqual(len(result["settings"]), 7)
                    self.assertEqual(len(result["capabilities"]), 7)
                    self.assertEqual(probe.await_count, 1)

    async def test_old_operations_continue_rejecting_any_relay_write(self):
        for request, status in ((None, "settings_scan_succeeded"),
                                (("indicator_mode", "follow"), "setting_verified")):
            for count in (0, 1, True):
                with self.subTest(request=request, count=count):
                    report = relay_report()
                    report.update(status=status, relay_writes=count)
                    with patch.object(worker.read, "run_probe", AsyncMock(return_value=report)):
                        result = await worker.run_request(worker.validated_input(input_data("read"))[0], request)
                    expected = ("read_verified" if request is None else "setting_verified") if type(count) is int and count == 0 else "error"
                    self.assertEqual(result["status"], expected)

    async def test_relay_rejects_inconsistent_or_missing_attestations(self):
        mutations = (
            {"relay_writes": True}, {"relay_writes": 2}, {"relay_write_attempts": True},
            {"relay_write_attempts": 0}, {"requested_value": 0}, {"initial_relay_state": 1},
            {"observed_relay_state": 0}, {"observed_relay_state": True},
            {"maybe_executed": False}, {"verified": False},
            {"initial_snapshot_complete": False}, {"verification_snapshot_complete": False},
            {"settings_unchanged": False}, {"authentication_verified": False}, {"f0_verified": False},
            {"connected_after_cleanup": True}, {"cleanup_error_code": "still_connected"},
            {"address": "02:00:00:00:00:02"}, {"model": "yutai.plug.fsov7m"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                report = {**relay_report(), **mutation}
                with patch.object(worker.read, "run_probe", AsyncMock(return_value=report)):
                    self.assertEqual((await worker.run_request(worker.validated_input(input_data("read"))[0], False))["status"], "error")

    async def test_reported_relay_must_match_independent_property_and_complete_snapshot(self):
        report = relay_report()
        report["properties"][0].update(value=True, raw_hex="01")
        with patch.object(worker.read, "run_probe", AsyncMock(return_value=report)):
            self.assertEqual((await worker.run_request(worker.validated_input(input_data("read"))[0], False))["status"], "error")
        report = copy.deepcopy(relay_report())
        report["properties"].pop()
        with patch.object(worker.read, "run_probe", AsyncMock(return_value=report)):
            with self.assertRaises(read.ProbeError):
                await worker.run_request(worker.validated_input(input_data("read"))[0], False)


if __name__ == "__main__":
    unittest.main()

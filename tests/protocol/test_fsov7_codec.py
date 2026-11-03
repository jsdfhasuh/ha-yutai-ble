"""Exercise ordinary-model dispatch and real controllers against synthetic frames."""

from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components/yutai_ble/protocol"))

import bridge_worker as worker
import fsov7_codec as codec
import read_probe as read
import relay_control as relay
import settings_control as control
import settings_scan as scan
from test_settings_control import BASE, FakeRaw, UPSTREAM, get_response, pack, set_response


def input_data(operation="read", **extra):
    return {"address": "02:00:00:00:00:07", "model": "yutai.plug.fsov7m",
            "gatt_ltmk": "ab" * 32, "operation": operation, **extra}


def properties(word=0x20400):
    words = {**BASE, (5, 4): word}
    items = []
    for offset in range(0, len(codec.READ_PROPERTIES), 4):
        batch = codec.READ_PROPERTIES[offset:offset + 4]
        items.extend(scan.parse_read_response(get_response(9, batch, words), 9, batch))
    return items


class CodecTests(unittest.TestCase):
    def test_decodes_only_observed_fields_and_options(self):
        settings, capabilities = codec.decode_settings(properties())
        self.assertEqual(settings, {"indicator_mode": "follow", "power_on_state": "off"})
        self.assertEqual(capabilities["indicator_mode"]["options"], ["follow", "off"])
        self.assertEqual(set(capabilities), set(settings))

    def test_roundtrips_all_confirmed_modes_preserving_unrelated_bits(self):
        for field, (mask, options) in codec.FIELDS.items():
            for initial_bits in options.values():
                initial = (0x80020400 & ~mask) | initial_bits
                for value, target_bits in options.items():
                    target = codec.target_word(field, value, initial)
                    self.assertEqual((target ^ initial) & ~mask, 0)
                    self.assertEqual(target & mask, target_bits)
                    self.assertEqual(codec.decode_settings(properties(target))[0][field], value)

    def test_unconfirmed_fields_and_values_rejected_at_worker_boundary(self):
        for field, value in (("indicator_mode", "inverse"), ("indicator_mode", "always_on"),
                             ("indicator_dnd_enabled", True), ("double_click_seconds", 120),
                             ("cycle_enabled", True), ("power_on_state", 1)):
            with self.subTest(field=field, value=value), self.assertRaises(read.ProbeError):
                worker.validated_input(input_data("setting", field=field, value=value))
        for field, value in (("indicator_mode", "off"), ("power_on_state", "memory")):
            target, setting = worker.validated_input(input_data("setting", field=field, value=value))
            self.assertEqual(target.model, "yutai.plug.fsov7m")
            self.assertEqual(setting, (field, value))

    def test_unknown_initial_mode_is_not_overwritten_or_exposed(self):
        self.assertNotIn("indicator_mode", codec.decode_settings(properties(0x24400))[0])
        with self.assertRaises(read.ProbeError):
            codec.target_word("indicator_mode", "off", 0x24400)

    def test_snapshot_integrity(self):
        for mutation in (lambda item: item.update(value=True), lambda item: item.update(raw_hex="00000000")):
            items = properties()
            mutation(next(item for item in items if (item["siid"], item["piid"]) == (5, 4)))
            with self.assertRaises(read.ProbeError):
                codec.decode_settings(items)
        items = properties()
        with self.assertRaises(read.ProbeError):
            codec.decode_settings(items + [items[0]])


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, field=None, value=False, *, initial_relay=True, fail_after_set=False):
        self.words = {**BASE, (5, 4): 0x20400, (2, 1): initial_relay}
        self.result, raw = {}, FakeRaw()
        factory = (relay.make_relay_client(UPSTREAM, value, codec_module=codec) if field is None else
                   control.make_control_client(UPSTREAM, field, value, codec_module=codec))
        self.client = factory(object(), b"x" * 32, raw, self.result)
        raw.owner = self.client
        self.client.authenticated = self.client.f0_verified = True
        self.client.sent = [(pack(1, 0xF0), 0)]
        self.sets = []

        async def exchange(packet, tid, counter):
            self.client.authorize_send(packet, counter)
            self.client.sent.append((packet, counter))
            if packet[4] == 0:
                identity = (2, 1) if field is None else (5, 4)
                self.sets.append(packet)
                self.words[identity] = bool(packet[-1]) if field is None else int.from_bytes(packet[-4:], "little")
                response = set_response(tid, identity)
            else:
                if fail_after_set and self.sets:
                    raise TimeoutError("synthetic")
                batch = self.client.expected_responses[tid][1]
                response = get_response(tid, batch, self.words)
            return self.client.parse_pending_response(response)

        self.client.exchange = exchange

    async def test_ordinary_relay_both_directions_and_noop(self):
        for initial in (True, False):
            for value in (True, False):
                self.prepare(value=value, initial_relay=initial)
                await self.client.control()
                self.assertEqual(self.result["status"], "relay_verified")
                self.assertEqual(len(self.sets), int(initial is not value))
                self.assertEqual(set(self.result["capabilities"]), {"indicator_mode", "power_on_state"})
                self.assertTrue(self.result["settings_unchanged"])

    async def test_ordinary_settings_exact_write_and_readback(self):
        for field, value in (("indicator_mode", "off"), ("indicator_mode", "follow"),
                             ("power_on_state", "memory"), ("power_on_state", "on"), ("power_on_state", "off")):
            self.prepare(field, value)
            await self.client.control()
            self.assertEqual(self.result["status"], "setting_verified")
            self.assertEqual(self.result["settings"][field], value)
            expected_writes = 0 if (field, value) in {("indicator_mode", "follow"), ("power_on_state", "off")} else 1
            self.assertEqual(len(self.sets), expected_writes)
            self.assertTrue(self.result["relay_state_unchanged"])

    async def test_failed_ordinary_writes_never_retry(self):
        for field, value in ((None, False), ("indicator_mode", "off")):
            self.prepare(field, value, fail_after_set=True)
            with self.assertRaises(TimeoutError):
                await self.client.control()
            self.assertEqual(len(self.sets), 1)
            self.assertFalse(self.result["verified"])

    async def test_worker_selects_each_model_without_mutating_other_codec(self):
        for model, expected in (("yutai.plug.fsov7m", 2), ("yutai.plug.fsov8m", 7), ("yutai.plug.fsov7m", 2)):
            target, request = worker.validated_input(input_data(model=model))
            result = {"address": target.address, "model": model, "status": "settings_scan_succeeded",
                      "authentication_verified": True, "f0_verified": True, "relay_writes": 0,
                      "connected_after_cleanup": False, "properties": properties()}
            with patch.object(read, "run_probe", AsyncMock(return_value=result)):
                response = await worker.run_request(target, request)
            self.assertEqual(response["status"], "read_verified")
            self.assertEqual(len(response["capabilities"]), expected)

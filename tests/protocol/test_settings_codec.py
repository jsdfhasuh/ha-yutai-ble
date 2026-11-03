"""Offline field-isolation tests; no BLE libraries or real credentials."""

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "yutai_ble" / "protocol"))

import read_probe as read
import settings_codec as codec
import bridge_worker as worker


def word_property(value, **changes):
    return {"siid": 5, "piid": 4, "status": 0, "typelen": 0x5004,
            "type_name": "uint32", "value": value,
            "raw_hex": value.to_bytes(4, "little").hex(), **changes}


class CodecTests(unittest.TestCase):
    def test_observed_modes_and_other_bits_preserved(self):
        observed = {"follow": 0x20420, "inverse": 0x24420,
                    "off": 0x28420, "always_on": 0x2C420}
        for current in observed.values():
            for name, expected in observed.items():
                with self.subTest(current=current, name=name):
                    self.assertEqual(codec.target_word("indicator_mode", name, current), expected)
                    high = codec.target_word("indicator_mode", name, current | 0x80000000)
                    self.assertEqual(high, expected | 0x80000000)
                    self.assertEqual((current ^ high) & ~(0xC000 | 0x80000000), 0)

    def test_invalid_numbers_are_not_coerced(self):
        for value in [True, False, -1, 2**32, 1.0, "132128", None]:
            with self.subTest(value=value), self.assertRaises(read.ProbeError):
                codec.target_word("indicator_mode", "follow", value)

    def test_unconfirmed_and_actuator_fields_rejected(self):
        for field in ["relay", "countdown_start", "countdown_cancel", "clock", 1]:
            with self.subTest(field=field), self.assertRaises(read.ProbeError):
                codec.validate_setting(field, "on")

    def test_snapshot_only_exposes_observed_capabilities(self):
        settings, capabilities = codec.decode_settings([word_property(0x2C420)])
        self.assertEqual(settings, {"indicator_mode": "always_on", "power_on_state": "off",
                                   "double_click_enabled": False})
        self.assertEqual(set(capabilities), {"indicator_mode", "power_on_state", "double_click_enabled"})
        self.assertEqual(capabilities["indicator_mode"]["options"],
                         ["follow", "inverse", "off", "always_on"])
        self.assertEqual(codec.decode_settings([word_property(0x20420, status=1)]), ({}, {}))
        self.assertEqual(codec.decode_settings([word_property(0x20420, typelen=1)]), ({}, {}))

    def test_duplicate_and_inconsistent_snapshots_rejected(self):
        prop = word_property(0x20420)
        with self.assertRaises(read.ProbeError):
            codec.decode_settings([prop, prop])
        with self.assertRaises(read.ProbeError):
            codec.decode_settings([word_property(0x20420, raw_hex="00000000")])

    def test_power_changes_preserve_indicator_and_unknown_bits(self):
        for initial in [0x28420, 0x29420, 0x2A420, 0x8002E420]:
            for name, bits in [("off", 0), ("on", 0x1000), ("memory", 0x2000)]:
                target = codec.target_word("power_on_state", name, initial)
                self.assertEqual(target & 0x3000, bits)
                self.assertEqual((initial ^ target) & ~0x3000, 0)
        with self.assertRaises(read.ProbeError):
            codec.target_word("power_on_state", "on", 0x3000)

    def test_dnd_high_bit_only_and_boolean_type_required(self):
        self.assertEqual(codec.target_word("indicator_dnd_enabled", True, 0x4B03C004), 0xCB03C004)
        self.assertEqual(codec.target_word("indicator_dnd_enabled", False, 0xCB03C004), 0x4B03C004)
        for value in [0, 1, "true", None]:
            with self.assertRaises(read.ProbeError):
                codec.validate_setting("indicator_dnd_enabled", value)

    def test_observed_time_vectors_and_restoration(self):
        original = 0xCB03C004  # Enabled, 20:00--08:00, unknown low bits 4.
        start = codec.target_word("indicator_dnd_start", "21:17", original)
        self.assertEqual(start, 0xCFD3C004)
        end = codec.target_word("indicator_dnd_end", "23:43", start)
        self.assertEqual(end, 0xCFDB1E04)
        restored = codec.target_word("indicator_dnd_start", "20:00", end)
        restored = codec.target_word("indicator_dnd_end", "08:00", restored)
        self.assertEqual(restored, original)
        prop = word_property(end, piid=6)
        settings, capabilities = codec.decode_settings([prop])
        self.assertEqual(settings, {"indicator_dnd_enabled": True,
                                   "indicator_dnd_start": "21:17", "indicator_dnd_end": "23:43"})
        self.assertEqual(capabilities["indicator_dnd_start"], {"kind": "time"})

    def test_time_boundaries_preserve_all_other_fields(self):
        for field, shift in [("indicator_dnd_start", 20), ("indicator_dnd_end", 9)]:
            mask = 0x7FF << shift
            for value, minutes in [("00:00", 0), ("23:59", 1439)]:
                target = codec.target_word(field, value, 0xCB03C1A5)
                self.assertEqual((target ^ 0xCB03C1A5) & ~mask, 0)
                self.assertEqual((target & mask) >> shift, minutes)
            for value in ["24:00", "20:60", "1:00", "01:00:00", 60, None]:
                with self.assertRaises(read.ProbeError):
                    codec.validate_setting(field, value)
            with self.assertRaises(read.ProbeError):
                codec.target_word(field, "00:00", 0x7FF << shift)

    def test_double_click_enable_only_changes_confirmed_bit4(self):
        self.assertEqual(codec.target_word("double_click_enabled", True, 0x20420), 0x20430)
        self.assertEqual(codec.target_word("double_click_enabled", False, 0x20430), 0x20420)

    def test_duration_observed_vectors_short_long_and_high_bit(self):
        vectors = [(1, 0x00200005), (17, 0x02200005), (120, 0x07A00005),
                   (3780, 0x0F400005), (3840, 0x0F600005),
                   (7440, 0x16E00005), (86340, 0xBB400005)]
        for seconds, word in vectors:
            with self.subTest(seconds=seconds):
                self.assertEqual(codec.target_word("double_click_seconds", seconds, 0x00200005), word)
                settings, _caps = codec.decode_settings([word_property(word, piid=5)])
                self.assertEqual(settings["double_click_seconds"], seconds)
                self.assertEqual(codec.target_word("double_click_seconds", 1, word), 0x00200005)

    def test_duration_boundary_and_unknown_low_bits_preserved(self):
        initial = 0x02200005 | 0x155550
        for seconds in [1, 59, 60, 120, 3600, 86340]:
            target = codec.target_word("double_click_seconds", seconds, initial)
            self.assertEqual(target & 0x1FFFFF, initial & 0x1FFFFF)
            self.assertEqual(codec.duration_seconds(target >> 21), seconds)
        for value in [0, True, 17.0, 61, 137, 86341, 86400, "17"]:
            with self.subTest(value=value), self.assertRaises(read.ProbeError):
                codec.validate_setting("double_click_seconds", value)
        for code in [0, 1499, 2047]:
            with self.assertRaises(read.ProbeError):
                codec.target_word("double_click_seconds", 17, (code << 21) | 5)

    def test_input_requires_supported_identity_no_arbitrary_property_or_relay(self):
        # This is a synthetic test value, not a real device key.
        payload = {"address": "02:00:00:00:00:01", "model": "yutai.plug.fsov8m",
                   "gatt_ltmk": "00" * 32, "operation": "read"}
        target, setting = worker.validated_input(payload)
        self.assertEqual(target.key, bytes(32))
        self.assertEqual(target.address, payload["address"])
        self.assertIsNone(setting)
        for changes in [{"operation": "relay"}, {"properties": [[2, 1]]},
                        {"model": "yutai.plug.fsov7m"},
                        {"operation": "setting", "field": "relay", "value": True}]:
            with self.subTest(changes=changes), self.assertRaises(read.ProbeError):
                worker.validated_input({**payload, **changes})


if __name__ == "__main__":
    unittest.main()

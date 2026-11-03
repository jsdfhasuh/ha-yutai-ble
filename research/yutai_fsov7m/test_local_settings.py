"""Pure local-word tests; no Bluetooth, SSH, ADB, or cloud calls."""
import unittest

import local_settings as local


BASE = 0x1FCB4438


class DecodeTests(unittest.TestCase):
    def test_observed_start_time_enable_and_repeat_vectors(self):
        vectors = (
            (0x3FCB4438, 360, 1080, False, False, True),
            (0x1FCB4438, 360, 1080, False, False, False),
            (0x9FEB8C38, 1393, 1080, True, False, False),
            (0x1FEB8C38, 1393, 1080, False, False, False),
            (0x5FEB8D77, 1393, 1399, False, True, False),
            (0x1FEB8D77, 1393, 1399, False, False, False),
        )
        for word, start_min, end_min, start_enabled, end_enabled, repeat in vectors:
            with self.subTest(word=word):
                self.assertEqual(local.decode_local_word(word), {
                    "start_min": start_min, "end_min": end_min, "start_enabled": start_enabled,
                    "end_enabled": end_enabled, "repeat": repeat, "weekday_mask": 127,
                })

    def test_weekday_bits_are_exposed_only_as_raw_mask(self):
        expected = local.decode_local_word(BASE)
        for weekday_mask in (0, 1, 42, 64, 85, 127):
            word = (BASE & ~local.WEEKDAY_MASK) | weekday_mask << 22
            self.assertEqual(local.decode_local_word(word), dict(expected, weekday_mask=weekday_mask))
        self.assertEqual(set(expected), local.FIELDS)

    def test_observed_complementary_weekday_vectors(self):
        for word, weekday_mask in ((0x356B8D77, 85), (0x2AAB8D77, 42)):
            with self.subTest(word=word):
                self.assertEqual(local.decode_local_word(word), {
                    "start_min": 1393, "end_min": 1399, "weekday_mask": weekday_mask,
                    "start_enabled": False, "end_enabled": False, "repeat": True,
                })

    def test_invalid_encoded_day_times_are_rejected(self):
        for name, mask, shift in (("start_min", local.START_MASK, 11), ("end_min", local.END_MASK, 0)):
            for minute in (1440, 2047):
                word = (BASE & ~mask) | minute << shift
                with self.subTest(field=name, minute=minute), self.assertRaisesRegex(ValueError, name):
                    local.decode_local_word(word)

    def test_raw_word_must_be_strict_uint32(self):
        for word in (True, False, -1, 0x100000000, None, 1.0, "1"):
            with self.subTest(word=word), self.assertRaises(ValueError):
                local.decode_local_word(word)
            with self.assertRaises(ValueError):
                local.patch_local_word(word)


class PatchTests(unittest.TestCase):
    def test_observed_start_and_enable_changes_are_independent(self):
        self.assertEqual(local.patch_local_word(BASE, start_min=1393), 0x1FEB8C38)
        self.assertEqual(local.patch_local_word(BASE, start_min=1393, start_enabled=True), 0x9FEB8C38)
        self.assertEqual(local.patch_local_word(0x9FEB8C38, start_enabled=False), 0x1FEB8C38)
        self.assertEqual(local.patch_local_word(0x1FEB8C38, start_min=360), BASE)
        self.assertEqual(local.patch_local_word(BASE, repeat=True), 0x3FCB4438)
        self.assertEqual(local.patch_local_word(0x3FCB4438, repeat=False), BASE)

    def test_observed_end_time_and_enable_changes_are_independent(self):
        self.assertEqual(local.patch_local_word(0x1FEB8C38, end_min=1399), 0x1FEB8D77)
        self.assertEqual(local.patch_local_word(0x1FEB8C38, end_min=1399, end_enabled=True), 0x5FEB8D77)
        self.assertEqual(local.patch_local_word(0x5FEB8D77, end_enabled=False), 0x1FEB8D77)
        self.assertEqual(local.patch_local_word(0x1FEB8D77, end_min=1080), 0x1FEB8C38)

    def test_no_changes_and_same_fields_preserve_original_word(self):
        for word in (BASE, 0x3FCB4438, 0x9FEB8C38, 0x1FEB8C38, 0x5FEB8D77,
                     0x1FEB8D77, 0x356B8D77, 0x2AAB8D77):
            self.assertEqual(local.patch_local_word(word), word)
            self.assertEqual(local.patch_local_word(word, **local.decode_local_word(word)), word)

    def test_observed_weekday_change_preserves_all_other_fields(self):
        self.assertEqual(local.patch_local_word(0x356B8D77, weekday_mask=42), 0x2AAB8D77)
        self.assertEqual(local.patch_local_word(0x2AAB8D77, weekday_mask=85), 0x356B8D77)
        self.assertEqual((0x356B8D77 ^ 0x2AAB8D77) & ~local.WEEKDAY_MASK, 0)

    def test_every_field_preserves_all_non_target_bits(self):
        descriptors = (
            ("start_min", local.START_MASK, (0, 1, 360, 1393, 1439)),
            ("end_min", local.END_MASK, (0, 1, 1080, 1399, 1439)),
            ("weekday_mask", local.WEEKDAY_MASK, (0, 42, 85, 127)),
            ("start_enabled", local.START_ENABLED_BIT, (False, True)),
            ("end_enabled", local.END_ENABLED_BIT, (False, True)),
            ("repeat", local.REPEAT_BIT, (False, True)),
        )
        for original in (0, BASE, 0x3FCB4438, 0x9FEB8C38, 0xFFFFFFFF):
            for name, mask, values in descriptors:
                for value in values:
                    with self.subTest(original=original, field=name, value=value):
                        changed = local.patch_local_word(original, **{name: value})
                        self.assertEqual((changed ^ original) & ~mask, 0)
                        self.assertGreaterEqual(changed, 0)
                        self.assertLessEqual(changed, 0xFFFFFFFF)

    def test_all_field_boundary_roundtrip(self):
        defined_mask = (local.START_MASK | local.END_MASK | local.WEEKDAY_MASK
                        | local.START_ENABLED_BIT | local.END_ENABLED_BIT | local.REPEAT_BIT)
        self.assertEqual(defined_mask, 0xFFFFFFFF)
        for original in (0, BASE, 0xFFFFFFFF):
            for start in (0, 1439):
                for weekday_mask in (0, 127):
                    for start_enabled in (False, True):
                        for end_enabled in (False, True):
                            for repeat in (False, True):
                                fields = {"start_min": start, "end_min": 1439 - start,
                                          "weekday_mask": weekday_mask,
                                          "start_enabled": start_enabled, "end_enabled": end_enabled,
                                          "repeat": repeat}
                                changed = local.patch_local_word(original, **fields)
                                self.assertEqual(local.decode_local_word(changed), fields)

    def test_strict_minute_ranges_and_boolean_types(self):
        for name in ("start_min", "end_min"):
            for value in (True, False, -1, 1440, 2047, None, "360", 360.0):
                with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                    local.patch_local_word(BASE, **{name: value})
        for name in ("start_enabled", "end_enabled", "repeat"):
            for value in (0, 1, None, "on", 1.0):
                with self.subTest(field=name, value=value), self.assertRaisesRegex(ValueError, "bool_required"):
                    local.patch_local_word(BASE, **{name: value})

    def test_strict_weekday_encoding_range(self):
        for value in (True, False, -1, 128, None, "42", 42.0):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "weekday_mask_out_of_range"):
                local.patch_local_word(BASE, weekday_mask=value)

    def test_unknown_day_master_clock_and_raw_fields_are_rejected(self):
        for name in ("end_time", "weekday", "weekdays", "enabled", "master", "clock", "raw", "mode"):
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, "unknown_local_field"):
                local.patch_local_word(BASE, **{name: 1})
        with self.assertRaises(TypeError):
            local.patch_local_word(BASE, word=1)

    def test_unmodified_unrecognized_time_is_preserved(self):
        self.assertEqual(local.patch_local_word(0xFFFFFFFF, repeat=False), 0xDFFFFFFF)
        self.assertEqual(local.patch_local_word(0xFFFFFFFF, start_enabled=False), 0x7FFFFFFF)
        self.assertEqual(local.patch_local_word(0xFFFFFFFF, end_enabled=False), 0xBFFFFFFF)
        fixed = local.patch_local_word(0xFFFFFFFF, start_min=360, end_min=1080)
        self.assertEqual(local.decode_local_word(fixed)["start_min"], 360)
        self.assertEqual(local.decode_local_word(fixed)["end_min"], 1080)


if __name__ == "__main__":
    unittest.main()

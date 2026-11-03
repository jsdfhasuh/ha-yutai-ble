"""Pure offline cycle word tests; no Bluetooth, SSH, ADB, or cloud calls."""
import unittest

import cycle_settings as cycle


BASE = (0x3FCB4438, 0x80000708, 0x708)


class DecodeTests(unittest.TestCase):
    def test_observed_default_words(self):
        self.assertEqual(cycle.decode_cycle_words(*BASE), {
            "start_min": 360, "end_min": 1080, "weekday_mask": 127,
            "on_seconds": 1800, "off_seconds": 1800,
            "start_state": True, "end_state": False, "repeat": True,
        })

    def test_observed_changed_times_durations_weekdays_and_states(self):
        cases = (
            ((0x3FCB4C38, BASE[1], BASE[2]), {"start_min": 361}),
            ((0x3FCB4439, BASE[1], BASE[2]), {"end_min": 1081}),
            ((0x304B4439, 0x744, 0x80000780), {
                "end_min": 1081, "weekday_mask": 65,
                "on_seconds": 1860, "off_seconds": 1920,
                "start_state": False, "end_state": True,
            }),
            ((0x104B4439, 0x744, 0x80000780), {
                "end_min": 1081, "weekday_mask": 65,
                "on_seconds": 1860, "off_seconds": 1920,
                "start_state": False, "end_state": True, "repeat": False,
            }),
        )
        for words, expected in cases:
            with self.subTest(words=words):
                decoded = cycle.decode_cycle_words(*words)
                for name, value in expected.items():
                    self.assertEqual(decoded[name], value)

    def test_schedule_high_bits_are_not_exposed(self):
        expected = cycle.decode_cycle_words(*BASE)
        for high in range(4):
            words = ((BASE[0] & 0x3FFFFFFF) | high << 30, BASE[1], BASE[2])
            self.assertEqual(cycle.decode_cycle_words(*words), expected)
        self.assertEqual(set(expected), cycle.FIELDS)

    def test_integer_words_reject_bool_negative_oversize_and_nonintegers(self):
        for index in range(3):
            for bad in (True, False, -1, 0x100000000, 1.0, None, "1"):
                words = list(BASE)
                words[index] = bad
                with self.subTest(index=index, bad=bad), self.assertRaises(ValueError):
                    cycle.decode_cycle_words(*words)
                with self.assertRaises(ValueError):
                    cycle.patch_cycle_words(*words)

    def test_invalid_encoded_minutes_are_not_reported_as_day_times(self):
        for field, shift in (("start_min", 11), ("end_min", 0)):
            for minute in (1440, 2047):
                word = (BASE[0] & ~(0x7FF << shift)) | minute << shift
                with self.subTest(field=field, minute=minute), self.assertRaisesRegex(ValueError, field):
                    cycle.decode_cycle_words(word, BASE[1], BASE[2])


class PatchTests(unittest.TestCase):
    def test_observed_single_field_vectors(self):
        cases = (
            ({"start_min": 361}, (0x3FCB4C38, BASE[1], BASE[2])),
            ({"end_min": 1081}, (0x3FCB4439, BASE[1], BASE[2])),
            ({"weekday_mask": 65, "end_min": 1081}, (0x304B4439, BASE[1], BASE[2])),
            ({"on_seconds": 1860}, (BASE[0], 0x80000744, BASE[2])),
            ({"off_seconds": 1920}, (BASE[0], BASE[1], 0x780)),
            ({"on_seconds": 1860, "start_state": False}, (BASE[0], 0x744, BASE[2])),
            ({"off_seconds": 1920, "end_state": True}, (BASE[0], BASE[1], 0x80000780)),
            ({"repeat": False}, (0x1FCB4438, BASE[1], BASE[2])),
        )
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(cycle.patch_cycle_words(*BASE, **changes), expected)

    def test_no_changes_and_same_fields_leave_original_words_unchanged(self):
        self.assertEqual(cycle.patch_cycle_words(*BASE), BASE)
        self.assertEqual(cycle.patch_cycle_words(*BASE, **cycle.decode_cycle_words(*BASE)), BASE)

    def test_each_field_changes_only_its_confirmed_mask(self):
        descriptors = (
            ("start_min", 0, 0x7FF << 11, (0, 1, 1439)),
            ("end_min", 0, 0x7FF, (0, 1, 1439)),
            ("weekday_mask", 0, 0x7F << 22, (0, 1, 65, 127)),
            ("on_seconds", 1, 0x7FFFFFFF, (0, 1, 0x7FFFFFFF)),
            ("off_seconds", 2, 0x7FFFFFFF, (0, 1, 0x7FFFFFFF)),
            ("start_state", 1, 0x80000000, (False, True)),
            ("end_state", 2, 0x80000000, (False, True)),
            ("repeat", 0, 0x20000000, (False, True)),
        )
        for high in range(8):
            original = ((BASE[0] & 0x1FFFFFFF) | high << 29, BASE[1], BASE[2])
            for name, index, mask, values in descriptors:
                for value in values:
                    with self.subTest(high=high, field=name, value=value):
                        changed = cycle.patch_cycle_words(*original, **{name: value})
                        for word_index, (before, after) in enumerate(zip(original, changed)):
                            self.assertEqual((before ^ after) & (~mask if word_index == index else 0xFFFFFFFF), 0)

    def test_all_field_boundary_roundtrip_preserves_schedule_high_bits(self):
        for high in range(4):
            original = ((BASE[0] & 0x3FFFFFFF) | high << 30, BASE[1], BASE[2])
            for value in (0, 1):
                changes = {
                    "start_min": 1439 if value else 0,
                    "end_min": 0 if value else 1439,
                    "weekday_mask": 127 if value else 0,
                    "on_seconds": 0x7FFFFFFF if value else 0,
                    "off_seconds": 0 if value else 0x7FFFFFFF,
                    "start_state": bool(value), "end_state": not bool(value),
                    "repeat": bool(value),
                }
                changed = cycle.patch_cycle_words(*original, **changes)
                self.assertEqual(cycle.decode_cycle_words(*changed), changes)
                self.assertEqual(changed[0] & 0xC0000000, original[0] & 0xC0000000)

    def test_rejects_unknown_enable_clock_and_raw_word_fields(self):
        for name in ("once", "enabled", "enable", "clock", "raw_word", "raw", "mode"):
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, "unknown_cycle_field"):
                cycle.patch_cycle_words(*BASE, **{name: 1})
        with self.assertRaises(TypeError):
            cycle.patch_cycle_words(*BASE, schedule_word=1)

    def test_rejects_invalid_field_ranges_and_bool_integer_values(self):
        for name, maximum in (("start_min", 1439), ("end_min", 1439), ("weekday_mask", 127),
                              ("on_seconds", 0x7FFFFFFF), ("off_seconds", 0x7FFFFFFF)):
            for bad in (True, False, -1, maximum + 1, 1.0, None, "1"):
                with self.subTest(field=name, bad=bad), self.assertRaises(ValueError):
                    cycle.patch_cycle_words(*BASE, **{name: bad})
        for name in ("start_state", "end_state", "repeat"):
            for bad in (0, 1, None, "on", 1.0):
                with self.subTest(field=name, bad=bad), self.assertRaisesRegex(ValueError, "bool_required"):
                    cycle.patch_cycle_words(*BASE, **{name: bad})

    def test_observed_repeat_only_change_and_restore_preserve_unknown_high_bits(self):
        before = (0x304B4439, 0x744, 0x80000780)
        after = (0x104B4439, 0x744, 0x80000780)
        self.assertEqual(cycle.patch_cycle_words(*before, repeat=False), after)
        self.assertEqual(cycle.patch_cycle_words(*after, repeat=True), before)
        self.assertTrue(cycle.decode_cycle_words(*before)["repeat"])
        self.assertFalse(cycle.decode_cycle_words(*after)["repeat"])
        for high in range(4):
            original = ((before[0] & 0x3FFFFFFF) | high << 30, before[1], before[2])
            changed = cycle.patch_cycle_words(*original, repeat=False)
            self.assertEqual(original[0] ^ changed[0], 0x20000000)
            self.assertEqual(original[0] & 0xC0000000, changed[0] & 0xC0000000)
            self.assertEqual(original[1:], changed[1:])

    def test_patching_one_field_preserves_unrecognized_unmodified_times(self):
        original = (0xFFFFFFFF, BASE[1], BASE[2])
        changed = cycle.patch_cycle_words(*original, on_seconds=31)
        self.assertEqual(changed, (0xFFFFFFFF, 0x8000001F, BASE[2]))
        fixed = cycle.patch_cycle_words(*original, start_min=0, end_min=1439)
        self.assertEqual(cycle.decode_cycle_words(*fixed)["start_min"], 0)
        self.assertEqual(fixed[0] & 0xFFC00000, 0xFFC00000)


if __name__ == "__main__":
    unittest.main()

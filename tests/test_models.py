"""Offline contract and type-safety tests."""

import unittest

from _load import integration

models = integration("models")
const = integration("const")


def config_data():
    return {
        "address": "02:00:00:00:00:01",
        "model": const.SUPPORTED_MODEL,
        "gatt_ltmk": "ab" * 32,
    }


def prop(siid, piid, value, boolean=False):
    return {
        "siid": siid,
        "piid": piid,
        "status": 0,
        "type_name": "bool" if boolean else "uint32",
        "typelen": 1 if boolean else 0x5004,
        "raw_hex": ("01" if value else "00") if boolean else value.to_bytes(4, "little").hex(),
        "value": value,
    }


def report():
    return {
        "address": "02:00:00:00:00:01",
        "model": const.SUPPORTED_MODEL,
        "status": "settings_scan_succeeded",
        "authentication_verified": True,
        "f0_verified": True,
        "relay_writes": 0,
        "connected_after_cleanup": False,
        "properties": [
            prop(2, 1, False, True),
            prop(4, 2, 7),
            prop(4, 3, 234),
            prop(4, 4, 29),
            prop(4, 1, 8000),
        ],
        "settings": {"indicator_mode": "inverse"},
        "capabilities": {"indicator_mode": {"kind": "select", "options": ["follow", "inverse"]}},
    }


class ConfigTests(unittest.TestCase):
    def test_supported_model_only(self):
        data = config_data()
        data["address"] = data["address"].lower()
        config = models.validate_config(data)
        self.assertEqual(config.address, "02:00:00:00:00:01")
        self.assertNotIn("ab" * 32, repr(config))
        for address, model in [
            ("02:00:00:00:00:02", "yutai.plug.fsov7m"),
            ("02:00:00:00:00:02", "wiing.plug.v7icm"),
            ("02:00:00:00:00:01", "yutai.plug.fsov7m"),
        ]:
            with self.subTest(address=address, model=model), self.assertRaises(models.ValidationError):
                models.validate_config({**data, "address": address, "model": model})

    def test_multiple_devices_keep_independent_identity_and_keys(self):
        first = models.validate_config(config_data())
        second = models.validate_config({**config_data(), "address": "02:00:00:00:00:02", "gatt_ltmk": "cd" * 32})
        self.assertNotEqual(first.address, second.address)
        self.assertNotEqual(first.worker_payload()["gatt_ltmk"], second.worker_payload()["gatt_ltmk"])
        self.assertNotIn(second.gatt_ltmk, repr(second))
        data = {**report(), "address": second.address}
        self.assertEqual(models.normalize_report(second, data).measurements["power"], 7)
        with self.assertRaises(models.ValidationError):
            models.normalize_report(first, data)
        with self.assertRaises(models.ValidationError):
            models.normalize_report(second, report())

    def test_invalid_addresses_rejected(self):
        for address in (None, "", "invalid", "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF", "02:00:00:00:00:0G"):
            with self.subTest(address=address), self.assertRaises(models.ValidationError):
                models.validate_config({**config_data(), "address": address})

    def test_key_errors_and_extra_data_are_sanitized(self):
        secret = "not-a-key-sensitive-value"
        for data in [
            {**config_data(), "gatt_ltmk": secret},
            {**config_data(), "gatt_ltmk": "ab" * 16},
            {**config_data(), "operation": "on"},
        ]:
            with self.assertRaises(models.ValidationError) as caught:
                models.validate_config(data)
            self.assertNotIn(secret, str(caught.exception))

    def test_interval_bounds(self):
        for value in (30, 60, 3600):
            self.assertEqual(models.validate_poll_interval(value), value)
        for value in (True, 29, 3601, 30.0, "60", None):
            with self.assertRaises(models.ValidationError):
                models.validate_poll_interval(value)


class SnapshotTests(unittest.TestCase):
    def test_meter_units_and_no_energy_exposed(self):
        values = models.normalize_report(models.validate_config(config_data()), report()).measurements
        self.assertEqual(values, {"relay_output": False, "voltage": 234, "current": 0.029, "power": 7})
        self.assertNotIn("energy", values)

    def test_missing_failed_or_malformed_meter_never_becomes_zero(self):
        for mutation in (
            lambda value: value.pop("value"),
            lambda value: value.update(status=-1),
            lambda value: value.update(value=True),
            lambda value: value.update(raw_hex="00000000"),
            lambda value: value.update(type_name="int32"),
            lambda value: value.update(typelen=1),
        ):
            data = report()
            mutation(data["properties"][3])
            self.assertIsNone(models.normalize_report(models.validate_config(config_data()), data).measurements["current"])
        data["properties"] = []
        self.assertTrue(all(value is None for value in models.normalize_report(models.validate_config(config_data()), data).measurements.values()))

    def test_duplicate_property_is_unavailable(self):
        data = report()
        data["properties"].append(prop(4, 2, 999))
        self.assertIsNone(models.normalize_report(models.validate_config(config_data()), data).measurements["power"])

    def test_boolean_relay_is_strict(self):
        data = report()
        data["properties"][0]["value"] = 0
        self.assertIsNone(models.normalize_report(models.validate_config(config_data()), data).measurements["relay_output"])
        data = report()
        data["properties"][0]["typelen"] = True
        self.assertIsNone(models.normalize_report(models.validate_config(config_data()), data).measurements["relay_output"])

    def test_safety_attestations_are_not_truthy_checks(self):
        for field, bad_value in [
            ("address", "02:00:00:00:00:02"),
            ("model", "yutai.plug.fsov7m"),
            ("authentication_verified", 1),
            ("f0_verified", "true"),
            ("relay_writes", False),
            ("relay_writes", 1),
            ("connected_after_cleanup", 0),
            ("status", "failed"),
        ]:
            with self.subTest(field=field), self.assertRaises(models.ValidationError):
                models.normalize_report(models.validate_config(config_data()), {**report(), field: bad_value})

    def test_only_explicit_verified_capabilities(self):
        data = report()
        self.assertEqual(set(models.normalize_report(models.validate_config(config_data()), data).capabilities), {"indicator_mode"})
        for capability in [
            {"relay_output": {"kind": "switch"}},
            {"indicator_mode": {"kind": "select", "options": ["unknown"]}},
            {"indicator_mode": {"kind": "select", "options": ["inverse", "inverse"]}},
            {"indicator_mode": {"kind": "switch"}},
            {"indicator_mode": {"kind": "select", "options": []}},
        ]:
            with self.assertRaises(models.ValidationError):
                models.normalize_report(models.validate_config(config_data()), {**data, "capabilities": capability})

    def test_invalid_setting_readback_field_is_unavailable(self):
        data = report()
        data["settings"]["indicator_mode"] = "always_on"
        self.assertNotIn("indicator_mode", models.normalize_report(models.validate_config(config_data()), data).settings)

    def test_setting_requires_exact_readback_and_attestation(self):
        data = report()
        data.update(status="setting_verified", verified=True)
        snapshot = models.normalize_report(models.validate_config(config_data()), data, operation="setting", field_name="indicator_mode", expected_value="inverse")
        self.assertEqual(snapshot.settings["indicator_mode"], "inverse")
        for bad in [
            {**data, "verified": False},
            {**data, "settings": {"indicator_mode": "follow"}},
            {**data, "settings": {}},
        ]:
            with self.assertRaises(models.ValidationError):
                models.normalize_report(models.validate_config(config_data()), bad, operation="setting", field_name="indicator_mode", expected_value="inverse")


class SettingTests(unittest.TestCase):
    def setUp(self):
        data = report()
        data["capabilities"] = {
            "indicator_mode": {"kind": "select", "options": ["follow", "inverse"]},
            "power_on_state": {"kind": "select", "options": ["off", "on", "memory"]},
            "indicator_dnd_enabled": {"kind": "switch"},
            "indicator_dnd_start": {"kind": "time"},
            "indicator_dnd_end": {"kind": "time"},
            "double_click_enabled": {"kind": "switch"},
            "double_click_seconds": {"kind": "number", "min": 1, "max": 86340, "step": 1, "unit": "s"},
        }
        self.capabilities = models.normalize_report(models.validate_config(config_data()), data).capabilities

    def test_strict_values_and_allowlist(self):
        for field, value in [
            ("indicator_mode", "inverse"),
            ("power_on_state", "memory"),
            ("indicator_dnd_enabled", False),
            ("indicator_dnd_start", "23:59"),
            ("indicator_dnd_end", "00:00"),
            ("double_click_enabled", True),
            ("double_click_seconds", 120),
        ]:
            self.assertEqual(models.validate_setting(field, value, self.capabilities), value)
        for field, value in [
            ("relay_output", True),
            ("operation", "on"),
            ("indicator_mode", "always_on"),
            ("indicator_dnd_enabled", 1),
            ("double_click_seconds", True),
            ("double_click_seconds", 120.0),
            ("double_click_seconds", 61),
            ("double_click_seconds", 137),
            ("double_click_seconds", 86460),
            ("indicator_dnd_start", "24:00"),
            ("indicator_dnd_start", "6:00"),
            ("indicator_dnd_start", "06:00:00"),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(models.ValidationError):
                models.validate_setting(field, value, self.capabilities)

    def test_invalid_number_capability_rejected(self):
        for bound in (True, "60", 0, -1, 86341):
            data = report()
            data["capabilities"] = {"double_click_seconds": {"kind": "number", "min": bound, "max": 86340, "step": 1, "unit": "s"}}
            with self.assertRaises(models.ValidationError):
                models.normalize_report(models.validate_config(config_data()), data)

    def test_countdown_seconds_and_minute_representation_boundaries(self):
        for value in (1, 17, 59, 60, 120, 3780, 3840, 7440, 86340):
            with self.subTest(value=value):
                self.assertEqual(models.validate_setting("double_click_seconds", value, self.capabilities), value)
        for value in (0, -1, 61, 137, 3599, 86339, 86341, 86400, True, 120.0, "120"):
            with self.subTest(value=value), self.assertRaises(models.ValidationError):
                models.validate_setting("double_click_seconds", value, self.capabilities)

    def test_unknown_twenty_four_hour_number_capability_is_rejected(self):
        data = report()
        data["capabilities"] = {"double_click_seconds": {"kind": "number", "min": 1, "max": 86400, "step": 1, "unit": "s"}}
        with self.assertRaises(models.ValidationError):
            models.normalize_report(models.validate_config(config_data()), data)

    def test_unrepresentable_readback_does_not_hide_other_fields(self):
        data = report()
        data["capabilities"]["double_click_seconds"] = {"kind": "number", "min": 1, "max": 86340, "step": 1, "unit": "s"}
        data["settings"]["double_click_seconds"] = 137
        snapshot = models.normalize_report(models.validate_config(config_data()), data)
        self.assertNotIn("double_click_seconds", snapshot.settings)
        self.assertEqual(snapshot.settings["indicator_mode"], "inverse")
        self.assertEqual(snapshot.measurements["power"], 7)


if __name__ == "__main__":
    unittest.main()

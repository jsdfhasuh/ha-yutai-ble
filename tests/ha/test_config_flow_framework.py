"""Check actual HA selector serialization and dotted model translation keys.

Run with the installed HA suite, separately from the lightweight schema stubs:
python -B -m unittest discover -s tests/ha -v
"""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import probatio

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import SelectSelector
from homeassistant.helpers.translation import recursive_flatten

from custom_components.yutai_ble.config_flow import YutaiConfigFlow
from custom_components.yutai_ble.const import DOMAIN, SUPPORTED_MODELS


class FrameworkConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        form = await YutaiConfigFlow().async_step_user()
        self.schema = form["data_schema"]
        self.model_selector = next(
            validator for field, validator in self.schema.schema.items()
            if field.schema == "model"
        )

    async def test_model_selector_serializes_original_values_and_validates_both_choices(self):
        self.assertIsInstance(self.model_selector, SelectSelector)
        # Use the same serializer as HA's config-flow HTTP response.
        fields = json.loads(json.dumps(probatio.to_field_list(
            self.schema, custom_serializer=cv.custom_serializer)))
        self.assertEqual({field["name"] for field in fields}, {"name", "address", "model", "gatt_ltmk"})
        model_field = next(field for field in fields if field["name"] == "model")
        self.assertEqual(model_field["default"], "yutai.plug.fsov8m")
        serialized = model_field["selector"]["select"]
        self.assertEqual(set(serialized["options"]), {"yutai.plug.fsov8m", "yutai.plug.fsov7m"})
        self.assertEqual(serialized["mode"], "list")
        self.assertEqual(serialized["translation_key"], "model")
        self.assertFalse(serialized["multiple"])
        self.assertFalse(serialized["custom_value"])

        for model in SUPPORTED_MODELS:
            with self.subTest(model=model):
                data = {
                    "name": "客厅插座",
                    "address": "02:00:00:00:00:01",
                    "model": model,
                    "gatt_ltmk": "ab" * 32,
                }
                self.assertEqual(self.schema(data), data)

    async def test_dotted_model_values_resolve_in_each_language(self):
        component = ROOT / "custom_components" / DOMAIN
        config = self.model_selector.serialize()["selector"]["select"]
        for path in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
            strings = json.loads((component / path).read_text(encoding="utf-8"))
            # HA flattens dictionaries without splitting dots already in a key.
            # The frontend appends the unmodified option value to this prefix.
            translations = recursive_flatten(f"component.{DOMAIN}.selector.", strings["selector"])
            for model in config["options"]:
                with self.subTest(path=path, model=model):
                    key = f"component.{DOMAIN}.selector.{config['translation_key']}.options.{model}"
                    self.assertIn(key, translations)
                    self.assertTrue(translations[key])
                    self.assertNotIn(translations[key], SUPPORTED_MODELS)
                    self.assertEqual(self.model_selector(model), model)


if __name__ == "__main__":
    unittest.main()

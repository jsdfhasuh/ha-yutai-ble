"""Release layout and privacy tests. Never access real credentials."""

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("release_builder_test", ROOT / "tools/build_release.py")
key_copy = load("key_copy_test", ROOT / "tools/auth/copy_key.py")


class PackageTests(unittest.TestCase):
    def test_manifest_hacs_and_component_allowlist(self):
        component = ROOT / builder.COMPONENT
        manifest = json.loads((component / "manifest.json").read_text())
        self.assertEqual(manifest["version"], "0.6.2")
        self.assertTrue(manifest["config_flow"])
        self.assertEqual(manifest["codeowners"], ["@jsdfhasuh"])
        self.assertEqual(manifest["dependencies"], ["bluetooth"])
        hacs = json.loads((ROOT / "hacs.json").read_text())
        self.assertEqual(hacs["homeassistant"], "2026.10.0")
        actual = {p.relative_to(component).as_posix() for p in component.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts}
        self.assertEqual(actual, set(builder.COMPONENT_FILES))

    def test_archives_are_reproducible_and_exclude_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            first = builder.build(Path(directory) / "one")
            second = builder.build(Path(directory) / "two")
            self.assertEqual([p.read_bytes() for p in first], [p.read_bytes() for p in second])
            for path in first[:2]:
                with zipfile.ZipFile(path) as package:
                    self.assertIsNone(package.testzip())
                    for name in package.namelist():
                        self.assertFalse(set(Path(name).parts) & {"private", ".storage", ".git", "tests", "__pycache__"})
                        self.assertNotIn("test_", Path(name).name)
                        self.assertFalse(name.endswith((".dpapi", ".pyc", ".log")))
                        text = package.read(name).decode("utf-8")
                        self.assertNotRegex(text, r"[A-Za-z]:[\\/]Users[\\/]")
                        self.assertNotRegex(text, r"192\.168\.\d+\.\d+")
                        addresses = re.findall(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", text)
                        self.assertTrue(all(a in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"} for a in addresses))
            with zipfile.ZipFile(first[0]) as package:
                self.assertIn(builder.COMPONENT + "/manifest.json", package.namelist())
                self.assertFalse(any(name.startswith("tools/") for name in package.namelist()))
            with zipfile.ZipFile(first[1]) as package:
                self.assertIn("start_qr.cmd", package.namelist())
                self.assertIn("vendor/LICENSE.tokens-extractor", package.namelist())
            for path in first[:2]:
                self.assertIn(hashlib.sha256(path.read_bytes()).hexdigest(), first[2].read_text())

    def test_bundled_vendor_hashes_still_match(self):
        # Extract fixed constants without importing BLE dependencies.
        import ast
        protocol = ROOT / builder.COMPONENT / "protocol"
        tree = ast.parse((protocol / "read_probe.py").read_text())
        hashes = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VENDOR_HASHES" for t in node.targets))
        for name, expected in hashes.items():
            self.assertEqual(hashlib.sha256((protocol / "vendor" / name).read_bytes()).hexdigest(), expected)

    def test_copy_helper_requires_gatt_key_for_supported_model(self):
        data = {"model": "yutai.plug.fsov8m", "address": "02:00:00:00:00:01", "gatt_ltmk": "ab" * 32}
        self.assertEqual(key_copy.decode_payload(json.dumps(data)), (data["address"], data["model"], data["gatt_ltmk"]))
        ordinary = {**data, "model": "yutai.plug.fsov7m"}
        self.assertEqual(key_copy.decode_payload(json.dumps(ordinary))[1], ordinary["model"])
        for changes in ({"model": "wiing.plug.v7icm"}, {"gatt_ltmk": "ab" * 16}, {"address": "invalid"}):
            with self.assertRaises(ValueError):
                key_copy.decode_payload(json.dumps({**data, **changes}))

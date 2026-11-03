"""Check published research sources, not private experiment directories."""

import ast
import hashlib
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublicResearchTests(unittest.TestCase):
    def test_only_public_source_docs_and_synthetic_addresses(self):
        for path in (ROOT / "research").rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            self.assertTrue(path.suffix in {".py", ".md"} or path.name == "LICENSE", path.name)
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"[A-Za-z]:[\\/]Users[\\/]")
            self.assertNotRegex(text, r"192\.168\.\d+\.\d+")
            for address in re.findall(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", text):
                self.assertTrue(address.startswith("02:00:00:00:00:") or address in {
                    "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}, path.name)
            if path.suffix == ".py":
                ast.parse(text)

    def test_model_scopes_and_pinned_vendor_bytes(self):
        for folder, model in (("yutai_fsov7m", "yutai.plug.fsov7m"), ("wiing_v7icm", "wiing.plug.v7icm")):
            root = ROOT / "research" / folder
            tree = ast.parse((root / "read_probe.py").read_text(encoding="utf-8"))
            assignments = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                           for target in node.targets if isinstance(target, ast.Name)}
            self.assertEqual(ast.literal_eval(assignments["MODEL"]), model)
            self.assertNotIn("ADDRESS", assignments)
            hashes = ast.literal_eval(assignments["VENDOR_HASHES"])
            for name, expected in hashes.items():
                self.assertEqual(hashlib.sha256((root / "vendor" / name).read_bytes()).hexdigest(), expected)

"""Load pure integration modules without importing HA's package entrypoint."""

import importlib
import sys
import types
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "yutai_ble"
if "custom_components" not in sys.modules:
    parent = types.ModuleType("custom_components")
    parent.__path__ = [str(COMPONENT.parent)]
    sys.modules["custom_components"] = parent
if "custom_components.yutai_ble" not in sys.modules:
    package = types.ModuleType("custom_components.yutai_ble")
    package.__path__ = [str(COMPONENT)]
    sys.modules["custom_components.yutai_ble"] = package


def integration(module):
    return importlib.import_module(f"custom_components.yutai_ble.{module}")

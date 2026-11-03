"""Config-flow decisions with small HA/schema stubs, not a live HA instance."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_models import config_data, report


class Duplicate(Exception):
    pass


class FlowStub:
    def __init_subclass__(cls, **kwargs):
        return super().__init_subclass__()

    def __init__(self):
        self.configured = set()

    async def async_set_unique_id(self, address):
        self.unique_id = address

    def _abort_if_unique_id_configured(self):
        if self.unique_id in self.configured:
            raise Duplicate()

    def async_create_entry(self, **data):
        return {"type": "create_entry", **data}

    def async_show_form(self, **data):
        return {"type": "form", **data}

    def async_abort(self, **data):
        return {"type": "abort", **data}


def load_flow():
    names = ("voluptuous", "homeassistant.config_entries", "homeassistant.const",
             "homeassistant.core", "homeassistant.helpers")
    stubs = {name: ModuleType(name) for name in names}
    vol = stubs["voluptuous"]
    vol.Schema = lambda value: value
    vol.Required = lambda key, **kwargs: key
    entries = stubs["homeassistant.config_entries"]
    entries.ConfigEntry = entries.ConfigFlowResult = object
    entries.ConfigFlow = entries.OptionsFlow = FlowStub
    for name in ("ADDRESS", "MODEL", "NAME"):
        setattr(stubs["homeassistant.const"], "CONF_" + name, name.lower())
    stubs["homeassistant.core"].callback = lambda method: method
    stubs["homeassistant.helpers"].selector = SimpleNamespace(
        TextSelector=lambda config: config, TextSelectorConfig=lambda **kwargs: kwargs,
        TextSelectorType=SimpleNamespace(PASSWORD="password"))
    path = Path(__file__).resolve().parents[1] / "custom_components/yutai_ble/config_flow.py"
    spec = importlib.util.spec_from_file_location("custom_components.yutai_ble.config_flow_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


flow_module = load_flow()


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_devices_create_separate_entries_with_read_only_validation(self):
        for address, key in (("02:00:00:00:00:01", "ab" * 32), ("02:00:00:00:00:02", "cd" * 32)):
            flow = flow_module.YutaiConfigFlow()
            data = {**config_data(), "address": address, "gatt_ltmk": key}
            with patch.object(flow_module.BridgeClient, "read", AsyncMock(return_value={**report(), "address": address})) as read:
                result = await flow.async_step_user(data)
            self.assertEqual(result["type"], "create_entry")
            self.assertEqual(flow.unique_id, address)
            self.assertEqual(result["data"]["gatt_ltmk"], key)
            self.assertEqual(read.call_args.args[0].address, address)

    async def test_duplicate_normalized_address_aborts_before_bluetooth(self):
        flow = flow_module.YutaiConfigFlow()
        flow.configured.add("02:AA:00:00:00:01")
        with patch.object(flow_module.BridgeClient, "read", AsyncMock()) as read:
            with self.assertRaises(Duplicate):
                await flow.async_step_user({**config_data(), "address": " 02:aa:00:00:00:01 "})
        read.assert_not_awaited()

    async def test_crossed_report_cannot_create_entry(self):
        flow = flow_module.YutaiConfigFlow()
        with patch.object(flow_module.BridgeClient, "read", AsyncMock(return_value=report())):
            result = await flow.async_step_user({**config_data(), "address": "02:00:00:00:00:02"})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})

    async def test_unknown_model_rejected_before_bluetooth(self):
        flow = flow_module.YutaiConfigFlow()
        with patch.object(flow_module.BridgeClient, "read", AsyncMock()) as read:
            result = await flow.async_step_import({**config_data(), "model": "yutai.plug.fsov7m"})
        self.assertEqual(result["reason"], "invalid_config")
        read.assert_not_awaited()

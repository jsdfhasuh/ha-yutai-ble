"""Manual/import flow; commissioning only performs a read."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS, CONF_MODEL, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .bridge import BridgeClient, BridgeError
from .const import (
    CONF_GATT_LTMK,
    CONF_POLL_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    SUPPORTED_MODEL,
)
from .models import ValidationError, normalize_report, validate_config, validate_poll_interval


class YutaiConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return YutaiOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                config = validate_config(user_input)
            except ValidationError:
                errors["base"] = "invalid_config"
            else:
                await self.async_set_unique_id(config.address)
                self._abort_if_unique_id_configured()
                try:
                    normalize_report(config, await BridgeClient().read(config))
                except (BridgeError, ValidationError):
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=config.name,
                        data={**config.worker_payload(), CONF_NAME: config.name},
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Required(CONF_ADDRESS): str,
                    vol.Required(CONF_MODEL, default=SUPPORTED_MODEL): str,
                    vol.Required(CONF_GATT_LTMK): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        try:
            config = validate_config(user_input)
        except ValidationError:
            return self.async_abort(reason="invalid_config")
        await self.async_set_unique_id(config.address)
        self._abort_if_unique_id_configured()
        try:
            normalize_report(config, await BridgeClient().read(config))
        except (BridgeError, ValidationError):
            return self.async_abort(reason="cannot_connect")
        return self.async_create_entry(
            title=config.name,
            data={**config.worker_payload(), CONF_NAME: config.name},
        )


class YutaiOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                interval = validate_poll_interval(user_input.get(CONF_POLL_INTERVAL))
            except ValidationError:
                errors["base"] = "invalid_interval"
            else:
                return self.async_create_entry(title="", data={CONF_POLL_INTERVAL: interval})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL))
                }
            ),
            errors=errors,
        )

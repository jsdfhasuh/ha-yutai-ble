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
    CONF_SESSION_IDLE_TIMEOUT,
    MAX_SESSION_IDLE_TIMEOUT,
    DEFAULT_NAME,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    SUPPORTED_MODEL,
    SUPPORTED_MODELS,
)
from .models import ValidationError, normalize_report, validate_config, validate_poll_interval, validate_session_idle_timeout


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
                    vol.Required(CONF_MODEL, default=SUPPORTED_MODEL): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(SUPPORTED_MODELS),
                            translation_key="model",
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
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
        metering = self.config_entry.data.get(CONF_MODEL) == SUPPORTED_MODEL
        if user_input is not None:
            try:
                interval = validate_poll_interval(user_input.get(CONF_POLL_INTERVAL))
            except ValidationError:
                errors["base"] = "invalid_interval"
            else:
                try:
                    idle = validate_session_idle_timeout(user_input.get(CONF_SESSION_IDLE_TIMEOUT, 0)) if metering else 0
                except ValidationError:
                    errors["base"] = "invalid_session_timeout"
                else:
                    options = {**self.config_entry.options, CONF_POLL_INTERVAL: interval}
                    if metering:
                        options[CONF_SESSION_IDLE_TIMEOUT] = idle
                    return self.async_create_entry(title="", data=options)
        schema = {
            vol.Required(CONF_POLL_INTERVAL, default=self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)):
                vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL))
        }
        if metering:
            schema[vol.Required(CONF_SESSION_IDLE_TIMEOUT, default=self.config_entry.options.get(CONF_SESSION_IDLE_TIMEOUT, 0))] = vol.All(
                vol.Coerce(int), vol.Range(min=0, max=MAX_SESSION_IDLE_TIMEOUT))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

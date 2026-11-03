"""Minute-resolution do-not-disturb boundaries."""

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from .entity import YutaiEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities(
        YutaiTime(coordinator, field)
        for field, capability in coordinator.data.capabilities.items()
        if capability.kind == "time"
    )


class YutaiTime(YutaiEntity, TimeEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> time | None:
        value = self.verified_value
        return time.fromisoformat(value) if value is not None else None

    async def async_set_value(self, value: time) -> None:
        if value.second or value.microsecond or value.tzinfo is not None:
            raise HomeAssistantError("Yutai BLE time requires a local whole minute")
        await self.coordinator.async_set_setting(self.field, value.strftime("%H:%M"))

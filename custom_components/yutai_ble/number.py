"""Verified countdown duration, in seconds."""

import math

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .entity import YutaiEntity
from .models import ValidationError, validate_setting


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities(
        YutaiNumber(coordinator, field)
        for field, capability in coordinator.data.capabilities.items()
        if capability.kind == "number"
    )


class YutaiNumber(YutaiEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, field):
        super().__init__(coordinator, field)
        capability = coordinator.data.capabilities[field]
        self._attr_native_min_value = capability.minimum
        self._attr_native_max_value = capability.maximum
        self._attr_native_step = capability.step

    @property
    def native_value(self) -> int | None:
        return self.verified_value

    async def async_set_native_value(self, value: float) -> None:
        try:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
                or int(value) != value
            ):
                raise ValidationError("invalid_duration")
            seconds = int(value)
            validate_setting(self.field, seconds, self.coordinator.data.capabilities)
        except ValidationError:
            raise HomeAssistantError(
                "Enter 1-59 seconds, or whole minutes from 60 to 86340 seconds (23 h 59 min).",
                translation_domain=DOMAIN,
                translation_key="invalid_countdown_duration",
            ) from None
        await self.coordinator.async_set_setting(self.field, seconds)

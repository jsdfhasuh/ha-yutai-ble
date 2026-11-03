"""Independent verified output control and configuration switches."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import YutaiEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([YutaiRelaySwitch(coordinator)])
    async_add_entities(
        YutaiSwitch(coordinator, field)
        for field, capability in coordinator.data.capabilities.items()
        if capability.kind == "switch"
    )


class YutaiSwitch(YutaiEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool | None:
        return self.verified_value

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_setting(self.field, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_setting(self.field, False)


class YutaiRelaySwitch(YutaiEntity, SwitchEntity):
    _attr_entity_category = None

    def __init__(self, coordinator):
        super().__init__(coordinator, "relay_output", measurement=True)
        self._attr_unique_id = f"{coordinator.device_config.address}_relay_control"
        self._attr_translation_key = "relay_control"
        self._attr_icon = "mdi:power"

    @property
    def is_on(self) -> bool | None:
        return self.verified_value

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_relay(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_relay(False)

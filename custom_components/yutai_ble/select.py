"""Only backend-confirmed select settings."""

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import YutaiEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities(
        YutaiSelect(coordinator, field)
        for field, capability in coordinator.data.capabilities.items()
        if capability.kind == "select"
    )


class YutaiSelect(YutaiEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def options(self) -> list[str]:
        capability = self.coordinator.data.capabilities.get(self.field)
        return list(capability.options) if capability else []

    @property
    def current_option(self) -> str | None:
        return self.verified_value

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_setting(self.field, option)

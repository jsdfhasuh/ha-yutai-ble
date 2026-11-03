"""Read-only relay output indication."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import YutaiEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([YutaiRelayOutput(coordinator, "relay_output", measurement=True)])


class YutaiRelayOutput(YutaiEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.POWER

    @property
    def is_on(self) -> bool | None:
        return self.verified_value

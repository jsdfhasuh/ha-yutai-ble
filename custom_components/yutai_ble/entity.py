"""Common non-polling entities bound to the verified snapshot."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import YutaiCoordinator


class YutaiEntity(CoordinatorEntity[YutaiCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: YutaiCoordinator, field: str, *, measurement: bool = False) -> None:
        super().__init__(coordinator)
        self.field = field
        self.measurement = measurement
        config = coordinator.device_config
        self._attr_unique_id = f"{config.address}_{field}"
        self._attr_translation_key = field
        icons = {
            "indicator_mode": "mdi:led-on",
            "power_on_state": "mdi:power-settings",
            "indicator_dnd_enabled": "mdi:weather-night",
            "indicator_dnd_start": "mdi:weather-sunset-down",
            "indicator_dnd_end": "mdi:weather-sunset-up",
            "double_click_enabled": "mdi:gesture-double-tap",
            "double_click_seconds": "mdi:timer-outline",
            "relay_output": "mdi:power-socket-eu",
        }
        if field in icons:
            self._attr_icon = icons[field]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config.address)},
            connections={(CONNECTION_BLUETOOTH, config.address)},
            name=config.name,
            manufacturer="Yutai",
            model=config.model,
        )

    @property
    def available(self) -> bool:
        if self.field == "relay_output" and self.coordinator.relay_readback is not None:
            return True
        if not super().available or self.coordinator.data is None:
            return False
        data = self.coordinator.data
        if self.measurement:
            return data.measurements.get(self.field) is not None
        return self.field in data.capabilities and self.field in data.settings

    @property
    def verified_value(self):
        if self.field == "relay_output" and self.coordinator.relay_readback is not None:
            return self.coordinator.relay_readback
        data = self.coordinator.data
        if data is None:
            return None
        values = data.measurements if self.measurement else data.settings
        return values.get(self.field)

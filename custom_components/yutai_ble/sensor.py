"""Instantaneous metering only; cumulative energy is not yet verified."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfPower
from homeassistant.helpers.entity import EntityCategory

from .entity import YutaiEntity

_METERS = {
    "voltage": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, 0),
    "current": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE, 3),
    "power": (SensorDeviceClass.POWER, UnitOfPower.WATT, 0),
}


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities(YutaiSensor(coordinator, field) for field in _METERS)
    async_add_entities(YutaiDiagnostic(coordinator, field) for field in
                       ("connection_status", "last_success", "consecutive_failures"))


class YutaiSensor(YutaiEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, field):
        super().__init__(coordinator, field, measurement=True)
        device_class, unit, precision = _METERS[field]
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_display_precision = precision

    @property
    def native_value(self) -> float | None:
        return self.verified_value


class YutaiDiagnostic(YutaiEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, field):
        super().__init__(coordinator, field)
        if field == "last_success":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_icon = "mdi:clock-check-outline"
        elif field == "connection_status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["verified", "retrying", "initializing"]
            self._attr_icon = "mdi:bluetooth-connect"
        else:
            self._attr_icon = "mdi:counter"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        coordinator = self.coordinator
        if self.field == "last_success":
            return coordinator.last_success_at
        if self.field == "consecutive_failures":
            return coordinator.consecutive_failures
        if coordinator.last_update_success and coordinator.last_success_at is not None:
            return "verified"
        return "retrying" if coordinator.last_error_code is not None else "initializing"

    @property
    def extra_state_attributes(self):
        if self.field != "connection_status":
            return None
        interval = self.coordinator.update_interval
        return {"last_error": self.coordinator.last_error_code,
                "poll_interval_seconds": int(interval.total_seconds()) if interval else None}

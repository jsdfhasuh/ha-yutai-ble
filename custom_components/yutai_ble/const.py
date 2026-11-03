"""Constants for the narrowly scoped Yutai BLE integration."""

DOMAIN = "yutai_ble"
CONF_GATT_LTMK = "gatt_ltmk"
CONF_POLL_INTERVAL = "poll_interval"
from .protocol.device_identity import SUPPORTED_MODEL
DEFAULT_NAME = "\u79b9\u6cf0\u8ba1\u91cf\u63d2\u5ea7"
DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 30
MAX_POLL_INTERVAL = 3600

SELECT_OPTIONS = {
    "indicator_mode": ("follow", "inverse", "off", "always_on"),
    "power_on_state": ("off", "on", "memory"),
}
FIELD_KINDS = {
    "indicator_mode": "select",
    "power_on_state": "select",
    "indicator_dnd_enabled": "switch",
    "indicator_dnd_start": "time",
    "indicator_dnd_end": "time",
    "double_click_enabled": "switch",
    "double_click_seconds": "number",
}

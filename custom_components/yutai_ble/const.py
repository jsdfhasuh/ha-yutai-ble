"""Constants for the narrowly scoped Yutai BLE integration."""

DOMAIN = "yutai_ble"
CONF_GATT_LTMK = "gatt_ltmk"
CONF_POLL_INTERVAL = "poll_interval"
CONF_SESSION_IDLE_TIMEOUT = "session_idle_timeout"
DEFAULT_SESSION_IDLE_TIMEOUT = 0
MAX_SESSION_IDLE_TIMEOUT = 120
from .protocol.device_identity import ORDINARY_MODEL, SUPPORTED_MODEL, SUPPORTED_MODELS
DEFAULT_NAME = "\u79b9\u6cf0\u84dd\u7259\u63d2\u5ea7"
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

MODEL_FIELDS = {
    SUPPORTED_MODEL: frozenset(FIELD_KINDS),
    ORDINARY_MODEL: frozenset(("indicator_mode", "power_on_state")),
}
MODEL_SELECT_OPTIONS = {
    SUPPORTED_MODEL: SELECT_OPTIONS,
    ORDINARY_MODEL: {
        "indicator_mode": ("follow", "off"),
        "power_on_state": ("off", "on", "memory"),
    },
}

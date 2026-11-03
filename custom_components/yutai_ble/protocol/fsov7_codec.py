"""Only the fsov7m settings independently written and read back on hardware."""

import read_probe as read

READ_PROPERTIES = (
    (2, 1), (4, 2), (4, 3), (4, 4),
    (5, 1), (5, 2), (5, 3), (5, 4), (5, 5), (5, 6), (5, 7),
    (7, 1), (7, 2),
)
PROPERTY = (5, 4)
FIELDS = {
    "indicator_mode": (0xC000, {"follow": 0, "off": 0x8000}),
    "power_on_state": (0x3000, {"off": 0, "on": 0x1000, "memory": 0x2000}),
}


def validate_setting(field, value):
    if type(field) is not str or field not in FIELDS:
        read.fail("setting_not_confirmed")
    mask, options = FIELDS[field]
    if type(value) is not str or value not in options:
        read.fail("setting_value_not_confirmed")
    return PROPERTY, mask


def target_word(field, value, initial):
    _property, mask = validate_setting(field, value)
    if type(initial) is not int or not 0 <= initial <= 0xFFFFFFFF:
        read.fail("setting_uint32_required")
    options = FIELDS[field][1]
    if initial & mask not in options.values():
        read.fail("setting_current_value_not_confirmed")
    return (initial & ~mask) | options[value]


def decode_settings(properties):
    if not isinstance(properties, list):
        read.fail("settings_snapshot_invalid")
    words = {}
    for item in properties:
        if not isinstance(item, dict):
            read.fail("settings_snapshot_invalid")
        pair = item.get("siid"), item.get("piid")
        if any(type(part) is not int for part in pair) or pair in words:
            read.fail("settings_snapshot_identity_invalid")
        words[pair] = item
    item = words.get(PROPERTY, {})
    if (type(item.get("status")) is not int or item["status"] != 0
            or type(item.get("typelen")) is not int or item["typelen"] != 0x5004
            or item.get("type_name") != "uint32"):
        return {}, {}
    value = item.get("value")
    if (type(value) is not int or not 0 <= value <= 0xFFFFFFFF
            or item.get("raw_hex") != value.to_bytes(4, "little").hex()):
        read.fail("settings_snapshot_value_mismatch")
    settings, capabilities = {}, {}
    for field, (mask, options) in FIELDS.items():
        option = next((name for name, bits in options.items() if value & mask == bits), None)
        if option is not None:
            settings[field] = option
            capabilities[field] = {"kind": "select", "options": list(options)}
    return settings, capabilities

"""Packed fields confirmed on the commissioned fsov8m, never guessed fields."""

from __future__ import annotations

import re

import read_probe as read


READ_PROPERTIES = (
    (2, 1), (4, 2), (4, 3), (4, 4),
    (5, 1), (5, 2), (5, 3), (5, 4), (5, 5), (5, 6), (5, 7),
    (7, 1), (7, 2),
)

# Each entry is property, complete field mask, and observed enum encodings.
# These mappings were measured on fsov8m; do not reuse them across models.
ENUM_FIELDS = {
    "indicator_mode": ((5, 4), 0xC000, {
        "follow": 0, "inverse": 0x4000, "off": 0x8000, "always_on": 0xC000,
    }),
    "power_on_state": ((5, 4), 0x3000, {
        "off": 0, "on": 0x1000, "memory": 0x2000,
    }),
}
BOOL_FIELDS = {
    "indicator_dnd_enabled": ((5, 6), 0x80000000),
    "double_click_enabled": ((5, 4), 0x10),
}
TIME_FIELDS = {
    "indicator_dnd_start": ((5, 6), 20),
    "indicator_dnd_end": ((5, 6), 9),
}
DURATION_PROPERTY = (5, 5)
DURATION_SHIFT = 21
DURATION_MASK = 0xFFE00000
MAX_SECONDS = 23 * 3600 + 59 * 60


def duration_code(value):
    if type(value) is not int or not 1 <= value <= MAX_SECONDS:
        read.fail("setting_duration_outside_confirmed_range")
    if value <= 59:
        return value
    if value % 60:
        read.fail("setting_duration_whole_minutes_required")
    return value // 60 + 59


def duration_seconds(code):
    if not 1 <= code <= 1498:
        read.fail("setting_duration_code_not_confirmed")
    return code if code <= 59 else (code - 59) * 60


def time_minutes(value):
    if type(value) is not str or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None:
        read.fail("setting_time_invalid")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def uint32(value):
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        read.fail("setting_uint32_required")
    return value


def validate_setting(field, value):
    if type(field) is not str:
        read.fail("setting_not_confirmed")
    if field in ENUM_FIELDS:
        prop, mask, options = ENUM_FIELDS[field]
        if type(value) is not str or value not in options:
            read.fail("setting_value_not_confirmed")
        return prop, mask
    if field in BOOL_FIELDS:
        if type(value) is not bool:
            read.fail("setting_bool_required")
        return BOOL_FIELDS[field]
    if field in TIME_FIELDS:
        time_minutes(value)
        prop, shift = TIME_FIELDS[field]
        return prop, 0x7FF << shift
    if field == "double_click_seconds":
        duration_code(value)
        return DURATION_PROPERTY, DURATION_MASK
    read.fail("setting_not_confirmed")


def target_word(field, value, initial):
    _prop, mask = validate_setting(field, value)
    uint32(initial)
    if field in ENUM_FIELDS:
        options = ENUM_FIELDS[field][2]
        if initial & mask not in options.values():
            read.fail("setting_current_value_not_confirmed")
        return (initial & ~mask) | options[value]
    if field in TIME_FIELDS:
        shift = TIME_FIELDS[field][1]
        if (initial & mask) >> shift > 1439:
            read.fail("setting_current_time_invalid")
        return (initial & ~mask) | (time_minutes(value) << shift)
    if field == "double_click_seconds":
        duration_seconds((initial & mask) >> DURATION_SHIFT)
        return (initial & ~mask) | (duration_code(value) << DURATION_SHIFT)
    return (initial | mask) if value else (initial & ~mask)


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
    settings, capabilities = {}, {}
    for field in (*ENUM_FIELDS, *BOOL_FIELDS, *TIME_FIELDS, "double_click_seconds"):
        if field in ENUM_FIELDS:
            prop, mask, options = ENUM_FIELDS[field]
        elif field in BOOL_FIELDS:
            prop, mask = BOOL_FIELDS[field]
        elif field in TIME_FIELDS:
            prop, shift = TIME_FIELDS[field]
            mask = 0x7FF << shift
        else:
            prop, shift, mask = DURATION_PROPERTY, DURATION_SHIFT, DURATION_MASK
        item = words.get(prop, {})
        if (type(item.get("status")) is not int or item["status"] != 0
                or item.get("typelen") != 0x5004
                or item.get("type_name") != "uint32"):
            continue
        value = uint32(item.get("value"))
        if item.get("raw_hex") != value.to_bytes(4, "little").hex():
            read.fail("settings_snapshot_value_mismatch")
        if field in ENUM_FIELDS:
            selected = next((name for name, bits in options.items() if value & mask == bits), None)
            if selected is None:
                continue
            settings[field] = selected
            capabilities[field] = {"kind": "select", "options": list(options)}
        elif field in BOOL_FIELDS:
            settings[field] = bool(value & mask)
            capabilities[field] = {"kind": "switch"}
        elif field in TIME_FIELDS:
            minutes = (value & mask) >> shift
            if minutes > 1439:
                continue
            settings[field] = "%02d:%02d" % divmod(minutes, 60)
            capabilities[field] = {"kind": "time"}
        else:
            code = (value & mask) >> DURATION_SHIFT
            if not 1 <= code <= 1498:
                continue
            settings[field] = duration_seconds(code)
            capabilities[field] = {"kind": "number", "min": 1, "max": MAX_SECONDS,
                                   "step": 1, "unit": "s"}
    return settings, capabilities

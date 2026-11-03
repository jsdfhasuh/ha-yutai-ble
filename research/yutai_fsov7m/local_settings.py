"""Pure offline codec for independently observed Yutai local word (5.7).

The two minute fields, their independent enable flags, repeat, and raw weekday
bits 22..28 are confirmed here. The measured day mapping is in README.md;
this codec intentionally exposes a raw mask rather than named weekdays.
The weekday range is an encoding range, not a verified device execution limit.
This module has no master-mode, clock-setting, or hardware I/O functionality.
"""


UINT32_MAX = 0xFFFFFFFF
MINUTES_MAX = 1439
START_MASK = 0x7FF << 11
END_MASK = 0x7FF
WEEKDAY_MASK = 0x7F << 22
START_ENABLED_BIT = 0x80000000
END_ENABLED_BIT = 0x40000000
REPEAT_BIT = 0x20000000
FIELDS = frozenset(("start_min", "end_min", "weekday_mask", "start_enabled", "end_enabled", "repeat"))


def _integer(value, maximum, name):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(name + "_out_of_range")
    return value


def decode_local_word(word):
    """Decode only confirmed fields, rejecting invalid encoded day times."""
    _integer(word, UINT32_MAX, "local_word")
    return {
        "start_min": _integer((word & START_MASK) >> 11, MINUTES_MAX, "start_min"),
        "end_min": _integer(word & END_MASK, MINUTES_MAX, "end_min"),
        "weekday_mask": (word & WEEKDAY_MASK) >> 22,
        "start_enabled": bool(word & START_ENABLED_BIT),
        "end_enabled": bool(word & END_ENABLED_BIT),
        "repeat": bool(word & REPEAT_BIT),
    }


def patch_local_word(word, **changes):
    """Patch explicit confirmed fields; leave all other original bits intact.

    Boolean fields accept only bool. Minute fields accept integers 0..1439 and
    weekday_mask accepts integers 0..127, never bool. The weekday mask is raw,
    without a day-name mapping. Unchanged minute fields retain their original
    encoding verbatim, even when that encoding is unrecognized.
    """
    _integer(word, UINT32_MAX, "local_word")
    if changes.keys() - FIELDS:
        raise ValueError("unknown_local_field")
    for name, value in changes.items():
        if name in ("start_min", "end_min"):
            _integer(value, MINUTES_MAX, name)
            mask, shift = (START_MASK, 11) if name == "start_min" else (END_MASK, 0)
            word = (word & ~mask) | value << shift
        elif name == "weekday_mask":
            _integer(value, 0x7F, name)
            word = (word & ~WEEKDAY_MASK) | value << 22
        else:
            if type(value) is not bool:
                raise ValueError(name + "_bool_required")
            mask = {"start_enabled": START_ENABLED_BIT, "end_enabled": END_ENABLED_BIT,
                    "repeat": REPEAT_BIT}[name]
            word = (word & ~mask) | (mask if value else 0)
    return word

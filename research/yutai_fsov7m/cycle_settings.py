"""Offline codecs for observed Yutai cycle settings (5.1), (5.2), and (5.3).

Only fields confirmed through phone changes and independent BLE reads are
exposed. Schedule bits 30..31 are outside this codec and are always preserved.
The weekday API stays raw; the measured day mapping is documented in README.md.

The accepted duration range is the 31-bit encoding range, not a verified device
operating range. A zero weekday mask is representable, not a claim that a
schedule without active weekdays will execute. This module performs no I/O.
"""


UINT32_MAX = 0xFFFFFFFF
SECONDS_MAX = 0x7FFFFFFF
MINUTES_MAX = 1439
WEEKDAY_MAX = 0x7F
TIME_MASK = 0x7FF
STATE_BIT = 0x80000000
REPEAT_BIT = 0x20000000

FIELDS = frozenset((
    "start_min", "end_min", "weekday_mask", "on_seconds", "off_seconds",
    "start_state", "end_state", "repeat",
))


def _integer(value, maximum, name):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(name + "_out_of_range")
    return value


def _words(schedule_word, on_word, off_word):
    return (
        _integer(schedule_word, UINT32_MAX, "schedule_word"),
        _integer(on_word, UINT32_MAX, "on_word"),
        _integer(off_word, UINT32_MAX, "off_word"),
    )


def decode_cycle_words(schedule_word, on_word, off_word):
    """Decode the eight observed fields, rejecting invalid encoded day times."""
    schedule_word, on_word, off_word = _words(schedule_word, on_word, off_word)
    start_min = _integer((schedule_word >> 11) & TIME_MASK, MINUTES_MAX, "start_min")
    end_min = _integer(schedule_word & TIME_MASK, MINUTES_MAX, "end_min")
    return {
        "start_min": start_min,
        "end_min": end_min,
        "weekday_mask": (schedule_word >> 22) & WEEKDAY_MAX,
        "on_seconds": on_word & SECONDS_MAX,
        "off_seconds": off_word & SECONDS_MAX,
        "start_state": bool(on_word & STATE_BIT),
        "end_state": bool(off_word & STATE_BIT),
        "repeat": bool(schedule_word & REPEAT_BIT),
    }


def patch_cycle_words(schedule_word, on_word, off_word, **changes):
    """Patch explicit observed fields and preserve every other original bit.

    State fields accept only bool. Integer fields reject bool and accept only
    their documented encoding ranges; device execution limits are not inferred.
    Unchanged time fields are preserved verbatim, even if an original word has
    an unrecognized encoded time. No enable or clock-setting field is accepted.
    """
    schedule_word, on_word, off_word = _words(schedule_word, on_word, off_word)
    if changes.keys() - FIELDS:
        raise ValueError("unknown_cycle_field")
    for name, value in changes.items():
        if name in ("start_state", "end_state", "repeat"):
            if type(value) is not bool:
                raise ValueError(name + "_bool_required")
            if name == "repeat":
                schedule_word = (schedule_word & ~REPEAT_BIT) | (REPEAT_BIT if value else 0)
            elif name == "start_state":
                on_word = (on_word & ~STATE_BIT) | (STATE_BIT if value else 0)
            else:
                off_word = (off_word & ~STATE_BIT) | (STATE_BIT if value else 0)
        elif name in ("on_seconds", "off_seconds"):
            _integer(value, SECONDS_MAX, name)
            if name == "on_seconds":
                on_word = (on_word & STATE_BIT) | value
            else:
                off_word = (off_word & STATE_BIT) | value
        elif name == "weekday_mask":
            _integer(value, WEEKDAY_MAX, name)
            schedule_word = (schedule_word & ~(WEEKDAY_MAX << 22)) | (value << 22)
        else:
            _integer(value, MINUTES_MAX, name)
            shift = 11 if name == "start_min" else 0
            schedule_word = (schedule_word & ~(TIME_MASK << shift)) | (value << shift)
    return schedule_word, on_word, off_word

"""Pure validation and normalization, with no HA or Bluetooth imports."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .const import (
    CONF_GATT_LTMK,
    DEFAULT_NAME,
    FIELD_KINDS,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MODEL_FIELDS,
    MODEL_SELECT_OPTIONS,
    SUPPORTED_MODEL,
)
from .protocol.device_identity import validate_identity

_HEX_KEY = re.compile(r"[0-9a-fA-F]{64}\Z")
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


class ValidationError(ValueError):
    """A fixed, privacy-safe validation error."""


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    address: str
    model: str
    gatt_ltmk: str = field(repr=False)
    name: str = DEFAULT_NAME

    def worker_payload(self) -> dict[str, str]:
        return {
            "address": self.address,
            "model": self.model,
            CONF_GATT_LTMK: self.gatt_ltmk,
        }


@dataclass(frozen=True, slots=True)
class Capability:
    kind: str
    options: tuple[str, ...] = ()
    minimum: int = 1
    maximum: int = 86340
    step: int = 1


@dataclass(frozen=True, slots=True)
class Snapshot:
    settings: dict[str, Any]
    capabilities: dict[str, Capability]
    measurements: dict[str, float | bool | None]


def validate_config(data: Mapping[str, Any]) -> DeviceConfig:
    """Accept a supported model, normalized address and a 32-byte key."""
    if not isinstance(data, Mapping) or set(data) - {
        "address", "model", CONF_GATT_LTMK, "name"
    }:
        raise ValidationError("invalid_config")
    address = data.get("address")
    model = data.get("model", SUPPORTED_MODEL)
    key = data.get(CONF_GATT_LTMK)
    name = data.get("name", DEFAULT_NAME)
    try:
        address, model = validate_identity(address, model)
    except ValueError:
        raise ValidationError("unsupported_device") from None
    if not isinstance(key, str) or _HEX_KEY.fullmatch(key) is None:
        raise ValidationError("invalid_key")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValidationError("invalid_config")
    return DeviceConfig(address, model, key.lower(), name.strip())


def validate_poll_interval(value: Any) -> int:
    if type(value) is not int or not MIN_POLL_INTERVAL <= value <= MAX_POLL_INTERVAL:
        raise ValidationError("invalid_interval")
    return value


def _capabilities(data: Any, model: str) -> dict[str, Capability]:
    if not isinstance(data, dict):
        raise ValidationError("invalid_capabilities")
    result = {}
    for name, description in data.items():
        if name not in MODEL_FIELDS[model] or not isinstance(description, dict):
            raise ValidationError("invalid_capabilities")
        kind = FIELD_KINDS[name]
        if description.get("kind") != kind:
            raise ValidationError("invalid_capabilities")
        if kind == "select":
            options = description.get("options")
            if (
                not isinstance(options, list)
                or not options
                or any(type(option) is not str or option not in MODEL_SELECT_OPTIONS[model][name] for option in options)
                or len(set(options)) != len(options)
            ):
                raise ValidationError("invalid_capabilities")
            result[name] = Capability(kind, tuple(options))
        elif kind == "number":
            minimum, maximum, step = (
                description.get("min"), description.get("max"), description.get("step")
            )
            if (
                any(type(item) is not int for item in (minimum, maximum, step))
                or not 1 <= minimum <= maximum <= 86340
                or not 1 <= step <= 86340
                or description.get("unit") != "s"
            ):
                raise ValidationError("invalid_capabilities")
            result[name] = Capability(kind, minimum=minimum, maximum=maximum, step=step)
        else:
            result[name] = Capability(kind)
    return result


def validate_setting(name: str, value: Any, capabilities: Mapping[str, Capability]) -> Any:
    """A fixed field whitelist prevents accidentally dispatching relay writes."""
    if name not in FIELD_KINDS or name not in capabilities:
        raise ValidationError("unsupported_setting")
    capability = capabilities[name]
    if capability.kind != FIELD_KINDS[name]:
        raise ValidationError("invalid_setting")
    valid = False
    if capability.kind == "select":
        valid = type(value) is str and value in capability.options
    elif capability.kind == "switch":
        valid = type(value) is bool
    elif capability.kind == "time":
        valid = type(value) is str and _TIME.fullmatch(value) is not None
    elif capability.kind == "number":
        valid = (
            type(value) is int
            and capability.minimum <= value <= capability.maximum
            and (value - capability.minimum) % capability.step == 0
            and 1 <= value <= 86340
            and (value <= 59 or value % 60 == 0)
        )
    if not valid:
        raise ValidationError("invalid_setting")
    return value


def _property_value(item: Any, boolean: bool = False) -> int | bool | None:
    if not isinstance(item, dict) or type(item.get("status")) is not int or item["status"] != 0:
        return None
    value = item.get("value")
    raw = item.get("raw_hex")
    if boolean:
        if (
            type(value) is bool
            and item.get("type_name") == "bool"
            and type(item.get("typelen")) is int
            and item.get("typelen") == 1
            and raw == ("01" if value else "00")
        ):
            return value
        return None
    if (
        type(value) is not int
        or not 0 <= value <= 0xFFFFFFFF
        or item.get("type_name") != "uint32"
        or type(item.get("typelen")) is not int
        or item.get("typelen") != 0x5004
        or type(raw) is not str
        or re.fullmatch(r"[0-9a-fA-F]{8}", raw) is None
        or int.from_bytes(bytes.fromhex(raw), "little") != value
    ):
        return None
    return value


def normalize_report(
    config: DeviceConfig,
    report: Any,
    *,
    operation: str = "read",
    field_name: str | None = None,
    expected_value: Any = None,
) -> Snapshot:
    """Validate all safety attestations; discard the raw report afterwards."""
    if not isinstance(report, dict) or operation not in {"read", "setting", "relay"}:
        raise ValidationError("invalid_report")
    if (
        report.get("address") != config.address
        or report.get("model") != config.model
        or report.get("authentication_verified") is not True
        or report.get("f0_verified") is not True
        or report.get("connected_after_cleanup") is not False
    ):
        raise ValidationError("unverified_report")
    writes = report.get("relay_writes")
    if type(writes) is not int or (operation != "relay" and writes != 0):
        raise ValidationError("unverified_report")
    if operation == "read":
        if report.get("status") not in {"settings_scan_succeeded", "read_verified"}:
            raise ValidationError("unverified_report")
    elif operation == "setting":
        if report.get("status") != "setting_verified" or report.get("verified") is not True:
            raise ValidationError("unverified_setting")
    else:
        initial = report.get("initial_relay_state")
        observed = report.get("observed_relay_state")
        attempts = report.get("relay_write_attempts")
        if (
            field_name is not None
            or type(expected_value) is not bool
            or report.get("operation") != "relay_control"
            or report.get("status") != "relay_verified"
            or report.get("verified") is not True
            or type(report.get("requested_value")) is not bool
            or report["requested_value"] is not expected_value
            or type(initial) is not bool
            or type(observed) is not bool
            or observed is not expected_value
            or writes not in (0, 1)
            or type(attempts) is not int
            or attempts != writes
            or writes != int(initial is not expected_value)
            or report.get("maybe_executed") is not bool(writes)
            or report.get("initial_snapshot_complete") is not True
            or report.get("verification_snapshot_complete") is not True
            or report.get("settings_unchanged") is not True
        ):
            raise ValidationError("unverified_relay")
    capabilities = _capabilities(report.get("capabilities"), config.model)
    incoming_settings = report.get("settings")
    if not isinstance(incoming_settings, dict):
        raise ValidationError("invalid_report")
    settings = {}
    for name in capabilities:
        if name in incoming_settings:
            try:
                settings[name] = validate_setting(name, incoming_settings[name], capabilities)
            except ValidationError:
                continue
    if operation == "setting":
        validate_setting(field_name, expected_value, capabilities)
        if (
            field_name not in settings
            or type(settings[field_name]) is not type(expected_value)
            or settings[field_name] != expected_value
        ):
            raise ValidationError("setting_mismatch")
    properties = report.get("properties")
    if not isinstance(properties, list):
        raise ValidationError("invalid_report")
    by_id = {}
    duplicates = set()
    for item in properties:
        if not isinstance(item, dict):
            continue
        siid, piid = item.get("siid"), item.get("piid")
        if type(siid) is not int or type(piid) is not int:
            continue
        identity = siid, piid
        if identity in by_id:
            duplicates.add(identity)
        by_id[identity] = item
    for identity in duplicates:
        by_id.pop(identity)
    measurements = {
        "voltage": _property_value(by_id.get((4, 3))) if config.model == SUPPORTED_MODEL else None,
        "current": _property_value(by_id.get((4, 4))) if config.model == SUPPORTED_MODEL else None,
        "power": _property_value(by_id.get((4, 2))) if config.model == SUPPORTED_MODEL else None,
        "relay_output": _property_value(by_id.get((2, 1)), boolean=True),
    }
    if measurements["current"] is not None:
        measurements["current"] /= 1000
    if operation == "relay" and measurements["relay_output"] is not expected_value:
        raise ValidationError("relay_readback_mismatch")
    return Snapshot(settings, capabilities, measurements)

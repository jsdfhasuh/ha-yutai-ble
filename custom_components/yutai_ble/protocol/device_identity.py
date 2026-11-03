"""Device identity shared by the HA entry and isolated worker."""

from dataclasses import dataclass, field
import re

SUPPORTED_MODEL = "yutai.plug.fsov8m"


def validate_identity(address, model):
    if not isinstance(address, str) or model != SUPPORTED_MODEL:
        raise ValueError("unsupported_device")
    address = address.strip().upper()
    if (re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address) is None
            or address in {"00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"}):
        raise ValueError("invalid_address")
    return address, model


@dataclass(frozen=True, slots=True)
class Target:
    address: str
    model: str
    key: bytes = field(repr=False)

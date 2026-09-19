"""Enums and dataclasses for Wattpilot property values and configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class LoadMode(IntEnum):
    """Charging load mode."""

    DEFAULT = 3
    ECO = 4
    NEXTTRIP = 5


class CarStatus(IntEnum):
    """Car connection / charging status."""

    NO_CAR = 1
    CHARGING = 2
    READY = 3
    COMPLETE = 4


class AccessState(IntEnum):
    """Access / lock state."""

    OPEN = 0
    WAIT = 1


class ErrorState(IntEnum):
    """Device error state."""

    UNKNOWN = 0
    IDLE = 1
    CHARGING = 2
    WAIT_CAR = 3
    COMPLETE = 4
    ERROR = 5


class CableLockMode(IntEnum):
    """Cable lock behaviour."""

    NORMAL = 0
    AUTO_UNLOCK = 1
    ALWAYS_LOCK = 2


class ForceState(IntEnum):
    """Force charging state."""

    NEUTRAL = 0
    OFF = 1
    ON = 2


class PhaseSwitchMode(IntEnum):
    """Phase switching mode."""

    AUTO = 0
    FORCE_1 = 1
    FORCE_3 = 2


class AuthHashType(StrEnum):
    """Authentication hash algorithm."""

    PBKDF2 = "pbkdf2"
    BCRYPT = "bcrypt"


@dataclass
class DeviceInfo:
    """Wattpilot device information populated from the hello message."""

    serial: str = ""
    name: str = ""
    hostname: str = ""
    friendly_name: str = ""
    manufacturer: str = ""
    device_type: str = ""
    protocol: int = 0
    secured: int = 0
    version: str = ""
    firmware: str = ""


@dataclass(frozen=True, slots=True)
class CloudInfo:
    """Cloud API information returned by :meth:`Wattpilot.enable_cloud_api`."""

    enabled: bool = False
    api_key: str = ""
    url: str = ""

"""Enums and dataclasses for Wattpilot property values and configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AuthHashType(StrEnum):
    """Authentication hash algorithm."""

    PBKDF2 = "pbkdf2"
    BCRYPT = "bcrypt"


@dataclass
class DeviceInfo:
    """Wattpilot device information populated from the hello message."""

    serial: str = ""
    name: str = ""
    manufacturer: str = ""
    device_type: str = ""
    secured: int = 0


@dataclass(frozen=True, slots=True)
class CloudInfo:
    """Cloud API information returned by :meth:`Wattpilot.enable_cloud_api`."""

    enabled: bool = False
    api_key: str = ""
    url: str = ""

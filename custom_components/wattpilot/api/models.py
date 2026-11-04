"""Enums, dataclasses and message rules of the Wattpilot protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


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


# What a handshake cannot go on without, per message type.
_REQUIRED_TEXT = {"hello": ("serial",), "authRequired": ("token1", "token2")}
# Absent it means the default; sent, it has to be a name.
_OPTIONAL_TEXT = {"authRequired": ("hash",)}


def missing_field(msg_type: str, message: dict[str, Any], serial: str) -> str | None:
    """Name the field a handshake message lacks, given the serial known so far."""
    if msg_type == "authRequired" and not serial:
        return "serial"  # the secret is derived from it, and hello brings it
    fields = _fields_to_check(msg_type, message)
    unusable = (name for name in fields if not _is_text(message.get(name)))
    return next(unusable, None)


def _fields_to_check(msg_type: str, message: dict[str, Any]) -> tuple[str, ...]:
    sent = (name for name in _OPTIONAL_TEXT.get(msg_type, ()) if name in message)
    return (*_REQUIRED_TEXT.get(msg_type, ()), *sent)


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)

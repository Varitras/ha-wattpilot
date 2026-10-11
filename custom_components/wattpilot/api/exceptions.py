"""Exception hierarchy for the wattpilot-api library."""

from __future__ import annotations

import errno


class WattpilotError(Exception):
    """Base exception for all wattpilot-api errors."""


class WattpilotConnectionError(WattpilotError):
    """
    Raised when a connection to the Wattpilot device fails.

    Named with the prefix on purpose: the original shadowed the builtin
    ConnectionError, so `except ConnectionError` in a caller silently caught
    a different class than it looked like.
    """


class AuthenticationError(WattpilotError):
    """Raised when authentication with the Wattpilot device fails."""


class PropertyError(WattpilotError):
    """Raised when a property operation fails (unknown key, read-only, etc.)."""


class CommandError(WattpilotError):
    """Raised when a command sent to the Wattpilot device fails."""


class DeviceIdentityError(WattpilotError):
    """Raised when the charger that answered is not the one this client knows."""

    def __init__(self, message: str, *, found: str) -> None:
        """Keep the serial that answered, for a message that names it."""
        super().__init__(message)
        self.found = found


def describe_error(error: BaseException) -> str:
    """
    wattpilot: an error fit for the log, which users attach to public issues.

    Our own messages are written to carry no identifier. A socket error's
    text quotes the charger's address and port, so anything else is named
    by its class and errno only.
    """
    if isinstance(error, WattpilotError):
        return str(error)
    code = getattr(error, "errno", None)
    if code is None:
        return type(error).__name__
    return f"{type(error).__name__} ({errno.errorcode.get(code, code)})"

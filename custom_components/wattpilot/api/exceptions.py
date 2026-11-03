"""Exceptions of the Wattpilot API client."""

from __future__ import annotations

import errno
import socket
import traceback
from pathlib import Path

_INTEGRATION = str(Path(__file__).parent.parent)


class WattpilotError(Exception):
    """Base exception of the Wattpilot API client."""


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
    """Raised when a value would change on conversion, or no update is offered."""


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
    Return an error fit for the log, which users attach to public issues.

    Our own messages are written to carry no identifier. A socket error's
    text quotes the charger's address and port, so anything else is named
    by its class and errno only -- a resolver error by its phrase.
    """
    if isinstance(error, WattpilotError):
        return str(error)
    if isinstance(error, socket.gaierror):
        # A resolver's phrase names no address, and its negative code has no
        # errno name: "gaierror (-2)" hid that the name did not resolve.
        return f"{type(error).__name__} ({error.strerror})"
    code = getattr(error, "errno", None)
    if code is None:
        return type(error).__name__
    return f"{type(error).__name__} ({errno.errorcode.get(code, code)})"


def where_raised(error: BaseException) -> str:
    """
    Return the integration's own line an error passed last, as file:line.

    Enough to find a defect from a log line; the message is left out, as it
    can quote what the charger sent.
    """
    own = [
        frame
        for frame in traceback.extract_tb(error.__traceback__)
        if frame.filename.startswith(_INTEGRATION)
    ]
    return f"{Path(own[-1].filename).name}:{own[-1].lineno}" if own else "?"

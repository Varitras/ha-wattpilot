"""Exception hierarchy for the wattpilot-api library."""


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

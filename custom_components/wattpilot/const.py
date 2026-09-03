"""
Constants for the Fronius Wattpilot integration.

The only module allowed to contain the domain literal (guard-enforced),
so that scripts/make_dev_copy.py stays a trivial two-file replace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

DOMAIN = "wattpilot"

CONF_CONNECTION_TYPE = "connection_type"
CONNECTION_LOCAL = "local"
CONNECTION_CLOUD = "cloud"

# Coalesce the charger's push flood: at most one entity update per property
# per interval (seconds). 0 disables it and forwards every push immediately.
# The device streams power and current every second while charging, which
# otherwise turns into that many state writes and recorder rows.
CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 5

# Set by the fork migration, cleared the first time setup reads the serial.
# Its presence is the only thing that makes adopting a *different* unique_id
# legitimate: a fork entry is keyed on the IP and has yet to learn its
# serial. Without it, a serial that disagrees with the entry means the
# address now belongs to another charger.
CONF_AWAITING_SERIAL = "awaiting_serial"

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
    Platform.UPDATE,
]


def awaits_serial(entry: ConfigEntry) -> bool:
    """
    Return whether this entry has yet to learn its charger's serial.

    Three shapes qualify, and all of them predate a successful local setup:
    no unique_id at all, the fork migration's one-time marker, and a cloud
    entry -- whose unique_id was assigned by an integration whose key this
    one has never verified against a live local connection.

    This is the single condition under which a serial that disagrees with
    the entry is an upgrade rather than a different charger. Both callers
    that decide identity -- setup adopting a serial, and the reconfigure
    flow deciding whether to reject one -- have to agree on it, or one of
    them locks the user out of a state the other happily creates.
    """
    return (
        entry.unique_id is None
        or bool(entry.data.get(CONF_AWAITING_SERIAL))
        or entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_CLOUD
    )


def signal_property(entry_id: str, key: str) -> str:
    """Dispatcher signal for one charger property of one config entry."""
    return f"{DOMAIN}_{entry_id}_{key}"


def signal_availability(entry_id: str) -> str:
    """Dispatcher signal for connection state changes of one config entry."""
    return f"{DOMAIN}_{entry_id}_availability"

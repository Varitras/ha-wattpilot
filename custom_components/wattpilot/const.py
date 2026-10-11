"""
Constants for the Fronius Wattpilot integration.

The only module allowed to contain the domain literal (guard-enforced),
so that scripts/make_dev_copy.py stays a trivial two-file replace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME, Platform

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

DOMAIN = "wattpilot"
# What a new charger is called; its entity IDs follow from it.
DEVICE_NAME = "Wattpilot"

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
    Platform.BINARY_SENSOR,
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


def entry_awaiting_serial_at(hass: HomeAssistant, host: str) -> ConfigEntry | None:
    """Return the entry at this address that has yet to learn its serial."""
    return next(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN, include_ignore=False)
            if awaits_serial(entry) and entry.data.get("host") == host
        ),
        None,
    )


def signal_property(entry_id: str, key: str) -> str:
    """Dispatcher signal for one charger property of one config entry."""
    return f"{DOMAIN}_{entry_id}_{key}"


def signal_availability(entry_id: str) -> str:
    """Dispatcher signal for connection state changes of one config entry."""
    return f"{DOMAIN}_{entry_id}_availability"


def serial_taken_by_other_entry(
    hass: HomeAssistant, entry: ConfigEntry, serial: str
) -> bool:
    """
    Return whether another entry already belongs to this charger.

    A second entry for the same charger would fight over the device. An
    ignored card is not one: it holds the serial only to keep the card from
    coming back, and counting it locked the fork entry out of its own
    charger for good (audit A18-07). Setup and the reconfigure and reauth
    flows decide on this together, like on awaits_serial.
    """
    return any(
        other.entry_id != entry.entry_id and other.unique_id == serial
        for other in hass.config_entries.async_entries(DOMAIN, include_ignore=False)
    )


def _names_taken(hass: HomeAssistant, exclude: str | None) -> set[str | None]:
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN, include_ignore=False)
        if entry.entry_id != exclude
    ]
    return {entry.title for entry in entries} | {
        entry.data.get(CONF_NAME) for entry in entries
    }


def new_device_name(
    hass: HomeAssistant, *, preferred: str | None = None, exclude: str | None = None
) -> str:
    """
    Name a new charger Wattpilot, numbered from the second one on.

    Not its host name, Wattpilot_<serial>: entity IDs follow the device
    name, and the serial in every one of them reached logs and posts. A
    renamed entry's device keeps the name it was created with. *preferred*
    wins while no other entry has it; *exclude* is the entry being named.
    """
    taken = _names_taken(hass, exclude)
    if preferred and preferred not in taken:
        return preferred
    name, number = DEVICE_NAME, 1
    while name in taken:
        number += 1
        name = f"{DEVICE_NAME} {number}"
    return name


def reload_failed_entry(
    hass: HomeAssistant, entry: ConfigEntry | None, updates: dict[str, str] | None
) -> None:
    """
    Give a failed entry its charger's announced address, and reload it.

    Home Assistant reloads only a loaded or retrying entry after a discovery
    update: one that met another charger at its old address and failed stayed
    failed with the right address stored (reaudit RF-E-01). Only a change
    reloads, or every announcement would.
    """
    if entry is None or not updates or entry.state is not ConfigEntryState.SETUP_ERROR:
        return
    if hass.config_entries.async_update_entry(entry, data={**entry.data, **updates}):
        hass.config_entries.async_schedule_reload(entry.entry_id)

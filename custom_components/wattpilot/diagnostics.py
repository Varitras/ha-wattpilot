"""
Diagnostics with sensitive-data redaction.

Charger properties are scrubbed by redaction.sanitize_snapshot(): the same
function scripts/anonymize_probe.py uses to build the committed test
fixture, so the two can never drift apart. That covers device-reported
values (credential keys dropped, "cci"/"cards" neutralized instead of
dropped outright, MAC addresses and network fields scrubbed regardless of
key name). TO_REDACT stays a small hand-picked list here only for
entry.data: the connection settings the user typed in and what the entry
stores about the charger (device name, a cloud entry's serial), which are
not device-reported properties and so are out of scope for redaction.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_NAME

from .redaction import sanitize_snapshot

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .hub import WattpilotConfigEntry

# Config-entry fields -- see module docstring. A carried-over device name is
# whatever the fork's user called the charger (README audit RM2-01).
TO_REDACT = {"password", "host", CONF_NAME, "serial"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the diagnostics platform API
    entry: WattpilotConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for the config entry."""
    hub = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "device": {
            "model": hub.charger.model,
            "variant": hub.variant,
            "firmware": hub.firmware,
            "connected": hub.available,
        },
        "properties": sanitize_snapshot(hub.properties),
    }

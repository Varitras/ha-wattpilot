"""
Diagnostics with sensitive-data redaction.

Charger properties are scrubbed by redaction.sanitize_snapshot(): the same
function scripts/anonymize_probe.py uses to build the committed test
fixture, so the two can never drift apart. That covers device-reported
values (credential keys dropped, "cci"/"cards" neutralized instead of
dropped outright, MAC addresses and network fields scrubbed regardless of
key name). TO_REDACT stays a small hand-picked list here only for
entry.data: the raw connection settings (password, host) the user typed in,
which are not device-reported properties and so are out of scope for
redaction.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .redaction import sanitize_snapshot

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .hub import WattpilotConfigEntry

# Config-entry connection settings -- see module docstring.
TO_REDACT = {"password", "host"}


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

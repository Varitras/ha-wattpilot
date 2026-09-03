"""Integration services (fork-compatible names and fields)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .hub import WattpilotHub

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})
_NEXT_TRIP_SCHEMA = _DEVICE_SCHEMA.extend({vol.Required("trigger_time"): cv.time})
_CLOUD_SCHEMA = _DEVICE_SCHEMA.extend({vol.Required("cloud_api"): cv.boolean})

# Prefix/suffix fragment shown for a logged cloud API key. At or below the
# threshold the two fragments would jointly cover (or, shorter still,
# repeat) every character of the key, so nothing of it may be shown at all.


def _hub_for_device(hass: HomeAssistant, device_id: str) -> WattpilotHub:
    """
    Resolve a service device_id to a loaded hub or raise a UI error.

    A device only resolves if its config entry belongs to this domain AND
    is currently loaded (has runtime_data) -- reaching a hub for a foreign
    or unloaded device would be a real bug, not a UI nicety.
    """
    device = dr.async_get(hass).async_get(device_id)
    if device is not None:
        entry = hass.config_entries.async_get_entry(device.config_entry_id)
        if (
            entry is not None
            and entry.domain == DOMAIN
            and hasattr(entry, "runtime_data")
            and isinstance(entry.runtime_data, WattpilotHub)
        ):
            return entry.runtime_data
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="invalid_device",
        translation_placeholders={"device_id": device_id},
    )


def async_setup_services(hass: HomeAssistant) -> None:
    """Register all integration services once."""

    async def set_next_trip(call: ServiceCall) -> None:
        """Set the charger's next scheduled departure time."""
        hub = _hub_for_device(hass, call.data["device_id"])
        await hub.async_set_next_trip(call.data["trigger_time"])

    async def set_goe_cloud(call: ServiceCall) -> None:
        """Enable or disable the charger's go-e cloud API."""
        hub = _hub_for_device(hass, call.data["device_id"])
        if call.data["cloud_api"]:
            await hub.async_enable_cloud_api()
            # Neither the key nor the URL belongs here: even a prefix and
            # suffix narrow the key, and the vendor URL embeds the full
            # serial. Both are readable from the charger for anyone who
            # needs them (audit VA-14).
            _LOGGER.info("go-e cloud API enabled")
        else:
            await hub.async_disable_cloud_api()

    async def disconnect_charger(call: ServiceCall) -> None:
        """Disconnect the charger's WebSocket connection."""
        hub = _hub_for_device(hass, call.data["device_id"])
        await hub.async_shutdown()

    async def reconnect_charger(call: ServiceCall) -> None:
        """Reconnect the charger and resume dispatching updates."""
        hub = _hub_for_device(hass, call.data["device_id"])
        await hub.async_reconnect()

    hass.services.async_register(
        DOMAIN, "set_next_trip", set_next_trip, schema=_NEXT_TRIP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "set_goe_cloud", set_goe_cloud, schema=_CLOUD_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "disconnect_charger", disconnect_charger, schema=_DEVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "reconnect_charger", reconnect_charger, schema=_DEVICE_SCHEMA
    )

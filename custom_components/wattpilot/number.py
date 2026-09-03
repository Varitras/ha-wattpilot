"""Number platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.number import NumberEntity

from .descriptions import (
    NUMBER_DESCRIPTIONS,
    WattpilotNumberEntityDescription,
    filter_supported,
)
from .entity import WattpilotEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .hub import WattpilotConfigEntry

PARALLEL_UPDATES = 1

# fte needs the esk companion flag; the vendor library sets that as part of
# set_next_trip_energy, so it bypasses the plain property-write path below.
_NEXT_TRIP_ENERGY_KEY = "fte"


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: WattpilotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up numbers for supported charger properties."""
    hub = entry.runtime_data
    descriptions = filter_supported(
        NUMBER_DESCRIPTIONS,
        firmware=hub.firmware,
        variant=hub.variant,
        properties=hub.properties,
    )
    async_add_entities(
        WattpilotNumber(hub, entry.entry_id, description)
        for description in descriptions
    )


class WattpilotNumber(WattpilotEntity, NumberEntity):
    """One numeric charger property as a number."""

    entity_description: WattpilotNumberEntityDescription

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload
        try:
            self._attr_native_value = float(value)
        except TypeError, ValueError:
            self._attr_native_value = None

    async def async_set_native_value(self, value: float) -> None:
        """Write the new value to the charger."""
        key = self.entity_description.charger_key
        if key == _NEXT_TRIP_ENERGY_KEY:
            await self._hub.async_set_next_trip_energy(value)
            return
        payload: float | int = (
            int(value) if self.entity_description.set_as_int else value
        )
        await self._hub.async_set_property(key, payload)

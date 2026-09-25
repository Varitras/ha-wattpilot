"""Binary sensor platform."""

from __future__ import annotations

from collections.abc import Hashable
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorEntity

from .descriptions import (
    BINARY_SENSOR_DESCRIPTIONS,
    WattpilotBinarySensorEntityDescription,
    filter_supported,
)
from .entity import WattpilotEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .hub import WattpilotConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: WattpilotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors for supported charger properties."""
    hub = entry.runtime_data
    descriptions = filter_supported(
        BINARY_SENSOR_DESCRIPTIONS,
        firmware=hub.firmware,
        variant=hub.variant,
        properties=hub.properties,
    )
    async_add_entities(
        WattpilotBinarySensor(hub, entry.entry_id, description)
        for description in descriptions
    )


class WattpilotBinarySensor(WattpilotEntity, BinarySensorEntity):
    """One yes/no reading of a charger property."""

    entity_description: WattpilotBinarySensorEntityDescription

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload
        description = self.entity_description
        self._attr_is_on = None
        # A malformed push (a list, say) would raise in the set lookup.
        if not isinstance(value, Hashable):
            return
        if value in description.on_values:
            self._attr_is_on = True
        elif value in description.off_values:
            self._attr_is_on = False

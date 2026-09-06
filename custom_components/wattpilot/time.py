"""Time platform (next trip departure)."""

from __future__ import annotations

from datetime import time as dt_time
from typing import TYPE_CHECKING, Any

from homeassistant.components.time import TimeEntity

from .descriptions import (
    TIME_DESCRIPTIONS,
    WattpilotTimeEntityDescription,
    filter_supported,
)
from .entity import WattpilotEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .hub import WattpilotConfigEntry

PARALLEL_UPDATES = 1

_SECONDS_PER_DAY = 86400


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: WattpilotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the next-trip time entity."""
    hub = entry.runtime_data
    descriptions = filter_supported(
        TIME_DESCRIPTIONS,
        firmware=hub.firmware,
        variant=hub.variant,
        properties=hub.properties,
    )
    async_add_entities(
        WattpilotTime(hub, entry.entry_id, description) for description in descriptions
    )


class WattpilotTime(WattpilotEntity, TimeEntity):
    """Next-trip departure; wire format is seconds since midnight."""

    entity_description: WattpilotTimeEntityDescription

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload
        try:
            seconds = int(value) % _SECONDS_PER_DAY
        except TypeError, ValueError:
            self._attr_native_value = None
            return
        self._attr_native_value = dt_time(
            seconds // 3600, (seconds % 3600) // 60, seconds % 60
        )

    async def async_set_value(self, value: dt_time) -> None:
        """Write through the client, which sends seconds since local midnight."""
        await self._hub.async_set_next_trip(value)

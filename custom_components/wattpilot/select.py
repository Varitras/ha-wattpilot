"""Select platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import ServiceValidationError

from .descriptions import (
    SELECT_DESCRIPTIONS,
    WattpilotSelectEntityDescription,
    filter_supported,
)
from .entity import WattpilotEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .hub import WattpilotConfigEntry, WattpilotHub

PARALLEL_UPDATES = 1


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: WattpilotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up selects for supported charger properties."""
    hub = entry.runtime_data
    descriptions = filter_supported(
        SELECT_DESCRIPTIONS,
        firmware=hub.firmware,
        variant=hub.variant,
        properties=hub.properties,
    )
    async_add_entities(
        WattpilotSelect(hub, entry.entry_id, description)
        for description in descriptions
    )


class WattpilotSelect(WattpilotEntity, SelectEntity):
    """One enumerated charger property as a select."""

    entity_description: WattpilotSelectEntityDescription

    def __init__(
        self,
        hub: WattpilotHub,
        entry_id: str,
        description: WattpilotSelectEntityDescription,
    ) -> None:
        """Set the static option list alongside the base entity's setup."""
        super().__init__(hub, entry_id, description)
        self._attr_options = list(description.select_options.values())

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload
        self._attr_current_option = self.entity_description.select_options.get(value)

    async def async_select_option(self, option: str) -> None:
        """Write the wire value matching the chosen label."""
        for wire_value, label in self.entity_description.select_options.items():
            if label == option:
                await self._hub.async_set_property(
                    self.entity_description.charger_key, wire_value
                )
                return
        raise ServiceValidationError(f"Unknown option: {option}")

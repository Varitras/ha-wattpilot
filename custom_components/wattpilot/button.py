"""Button platform."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity

from .descriptions import (
    BUTTON_DESCRIPTIONS,
    WattpilotButtonEntityDescription,
    filter_supported,
)
from .entity import WattpilotEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .hub import WattpilotConfigEntry

PARALLEL_UPDATES = 1


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: WattpilotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up charger action buttons."""
    hub = entry.runtime_data
    descriptions = filter_supported(
        BUTTON_DESCRIPTIONS,
        firmware=hub.firmware,
        variant=hub.variant,
        properties=hub.properties,
    )
    async_add_entities(
        WattpilotButton(hub, entry.entry_id, description)
        for description in descriptions
    )


class WattpilotButton(WattpilotEntity, ButtonEntity):
    """Stateless action button."""

    entity_description: WattpilotButtonEntityDescription

    async def async_press(self) -> None:
        """Write the action value to the charger."""
        await self._hub.async_set_property(
            self.entity_description.charger_key,
            self.entity_description.press_value,
        )

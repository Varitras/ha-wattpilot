"""Switch platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity

from .descriptions import (
    SWITCH_DESCRIPTIONS,
    WattpilotSwitchEntityDescription,
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
    """Set up switches for supported charger properties."""
    hub = entry.runtime_data
    descriptions = filter_supported(
        SWITCH_DESCRIPTIONS,
        firmware=hub.firmware,
        variant=hub.variant,
        properties=hub.properties,
    )
    async_add_entities(
        WattpilotSwitch(hub, entry.entry_id, description)
        for description in descriptions
    )


class WattpilotSwitch(WattpilotEntity, SwitchEntity):
    """One boolean charger property as a switch."""

    entity_description: WattpilotSwitchEntityDescription

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload
        if not isinstance(value, bool):
            self._attr_is_on = None
            return
        self._attr_is_on = (not value) if self.entity_description.invert else value

    async def async_turn_on(self, **_kwargs: Any) -> None:  # noqa: ANN401 -- HA's ToggleEntity base signature requires **kwargs: Any
        """Turn on: write the (possibly inverted) wire value."""
        await self._hub.async_set_property(
            self.entity_description.charger_key,
            not self.entity_description.invert,
        )

    async def async_turn_off(self, **_kwargs: Any) -> None:  # noqa: ANN401 -- HA's ToggleEntity base signature requires **kwargs: Any
        """Turn off: write the (possibly inverted) wire value."""
        await self._hub.async_set_property(
            self.entity_description.charger_key,
            self.entity_description.invert,
        )

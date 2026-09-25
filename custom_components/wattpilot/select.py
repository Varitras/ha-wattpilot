"""Select platform."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
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
        """Set the option list alongside the base entity's setup."""
        super().__init__(hub, entry_id, description)
        self._attr_options = list(self._wire_options().values())

    def _wire_options(self) -> dict[Any, str]:
        """Wire value -> label: the fixed table, or the charger's own list."""
        description = self.entity_description
        if description.companion_key is None:
            return description.select_options
        allowed = self._hub.get_property(description.companion_key)
        if not isinstance(allowed, list):
            return {}
        return {value: f"{value} A" for value in allowed}

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload
        options = self._wire_options()
        self._attr_options = list(options.values())
        self._attr_current_option = options.get(value)

    async def async_select_option(self, option: str) -> None:
        """Write the wire value matching the chosen label."""
        for wire_value, label in self._wire_options().items():
            if label == option:
                await self._hub.async_set_property(
                    self.entity_description.charger_key, wire_value
                )
                return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_option",
            translation_placeholders={"option": option},
        )

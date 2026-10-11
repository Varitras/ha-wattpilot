"""Select platform."""

from __future__ import annotations

import logging
from collections.abc import Hashable
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
from .readings import describe_with_length

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .hub import WattpilotConfigEntry, WattpilotHub

_LOGGER = logging.getLogger(__name__)

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
    _last_unknown: Any = None

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
        # An unhashable entry raised here, in __init__, and took the whole
        # platform with it (post-check PC7-02).
        return {value: f"{value} A" for value in allowed if isinstance(value, Hashable)}

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload
        options = self._wire_options()
        self._attr_options = list(options.values())
        known = isinstance(value, Hashable) and value in options
        self._attr_current_option = options[value] if known else None
        self._report_unknown(value, known=known)

    def _report_unknown(self, value: Any, *, known: bool) -> None:  # noqa: ANN401 -- charger payload
        # A preset select sees a current between its presets as a matter of
        # course; a fixed table does not, and unknown alone told nobody (#25).
        description = self.entity_description
        if known or value is None or description.companion_key is not None:
            self._last_unknown = None
            return
        if value != self._last_unknown:
            # The shape, never the value: a car profile can be free text.
            _LOGGER.warning(
                "%s: the charger reports a value outside the known options for %s (%s)",
                description.key,
                description.charger_key,
                describe_with_length(value),
            )
        self._last_unknown = value

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

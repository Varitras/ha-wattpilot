"""Sensor platform."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.util import dt as dt_util

from .descriptions import (
    SENSOR_DESCRIPTIONS,
    WattpilotSensorEntityDescription,
    filter_supported,
)
from .entity import WattpilotEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .hub import WattpilotConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Same ratio Home Assistant's own recorder uses to tell a TOTAL_INCREASING
# reset apart from measurement noise (reset_detected() in
# homeassistant.components.sensor.recorder: `fstate < 0.9 * previous_fstate`).
_RESET_THRESHOLD_RATIO = 0.9


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: WattpilotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for supported charger properties."""
    hub = entry.runtime_data
    descriptions = filter_supported(
        SENSOR_DESCRIPTIONS,
        firmware=hub.firmware,
        variant=hub.variant,
        properties=hub.properties,
    )
    async_add_entities(
        WattpilotSensor(hub, entry.entry_id, description)
        for description in descriptions
    )


def _describe_shape(value: object) -> str:
    """Name what a value is without repeating what it says."""
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__} of {len(value)}"
    if isinstance(value, dict):
        return f"dict of {len(value)}"
    return type(value).__name__


def _namespace_get(value: Any, field: str) -> Any:  # noqa: ANN401 -- dynamically shaped charger payload
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


class WattpilotSensor(WattpilotEntity, SensorEntity):
    """One charger property as a sensor."""

    entity_description: WattpilotSensorEntityDescription

    def __init__(self, *args: Any) -> None:  # noqa: ANN401 -- forwards WattpilotEntity's args unchanged
        """Set the ID-chip index placeholder, if this sensor has one."""
        super().__init__(*args)
        if self.entity_description.cards_index is not None:
            self._attr_translation_placeholders = {
                "index": str(self.entity_description.cards_index)
            }

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401, C901 -- dynamically shaped charger payload; branches are the conversion rules, see class/task docs
        description = self.entity_description
        try:
            if description.cards_index is not None:
                self._apply_card(value, description.cards_index)
                return
            if description.value_index is not None:
                item = value[description.value_index]
                if description.index_attributes:
                    self._attr_extra_state_attributes = {
                        name: value[index]
                        for name, index in description.index_attributes.items()
                    }
                value = item
            elif description.namespace_value is not None:
                state = _namespace_get(value, description.namespace_value)
                self._attr_extra_state_attributes = {
                    field: _namespace_get(value, field)
                    for field in description.namespace_attributes
                }
                value = state
            if value is not None and description.html_unescape:
                value = html.unescape(str(value))
            if description.device_class is SensorDeviceClass.TIMESTAMP:
                self._attr_native_value = self._parse_timestamp(value)
                return
            if description.enum is not None:
                self._attr_native_value = description.enum.get(value, value)
                return
            if isinstance(value, (int, float)):
                if description.clamp_non_negative:
                    value = max(0, value)
                if (
                    description.reset_tolerant_monotonic
                    and description.suggested_display_precision is not None
                ):
                    # Round before deciding: the decision must match what's
                    # actually reported, not a raw value HA never sees.
                    value = round(value, description.suggested_display_precision)
                if self._should_suppress_decrease(description, value):
                    return
            self._attr_native_value = value
        except (TypeError, ValueError, LookupError) as err:
            # Bounded, purposeful catch: one malformed push must not kill
            # the platform; keep the previous state.
            # Shape and error type, never the value: this sink is not the
            # diagnostics download and nothing redacts it, so a card list
            # logged with %r put holder names into the log (audit A12-05).
            # The exception's own message can quote the value too, so only
            # its type is reported.
            _LOGGER.warning(
                "%s: unexpected %s for %s (%s)",
                self.entity_id,
                _describe_shape(value),
                description.charger_key,
                type(err).__name__,
            )

    def _apply_card(self, value: Any, index: int) -> None:  # noqa: ANN401 -- charger payload
        """
        Show one ID chip, or nothing when the charger has no such slot.

        Ten of these sensors exist and a charger may report fewer cards. An
        absent slot is the entity having nothing to show, not a malformed
        push: indexing past the end raised, and the generic handler then
        logged the whole card list with %r -- holder names included, in a
        sink nothing redacts (audit A12-05).
        """
        if index >= len(value):
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        card = value[index]
        self._attr_native_value = _namespace_get(card, "energy")
        self._attr_extra_state_attributes = {
            "name": _namespace_get(card, "name"),
            "cardId": _namespace_get(card, "cardId"),
        }

    def _should_suppress_decrease(
        self, description: WattpilotSensorEntityDescription, value: float
    ) -> bool:
        """Decide whether a lower value than last reported should be dropped."""
        previous = self._attr_native_value
        if not isinstance(previous, (int, float)) or value >= previous:
            return False
        if description.monotonic:
            return True
        if description.reset_tolerant_monotonic:
            # Below the threshold: a genuine counter reset, let it through.
            # Above it: measurement noise, keep reporting `previous`.
            return value >= previous * _RESET_THRESHOLD_RATIO
        return False

    @staticmethod
    def _parse_timestamp(value: Any) -> Any:  # noqa: ANN401 -- dynamically shaped charger payload
        if value is None or not isinstance(value, str):
            return None
        # Charger format: "2026-08-10 23:27:03.866 +02:00" (also seen with a
        # "T" date/time separator). The space before the offset breaks
        # fromisoformat, so retry with it collapsed. Keep the charger's own
        # offset rather than converting to HA's configured timezone: HA's
        # SensorEntity already normalizes any tz-aware datetime to UTC when
        # writing state, so a second conversion here would only change what
        # .hour reports depending on the HA instance's timezone, not the
        # exposed state.
        return dt_util.parse_datetime(value) or dt_util.parse_datetime(
            value.replace(" +", "+").replace(" -", "-")
        )

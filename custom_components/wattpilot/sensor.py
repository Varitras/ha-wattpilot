"""Sensor platform."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.components.sensor.const import NON_NUMERIC_DEVICE_CLASSES
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .descriptions import (
    NRG_ATTRIBUTES,
    SENSOR_DESCRIPTIONS,
    CompanionRole,
    WattpilotSensorEntityDescription,
    filter_supported,
)
from .entity import WattpilotEntity
from .readings import (
    charging_minutes,
    describe_shape,
    is_number,
    namespace_get,
    number_or_none,
    price_list,
    switched_phase_count,
    total_power,
)

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .hub import WattpilotConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Same ratio Home Assistant's own recorder uses to tell a TOTAL_INCREASING
# reset apart from measurement noise (reset_detected() in
# homeassistant.components.sensor.recorder: `fstate < 0.9 * previous_fstate`).
_RESET_THRESHOLD_RATIO = 0.9
# A pause inside a session, a phase switch for one, is not its end.
_IDLE_AFTER_SECONDS = 60


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
        _sensor_class(description)(hub, entry.entry_id, description)
        for description in descriptions
    )


def _sensor_class(
    description: WattpilotSensorEntityDescription,
) -> type[WattpilotSensor]:
    if description.companion_role is CompanionRole.BOOT_CLOCK:
        return WattpilotChargingDurationSensor
    if description.monotonic:
        return WattpilotTotalSensor
    if description.companion_role is CompanionRole.LIVE_POWER:
        return WattpilotAveragePowerSensor
    return WattpilotSensor


def _expects_number(description: WattpilotSensorEntityDescription) -> bool:
    """Home Assistant's rule: such a sensor's state must be a number (K-A)."""
    if description.device_class in NON_NUMERIC_DEVICE_CLASSES or description.enum:
        return False
    numeric_marks = (
        description.device_class,
        description.state_class,
        description.native_unit_of_measurement,
        description.suggested_display_precision,
    )
    return any(mark is not None for mark in numeric_marks)


class WattpilotSensor(WattpilotEntity, SensorEntity):
    """One charger property as a sensor."""

    entity_description: WattpilotSensorEntityDescription
    # A day or two of hourly prices, rewritten every hour, and per-phase
    # readings that differ in nearly every update while charging: history
    # of either would only fill the recorder.
    _unrecorded_attributes = frozenset({"prices", "readings", *NRG_ATTRIBUTES})
    # Held against, not an unknown a null left on display (RF-D-02).
    _last_number: float | None = None

    def __init__(self, *args: Any) -> None:  # noqa: ANN401 -- forwards WattpilotEntity's args unchanged
        """Set the ID-chip index placeholder, if this sensor has one."""
        super().__init__(*args)
        self._expects_number = _expects_number(self.entity_description)
        if self.entity_description.chip_index is not None:
            self._attr_translation_placeholders = {
                "index": str(self.entity_description.chip_index)
            }

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload; branches are the conversion rules, see class/task docs
        description = self.entity_description
        try:
            if description.cards_index is not None:
                self._apply_card(value, description.cards_index)
                return
            value = self._unwrap(value)
            self._apply_price_list()
            if value is not None and description.html_unescape:
                value = html.unescape(str(value))
            if description.device_class is SensorDeviceClass.TIMESTAMP:
                self._attr_native_value = self._parse_timestamp(value)
                return
            if description.enum is not None:
                self._attr_native_value = description.enum.get(value, value)
                return
            if self._expects_number:
                value = self._number(description, value)
            self._attr_native_value = value
        except (TypeError, ValueError, LookupError) as err:
            # Bounded, purposeful catch: one malformed push must not kill
            # the platform; keep the previous state.
            # Shape and error type, never the value: this sink is not the
            # diagnostics download and nothing redacts it, so a card list
            # logged with %r put holder names into the log (audit A12-05).
            # The exception's own message can quote the value too, so only
            # its type is reported. The sensor by its key: the entity ID
            # carries the serial (R1-02).
            _LOGGER.warning(
                "%s: unexpected %s for %s (%s)",
                description.key,
                describe_shape(value),
                description.charger_key,
                type(err).__name__,
            )

    def _number(
        self,
        description: WattpilotSensorEntityDescription,
        value: Any,  # noqa: ANN401 -- charger payload
    ) -> float | None:
        """Return the number to show; a held step back shows the last one."""
        if value is None and description.null_is_zero:
            return 0
        value = number_or_none(value)
        return None if value is None else self._held(description, value)

    def _held(
        self, description: WattpilotSensorEntityDescription, value: float
    ) -> float | None:
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
            return self._last_number
        self._last_number = value
        return value

    def _unwrap(self, value: Any) -> Any:  # noqa: ANN401 -- dynamically shaped charger payload
        """Pick the reading out of a structured payload, with its attributes."""
        description = self.entity_description
        if description.value_index is not None:
            return self._unwrap_indexed(value, description.value_index)
        if description.namespace_value is not None:
            return self._unwrap_namespace(value, description.namespace_value)
        if description.highest_reading:
            return self._unwrap_highest(value)
        if description.switched_phases:
            return switched_phase_count(value)
        return value

    def _unwrap_indexed(self, value: Any, index: int) -> Any:  # noqa: ANN401 -- dynamically shaped charger payload
        attributes = self.entity_description.index_attributes
        if attributes:
            self._attr_extra_state_attributes = {
                name: value[position] for name, position in attributes.items()
            }
        return value[index]

    def _unwrap_namespace(self, value: Any, field: str) -> Any:  # noqa: ANN401 -- dynamically shaped charger payload
        self._attr_extra_state_attributes = {
            name: namespace_get(value, name)
            for name in self.entity_description.namespace_attributes
        }
        return namespace_get(value, field)

    def _unwrap_highest(self, value: Any) -> Any:  # noqa: ANN401 -- dynamically shaped charger payload
        self._attr_extra_state_attributes = {"readings": value}
        readings = [item for item in value if is_number(item)]
        return max(readings, default=None)

    def _apply_price_list(self) -> None:
        """Show the companion price list, for the one sensor that has one."""
        key = self.entity_description.companion_key
        if key is None or self.entity_description.companion_role is not (
            CompanionRole.PRICES
        ):
            return
        self._attr_extra_state_attributes = {
            "prices": price_list(self._hub.get_property(key))
        }

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
        self._attr_native_value = number_or_none(namespace_get(card, "energy"))
        self._attr_extra_state_attributes = {
            "name": namespace_get(card, "name"),
            "cardId": namespace_get(card, "cardId"),
        }

    def _should_suppress_decrease(
        self, description: WattpilotSensorEntityDescription, value: float
    ) -> bool:
        """Decide whether a lower value than last reported should be dropped."""
        previous = self._last_number
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
        if not isinstance(value, str):
            return None
        # Charger format: "2026-08-10 23:27:03.866 +02:00" (also seen with a
        # "T" date/time separator). The space before the offset breaks
        # fromisoformat, so retry with it collapsed. Keep the charger's own
        # offset rather than converting to HA's configured timezone: HA's
        # SensorEntity already normalizes any tz-aware datetime to UTC when
        # writing state, so a second conversion here would only change what
        # .hour reports depending on the HA instance's timezone, not the
        # exposed state.
        return _instant(
            dt_util.parse_datetime(value)
            or dt_util.parse_datetime(value.replace(" +", "+").replace(" -", "-"))
        )


def _instant(parsed: datetime | None) -> datetime | None:
    """
    Return the charger's time, or None when Home Assistant cannot show it.

    The charger's offset is kept: Home Assistant converts to UTC itself when
    writing the state, and refuses a time without offset or with no UTC
    equivalent. Either refusal kept the enabled sensor from being added.
    """
    if parsed is None or parsed.tzinfo is None:
        return None
    try:
        dt_util.as_utc(parsed)  # the conversion Home Assistant will make
    except OverflowError:
        return None
    return parsed


class WattpilotTotalSensor(WattpilotSensor, RestoreSensor):
    """A total that never falls, also across a reload or restart."""

    _restored_total: float | None = None

    async def async_added_to_hass(self) -> None:
        """Seed the hold against a step back with the last reported total."""
        # In memory only, the hold ended with the entity: a reload in the
        # window after a charger restart -- the one a firmware update
        # triggers -- reported the lower total (audit A18-25).
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, (int, float)):
            self._attr_native_value = self._restored_total = last.native_value
            self._last_number = last.native_value
        await super().async_added_to_hass()

    def _should_suppress_decrease(
        self, description: WattpilotSensorEntityDescription, value: float
    ) -> bool:
        restored, self._restored_total = self._restored_total, None
        # A restored total can be another charger's: a replacement that took
        # over this entity ID was held at the old total for years (R1-03).
        # Only a step back as small as a restart's is held.
        if restored is not None and value < restored * _RESET_THRESHOLD_RATIO:
            return False
        return super()._should_suppress_decrease(description, value)


class WattpilotChargingDurationSensor(WattpilotSensor):
    """Charging duration: cdi counted against the boot clock beside it."""

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- charger payload
        # Never None where a role is set (tests/test_descriptions.py holds
        # that); a runtime check or cast here would only be a dead branch.
        boot_clock = self._hub.get_property(
            self.entity_description.companion_key  # type: ignore[arg-type]
        )
        self._attr_native_value = charging_minutes(value, boot_clock)


class WattpilotAveragePowerSensor(WattpilotSensor):
    """Average charging power, 0 W once the current has stopped for a minute."""

    _cancel_idle: CALLBACK_TYPE | None = None
    _stale: object = None  # the average the last session left behind

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- charger payload
        # tpa keeps the last session's average after charging, and threshold
        # automations kept firing on it for hours (audit A18-13).
        live = total_power(
            self._hub.get_property(self.entity_description.companion_key)  # type: ignore[arg-type]
        )
        if live is None or live > 0:
            self._stop_idle_timer()
            self._show_live(value, live)
        else:
            self._hold_while_idle()

    def _show_live(self, value: Any, live: float | None) -> None:  # noqa: ANN401 -- charger payload
        if not live or value is None:
            # Unknown, whichever reading is: no telling a stale average, and
            # which one is stale stays known (PC5-R-03, PC6-03, PC7-01).
            super()._apply_value(None)
            return
        if value == self._stale:
            self._attr_native_value = 0  # until this session's own average (RF-D-04)
            return
        self._stale = None
        super()._apply_value(value)

    def _hold_while_idle(self) -> None:
        if not self._attr_native_value:
            # Nothing to hold, as when the entry starts with nothing flowing.
            self._show_idle()
        elif self._cancel_idle is None:
            self._cancel_idle = async_call_later(
                self.hass, _IDLE_AFTER_SECONDS, self._went_idle
            )

    @callback
    def _went_idle(self, _now: datetime) -> None:
        self._show_idle()
        self.async_write_ha_state()

    def _show_idle(self) -> None:
        self._attr_native_value = 0
        self._stale = self._hub.get_property(self.entity_description.charger_key)

    def _stop_idle_timer(self) -> None:
        if self._cancel_idle is not None:
            self._cancel_idle()
            self._cancel_idle = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending switch to 0 W."""
        self._stop_idle_timer()
        await super().async_will_remove_from_hass()

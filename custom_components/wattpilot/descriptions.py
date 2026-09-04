"""
Declarative entity descriptions — the single source of truth.

Pure data + filtering. Must not import the API module (architecture guard).
"""

from __future__ import annotations

from dataclasses import dataclass
from operator import eq, ge, gt, le, lt
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntityDescription
from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntityDescription
from homeassistant.components.time import TimeEntityDescription
from homeassistant.components.update import UpdateDeviceClass, UpdateEntityDescription
from homeassistant.const import EntityCategory
from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

# Charger hardware variants, as the device reports them in "var". The value
# is compared as a string because the real device sends the integer 11 while
# the vendor client annotates the property as str.
VARIANT_11 = "11"
VARIANT_22 = "22"

_COMPARATORS: dict[str, Callable[[Version, Version], bool]] = {
    "<=": le,
    ">=": ge,
    "==": eq,
    "<": lt,
    ">": gt,
}
_PREFIX_ORDER = sorted(_COMPARATORS, key=len, reverse=True)


@dataclass(frozen=True, kw_only=True)
class WattpilotDescriptionMixin:
    """Wattpilot-specific fields shared by all platform descriptions."""

    charger_key: str
    # unique_id suffix override; defaults to charger_key. Fork-compatible
    # values are frozen in tests/fixtures/fork_parity.json — never rename.
    uid: str | None = None
    # Firmware constraint, e.g. ">=41.7". One operator, one version.
    firmware: str | None = None
    # Charger variant filter: "11" or "22" (kW).
    variant: str | None = None
    # Buttons/update trigger keys are write-only: no presence check.
    requires_property: bool = True

    @property
    def uid_suffix(self) -> str:
        """Suffix used in the entity unique_id `<serial>-<suffix>`."""
        return self.uid or self.charger_key


def firmware_supported(current: str | None, constraint: str | None) -> bool:
    """
    Return whether the charger firmware satisfies the constraint.

    Unknown firmware or an unparsable constraint disables the entity
    instead of crashing the platform.
    """
    if constraint is None:
        return True
    if current is None:
        return False
    matched_operator = next(
        (op for op in _PREFIX_ORDER if constraint.startswith(op)), None
    )
    if matched_operator is None:
        return False
    try:
        current_version = Version(current)
        wanted = Version(constraint[len(matched_operator) :])
    except InvalidVersion:
        return False
    return _COMPARATORS[matched_operator](current_version, wanted)


def variant_supported(current: object, constraint: str | None) -> bool:
    """Return whether the charger variant (11/22 kW) matches the filter."""
    if constraint is None:
        return True
    if current is None:
        return False
    return str(current) == constraint


def filter_supported[DescriptionT: WattpilotDescriptionMixin](
    descriptions: Sequence[DescriptionT],
    *,
    firmware: str | None,
    variant: object,
    properties: Mapping[str, object],
) -> list[DescriptionT]:
    """Keep descriptions supported by this device."""
    return [
        description
        for description in descriptions
        if firmware_supported(firmware, description.firmware)
        and variant_supported(variant, description.variant)
        and (not description.requires_property or description.charger_key in properties)
    ]


# ---------------------------------------------------------------------------
# Sensor descriptions
# ---------------------------------------------------------------------------

# The charge state of the vehicle, from device property "car". Values
# verified against the pinned fork commit 1decee7.
CAR_STATE_ENUM: dict[Any, str] = {
    0: "Unknown",
    1: "Idle",
    2: "Charging",
    3: "Wait Car",
    4: "Complete",
    5: "Error",
}

# car_connected reads the same "car" property but answers a different
# question: is a cable plugged into a car at all. Every state that implies
# one -- charging, waiting for the car, complete -- collapses to Connected.
# Mapping both sensors through CAR_STATE_ENUM made them literal duplicates;
# the deysel fork split them the same way in 2026.8.0.
CAR_CONNECTION_ENUM: dict[Any, str] = {
    0: "Unknown",
    1: "Disconnected",
    2: "Connected",
    3: "Connected",
    4: "Connected",
    5: "Error",
}


@dataclass(frozen=True, kw_only=True)
class WattpilotSensorEntityDescription(
    WattpilotDescriptionMixin, SensorEntityDescription
):
    """Sensor description with Wattpilot conversion rules."""

    enum: dict[Any, str] | None = None
    value_index: int | None = None
    index_attributes: dict[str, int] | None = None
    namespace_value: str | None = None
    namespace_attributes: tuple[str, ...] = ()
    html_unescape: bool = False
    clamp_non_negative: bool = False
    monotonic: bool = False
    # Distinct from `monotonic`: decreases are compared against the reset
    # threshold instead of being rejected outright, see sensor.py.
    reset_tolerant_monotonic: bool = False
    cards_index: int | None = None


_NRG_ATTRIBUTES = {
    "L1_Voltage": 0,
    "L2_Voltage": 1,
    "L3_Voltage": 2,
    "N_Voltage": 3,
    "L1_Ampere": 4,
    "L2_Ampere": 5,
    "L3_Ampere": 6,
    "L1_Power": 7,
    "L2_Power": 8,
    "L3_Power": 9,
    "N_Power": 10,
    "TotalPower": 11,
    "L1_PowerRelative": 12,
    "L2_PowerRelative": 13,
    "L3_PowerRelative": 14,
    "N_PowerRelative": 15,
}

_INTERNAL_ERROR_ENUM: dict[Any, str] = {
    0: "None",
    1: "FI AC",
    2: "FI DC",
    3: "Phase",
    4: "Overvolt",
    5: "Overamp",
    6: "Diode",
    7: "PP Invalid",
    8: "Gnd Invalid",
    9: "Contactor Stuck",
    10: "Contactor Missing",
    11: "FI Unknown",
    12: "Unknown",
    13: "Overtemp",
    14: "No Comm",
    15: "Status Lock Stuck Open",
    16: "Status Lock Stuck Locked",
    20: "Reserved 20",
    21: "Reserved 21",
    22: "Reserved 22",
    23: "Reserved 23",
    24: "Reserved 24",
}

_CHARGING_REASON_ENUM: dict[Any, str] = {
    0: "Not Charging - No Charge Control Data",
    1: "Not Charging - Overtemperature",
    2: "Not Charging - Access Control Wait",
    3: "Charging - Force State On",
    4: "Not Charging - Force State Off",
    5: "Not Charging - Scheduler",
    6: "Not Charging - Energy Limit",
    7: "Charging - aWattar Price Low",
    8: "Charging - Automatic Stop Test",
    9: "Charging - Automatic Stop Not Enough Time",
    10: "Charging - Automatic Stop",
    11: "Charging - Automatic Stop No Clock",
    12: "Charging - PV Surplus",
    13: "Charging - Fallback Go-e Default",
    14: "Charging - Fallback Go-e Scheduler",
    15: "Charging - Fallback Default",
    16: "Not Charging - Fallback Go-e aWattar",
    17: "Not Charging - Fallback aWattar",
    18: "Not Charging - Fallback Automatic Stop",
    19: "Charging - Car Compatibility Keep Alive",
    20: "Charging - Charge Pause Not Allowed",
    22: "Not Charging - Simulate Unplugging",
    23: "Not Charging - Phase Switch",
    24: "Not Charging - Min Pause Duration",
    26: "Not Charging - Error",
    27: "Not Charging - Load Management Doesn't Want",
    28: "Not Charging - OCPP Doesn't Want",
    29: "Not Charging - Reconnect Delay",
    30: "Not Charging - Adapter Blocking",
    31: "Not Charging - Underfrequency Control",
    32: "Not Charging - Unbalanced Load",
    33: "Charging - Discharging PV Battery",
    34: "Not Charging - Grid Monitoring",
    35: "Not Charging - OCPP Fallback",
}

_LOCK_FEEDBACK_ENUM: dict[Any, str] = {
    0: "Unknown",
    1: "Unlocked",
    2: "Unlock Failed",
    3: "Locked",
    4: "Lock Failed",
    5: "Lock/Unlock Power Out",
}

# The fork maps a missing transaction to 999 via a `default_state` field we
# don't have (see WattpilotSensorEntityDescription); a direct `None` entry
# gets the same "No Transaction" result when the charger pushes a null trx.
_ID_CHIP_ENUM: dict[Any, str] = {
    None: "No Transaction",
    0: "No Chip",
    **{i: f"ID Chip {i - 1}" for i in range(1, 11)},
    999: "No Transaction",
}

_WIFI_STATE_ENUM: dict[Any, str] = {
    0: "Idle",
    1: "No SSID Available",
    2: "Scan Completed",
    3: "Connected",
    4: "Connect Failed",
    5: "Connection Lost",
    6: "Disconnected",
    8: "Connecting",
    9: "Disconnecting",
    10: "No Shield",
}


def _energy_split_sensor(
    charger_key: str, key: str
) -> WattpilotSensorEntityDescription:
    """
    Session-energy-by-source sensors (whs/whb/whg/who).

    Undocumented firmware properties found by probing firmware 42.5:
    wh == whs + whb + whg + who (verified against a live device).
    """
    return WattpilotSensorEntityDescription(
        key=key,
        charger_key=charger_key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="Wh",
        suggested_unit_of_measurement="kWh",
        suggested_display_precision=2,
        clamp_non_negative=True,
    )


SENSOR_DESCRIPTIONS: tuple[WattpilotSensorEntityDescription, ...] = (
    WattpilotSensorEntityDescription(
        key="access_state",
        charger_key="acs",
        uid="access_state",
        translation_key="access_state",
        enum={0: "Open", 1: "Authentication Required"},
    ),
    WattpilotSensorEntityDescription(
        key="car_connected",
        charger_key="car",
        uid="car_connected",
        translation_key="car_connected",
        enum=CAR_CONNECTION_ENUM,
    ),
    *(
        WattpilotSensorEntityDescription(
            key=f"id_chip_{i}",
            charger_key="cards",
            uid=f"cards_{i}",
            translation_key="id_chip",
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement="Wh",
            suggested_unit_of_measurement="kWh",
            entity_registry_enabled_default=False,
            cards_index=i,
        )
        for i in range(10)
    ),
    WattpilotSensorEntityDescription(
        key="car_state",
        charger_key="car",
        translation_key="car_state",
        enum=CAR_STATE_ENUM,
    ),
    WattpilotSensorEntityDescription(
        key="inverter",
        charger_key="cci",
        translation_key="inverter",
        entity_category=EntityCategory.DIAGNOSTIC,
        namespace_value="label",
        html_unescape=True,
        namespace_attributes=(
            "paired",
            "model",
            "commonName",
            "ip",
            "connected",
            "status",
            "message",
        ),
    ),
    WattpilotSensorEntityDescription(
        key="wifi_connection",
        charger_key="ccw",
        translation_key="wifi_connection",
        entity_category=EntityCategory.DIAGNOSTIC,
        namespace_value="ssid",
        namespace_attributes=("ip", "netmask", "gw", "channel", "bssid"),
    ),
    WattpilotSensorEntityDescription(
        key="cable_unlock",
        charger_key="cus",
        translation_key="cable_unlock",
        entity_category=EntityCategory.DIAGNOSTIC,
        enum=_LOCK_FEEDBACK_ENUM,
    ),
    WattpilotSensorEntityDescription(
        key="internal_error",
        charger_key="err",
        translation_key="internal_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        enum=_INTERNAL_ERROR_ENUM,
    ),
    WattpilotSensorEntityDescription(
        key="total_energy",
        charger_key="eto",
        translation_key="total_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="Wh",
        suggested_unit_of_measurement="kWh",
        suggested_display_precision=2,
        clamp_non_negative=True,
        monotonic=True,
    ),
    WattpilotSensorEntityDescription(
        key="lock_feedback",
        charger_key="ffb",
        translation_key="lock_feedback",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        enum=_LOCK_FEEDBACK_ENUM,
    ),
    WattpilotSensorEntityDescription(
        key="effective_lock_setting",
        charger_key="lck",
        translation_key="effective_lock_setting",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        enum={0: "Normal", 1: "AutoUnlock", 2: "AlwaysLock", 3: "ForceUnlock"},
    ),
    WattpilotSensorEntityDescription(
        key="local_time",
        charger_key="loc",
        translation_key="local_time",
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    WattpilotSensorEntityDescription(
        key="charging_reason",
        charger_key="modelStatus",
        translation_key="charging_reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        enum=_CHARGING_REASON_ENUM,
    ),
    WattpilotSensorEntityDescription(
        key="charging_power",
        charger_key="nrg",
        translation_key="charging_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="W",
        value_index=11,
        index_attributes=_NRG_ATTRIBUTES,
    ),
    WattpilotSensorEntityDescription(
        key="webserver_queue",
        charger_key="qsw",
        translation_key="webserver_queue",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        firmware="<=40.7",
    ),
    WattpilotSensorEntityDescription(
        key="reboot_counter",
        charger_key="rbc",
        translation_key="reboot_counter",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    WattpilotSensorEntityDescription(
        key="reboot_timer",
        charger_key="rbt",
        translation_key="reboot_timer",
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="ms",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    WattpilotSensorEntityDescription(
        key="wifi_signal",
        charger_key="rssi",
        translation_key="wifi_signal",
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    WattpilotSensorEntityDescription(
        key="charger_temp",
        charger_key="tma",
        translation_key="charger_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement="°C",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_index=0,
    ),
    WattpilotSensorEntityDescription(
        key="id_chip_current",
        charger_key="trx",
        translation_key="id_chip_current",
        entity_registry_enabled_default=False,
        enum=_ID_CHIP_ENUM,
    ),
    WattpilotSensorEntityDescription(
        key="http_clients",
        charger_key="wcch",
        translation_key="http_clients",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        firmware="<=40.7",
    ),
    WattpilotSensorEntityDescription(
        key="websocket_clients",
        charger_key="wccw",
        translation_key="websocket_clients",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        firmware="<=40.7",
    ),
    WattpilotSensorEntityDescription(
        key="session_energy",
        charger_key="wh",
        translation_key="session_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="Wh",
        suggested_unit_of_measurement="kWh",
        suggested_display_precision=2,
        clamp_non_negative=True,
        # Measured on a live charger (firmware 42.5, 1200s session log): wh
        # does NOT reset when the car unplugs (stayed at 847.86 Wh with the
        # cable out) -- it resets at the start of the *next* session instead
        # (0.0 -> -0.0068 observed right as charging began). The same log
        # also showed small backward steps of about -0.007 Wh mid-session
        # (measurement noise, not a reset). NOT plain `monotonic`: that would
        # rehide the genuine reset (fork bug 2026.6.2). reset_tolerant_monotonic
        # tells noise and resets apart by relative size instead.
        reset_tolerant_monotonic=True,
    ),
    WattpilotSensorEntityDescription(
        key="wifi_state",
        charger_key="wst",
        translation_key="wifi_state",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        enum=_WIFI_STATE_ENUM,
    ),
    _energy_split_sensor("whs", "session_energy_solar"),
    _energy_split_sensor("whb", "session_energy_battery"),
    _energy_split_sensor("whg", "session_energy_grid"),
    _energy_split_sensor("who", "session_energy_other"),
    WattpilotSensorEntityDescription(
        key="charging_allowed",
        charger_key="alw",
        translation_key="charging_allowed",
        # ponytail: a plain string state, because the integration has no
        # binary_sensor platform. Add one (and move this there) once a second
        # read-only boolean earns it -- "adi", "cpe" and "fsp" are candidates.
        enum={True: "Allowed", False: "Blocked"},
    ),
    WattpilotSensorEntityDescription(
        key="allowed_current",
        charger_key="acu",
        translation_key="allowed_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="A",
        # Null whenever the charger offers the car nothing, idle included, so
        # this reads unknown rather than 0 A most of the time. That is the
        # point of it: "amp" is what the user asked for, this is what the car
        # is actually being offered right now.
    ),
    WattpilotSensorEntityDescription(
        key="average_charging_power",
        charger_key="tpa",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="W",
        translation_key="average_charging_power",
        suggested_display_precision=0,
        # The charger's own 30-second average of total power. Steadier than
        # the instantaneous value in "nrg", which is what threshold
        # automations want.
    ),
    WattpilotSensorEntityDescription(
        key="grid_frequency",
        charger_key="fhz",
        translation_key="grid_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="Hz",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Off by default: it changed in 697 of 1200 pushes on the reference
        # device, so it is pure recorder load unless someone is watching grid
        # quality. The vendor documents 0 Hz as "unknown"; that value is
        # passed through unchanged, having never been observed.
        entity_registry_enabled_default=False,
    ),
    WattpilotSensorEntityDescription(
        key="phases_in_use",
        charger_key="pnp",
        translation_key="phases_in_use",
        state_class=SensorStateClass.MEASUREMENT,
        # Not a live count of energised phases: measured on firmware 42.5,
        # it follows the charger's phase *switch* (1 -> 3 about 24 s after
        # charging starts, and it stayed at 3 with zero current for the rest
        # of the session). Read it as "how many phases is the charger set to
        # use", which is what phase-switch automations need.
    ),
)


# ---------------------------------------------------------------------------
# Switch descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class WattpilotSwitchEntityDescription(
    WattpilotDescriptionMixin, SwitchEntityDescription
):
    """Switch description; invert flips wire value vs. displayed state."""

    invert: bool = False


# invert=True on "bac" and "nmo" is pinned against the fork's runtime
# ChargerSwitch (state read + write) at commit 1decee7, not just its
# description table -- both flip the displayed state AND the value written
# back to the charger. Getting either backwards silently inverts a
# safety-relevant setting (button lock / ground check).
SWITCH_DESCRIPTIONS: tuple[WattpilotSwitchEntityDescription, ...] = (
    WattpilotSwitchEntityDescription(
        key="lock_level_selection",
        charger_key="bac",
        translation_key="lock_level_selection",
        invert=True,
        firmware="<38.5",
    ),
    WattpilotSwitchEntityDescription(
        key="boost",
        charger_key="ebe",
        translation_key="boost",
        firmware=">=41.7",
    ),
    WattpilotSwitchEntityDescription(
        key="charge_pause",
        charger_key="fap",
        translation_key="charge_pause",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotSwitchEntityDescription(
        key="remain_in_eco_mode",
        charger_key="fre",
        translation_key="remain_in_eco_mode",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotSwitchEntityDescription(
        key="lumina_strom_awattar",
        charger_key="ful",
        translation_key="lumina_strom_awattar",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotSwitchEntityDescription(
        key="pv_surplus",
        charger_key="fup",
        translation_key="pv_surplus",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotSwitchEntityDescription(
        key="load_balancing",
        charger_key="loe",
        translation_key="load_balancing",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotSwitchEntityDescription(
        key="led_energy_saving",
        charger_key="lse",
        translation_key="led_energy_saving",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotSwitchEntityDescription(
        key="ground_check",
        charger_key="nmo",
        translation_key="ground_check",
        entity_category=EntityCategory.CONFIG,
        invert=True,
    ),
    WattpilotSwitchEntityDescription(
        key="discharge_pv_battery",
        charger_key="pdte",
        translation_key="discharge_pv_battery",
        entity_category=EntityCategory.CONFIG,
        firmware=">=41.7",
    ),
    WattpilotSwitchEntityDescription(
        key="simulate_unplugging",
        charger_key="su",
        translation_key="simulate_unplugging",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotSwitchEntityDescription(
        key="network_time_protocol",
        charger_key="tse",
        translation_key="network_time_protocol",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotSwitchEntityDescription(
        key="unlock_power_outage",
        charger_key="upo",
        translation_key="unlock_power_outage",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotSwitchEntityDescription(
        key="auto_disable_hotspot",
        charger_key="wda",
        translation_key="auto_disable_hotspot",
        entity_category=EntityCategory.CONFIG,
    ),
)


# ---------------------------------------------------------------------------
# Number descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class WattpilotNumberEntityDescription(
    WattpilotDescriptionMixin, NumberEntityDescription
):
    """Number description; set_as_int coerces writes to int."""

    set_as_int: bool = False


# Bounds/mode/set_as_int verified against the fork's NUMBER_DESCRIPTIONS at
# pinned commit 1decee7 (AST-level diff, not transcription): all 13 entries
# match on native_min_value/native_max_value/native_step/mode/device_class/
# unit/variant/firmware/entity_category/enabled_default. The fork's own
# runtime never reads its equivalent per-entry type hint (set_type is
# assigned to an attribute and never consulted again); it forwards the raw
# float to the vendor client, which coerces by its own property schema. Our
# FakeWattpilot test double doesn't replicate that library-side coercion, so
# set_as_int does the coercion explicitly here -- same end result, matching
# the fork's set_type="int" flags 1:1.
NUMBER_DESCRIPTIONS: tuple[WattpilotNumberEntityDescription, ...] = (
    WattpilotNumberEntityDescription(
        key="max_charging_current_11kw",
        charger_key="amp",
        set_as_int=True,
        translation_key="max_charging_current",
        native_min_value=6.0,
        native_max_value=16.0,
        native_step=1.0,
        mode=NumberMode.SLIDER,
        device_class=NumberDeviceClass.CURRENT,
        native_unit_of_measurement="A",
        variant=VARIANT_11,
    ),
    WattpilotNumberEntityDescription(
        key="max_charging_current_22kw",
        charger_key="amp",
        uid="amp_22kw",
        set_as_int=True,
        translation_key="max_charging_current",
        native_min_value=6.0,
        native_max_value=32.0,
        native_step=1.0,
        mode=NumberMode.SLIDER,
        device_class=NumberDeviceClass.CURRENT,
        native_unit_of_measurement="A",
        variant=VARIANT_22,
    ),
    WattpilotNumberEntityDescription(
        key="max_price",
        charger_key="awp",
        translation_key="max_price",
        native_min_value=-99999,
        native_max_value=999999,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        # No MONETARY device class: it requires an ISO 4217 currency code and
        # "ct" is not one, so Home Assistant would be told a currency that
        # does not exist (audit VA-15). Converting instead is not available
        # either -- "awc" selects the market, so the currency is not fixed at
        # description time. The device stores this in cent (vendor schema:
        # "awattarMaxPrice in ct") and we pass it through unscaled; labelling
        # it EUR overstated every price 100x, reads and writes alike.
        # Deliberate deviation from the fork; see the exceptions in
        # tests/parity.py.
        native_unit_of_measurement="ct",
    ),
    WattpilotNumberEntityDescription(
        key="boost_discharges_until",
        charger_key="ebt",
        set_as_int=True,
        translation_key="boost_discharges_until",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        device_class=NumberDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        firmware=">=41.7",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotNumberEntityDescription(
        key="pv_battery_threshold",
        charger_key="fam",
        set_as_int=True,
        translation_key="pv_battery_threshold",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        device_class=NumberDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotNumberEntityDescription(
        key="min_charging_time",
        charger_key="fmt",
        set_as_int=True,
        translation_key="min_charging_time",
        device_class=NumberDeviceClass.DURATION,
        native_min_value=60000,
        native_max_value=3600000,
        native_step=60000,
        native_unit_of_measurement="ms",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotNumberEntityDescription(
        key="ohmpilot_threshold",
        charger_key="fot",
        set_as_int=True,
        translation_key="ohmpilot_threshold",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement="°C",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotNumberEntityDescription(
        key="start_charging_at",
        charger_key="fst",
        translation_key="start_charging_at",
        native_min_value=0,
        native_max_value=999999,
        native_step=1,
        native_unit_of_measurement="W",
        device_class=NumberDeviceClass.POWER,
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotNumberEntityDescription(
        key="next_trip_charging",
        charger_key="fte",
        translation_key="next_trip_charging",
        native_min_value=0,
        native_max_value=999999,
        native_step=10,
        device_class=NumberDeviceClass.ENERGY,
        native_unit_of_measurement="Wh",
    ),
    WattpilotNumberEntityDescription(
        key="phase_switch_delay",
        charger_key="mpwst",
        set_as_int=True,
        translation_key="phase_switch_delay",
        native_min_value=0,
        native_max_value=99999999,
        native_unit_of_measurement="ms",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotNumberEntityDescription(
        key="phase_switch_interval",
        charger_key="mptwt",
        set_as_int=True,
        translation_key="phase_switch_interval",
        native_min_value=0,
        native_max_value=99999999,
        native_unit_of_measurement="ms",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotNumberEntityDescription(
        key="pv_battery_discharges_until",
        charger_key="pdt",
        set_as_int=True,
        translation_key="pv_battery_discharges_until",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        device_class=NumberDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        firmware=">=41.7",
        entity_category=EntityCategory.CONFIG,
    ),
    WattpilotNumberEntityDescription(
        key="three_phase_power_level",
        charger_key="spl3",
        set_as_int=True,
        translation_key="three_phase_power_level",
        native_min_value=0,
        native_max_value=999999,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement="W",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.CONFIG,
    ),
)


# ---------------------------------------------------------------------------
# Select descriptions
# ---------------------------------------------------------------------------

# Verified against the fork's SELECT_DESCRIPTIONS at pinned commit 1decee7
# (AST-level diff, not transcription): all 8 entries match on
# translation_key/firmware/variant/entity_category and on every wire-value ->
# label pair, including all 53 _AWATTAR_COUNTRIES entries.
_AWATTAR_COUNTRIES: dict[Any, str] = {
    0: "Austria",
    10043: "Belgium",
    10016: "Bulgaria",
    10003: "Croatia",
    10019: "Czechia",
    10024: "Denmark DK1",
    10036: "Denmark DK2",
    10055: "Denmark Tibber DK1",
    10056: "Denmark Tibber DK2",
    100055: "Denmark Tibber DK1 (alias 100055)",
    100056: "Denmark Tibber DK2 (alias 100056)",
    10020: "Estonia",
    10004: "Finland",
    10015: "France",
    1: "Germany",
    10006: "Greece",
    10042: "Hungary",
    10017: "Italy Calabria",
    10031: "Italy Centre-North",
    10009: "Italy Centre-South",
    10034: "Italy North",
    10023: "Italy SacoAC",
    10001: "Italy SacoDC",
    10029: "Italy Sardinia",
    10005: "Italy Sicily",
    10041: "Italy South",
    10010: "Latvia",
    10030: "Lithuania",
    10012: "Montenegro",
    10038: "Netherlands",
    10057: "Norway Elspot1",
    10058: "Norway Elspot2",
    10059: "Norway Elspot3",
    10060: "Norway Elspot4",
    10061: "Norway Elspot5",
    10021: "Norway NO2NSL",
    10037: "Poland",
    10027: "Portugal",
    10044: "Romania",
    10039: "Serbia",
    10040: "Slovakia",
    10025: "Slovenia",
    10018: "Spain",
    10062: "Sweden Elspot1",
    10063: "Sweden Elspot2",
    10064: "Sweden Elspot3",
    10065: "Sweden Elspot4",
    10045: "Sweden Tibber Elspot1",
    10046: "Sweden Tibber Elspot2",
    10047: "Sweden Tibber Elspot3",
    10048: "Sweden Tibber Elspot4",
    10035: "Switzerland",
    10002: "Ukraine",
}


@dataclass(frozen=True, kw_only=True)
class WattpilotSelectEntityDescription(
    WattpilotDescriptionMixin, SelectEntityDescription
):
    """Select description: wire value -> display label."""

    # kw_only dataclass: a required field after defaulted mixin fields is
    # fine -- keyword-only fields carry no positional ordering constraint.
    select_options: dict[Any, str]


SELECT_DESCRIPTIONS: tuple[WattpilotSelectEntityDescription, ...] = (
    WattpilotSelectEntityDescription(
        key="access_control",
        charger_key="acs",
        translation_key="access_control",
        entity_category=EntityCategory.CONFIG,
        select_options={0: "Open", 1: "Authentication Required"},
    ),
    WattpilotSelectEntityDescription(
        key="awattar_country",
        charger_key="awc",
        translation_key="awattar_country",
        entity_category=EntityCategory.CONFIG,
        select_options=_AWATTAR_COUNTRIES,
    ),
    WattpilotSelectEntityDescription(
        key="lock_level_selection",
        charger_key="bac",
        translation_key="lock_level_selection",
        firmware=">=38.5",
        select_options={
            0: "Always locked",
            1: "Locked when car is connected",
            3: "Never locked",
        },
    ),
    WattpilotSelectEntityDescription(
        key="boost_type",
        charger_key="ebo",
        translation_key="boost_type",
        firmware=">=41.7",
        entity_category=EntityCategory.CONFIG,
        select_options={
            True: "One-Time",
            False: "Repeat for as long as vehicle is plugged in",
        },
    ),
    WattpilotSelectEntityDescription(
        key="charging_mode",
        charger_key="lmo",
        translation_key="charging_mode",
        select_options={3: "Default", 4: "Eco", 5: "Next Trip"},
    ),
    WattpilotSelectEntityDescription(
        key="phase_switch",
        charger_key="psm",
        translation_key="phase_switch",
        entity_category=EntityCategory.CONFIG,
        select_options={0: "Auto", 1: "1 Phase", 2: "3 Phases"},
    ),
    WattpilotSelectEntityDescription(
        key="daylight_saving",
        charger_key="tds",
        translation_key="daylight_saving",
        entity_category=EntityCategory.CONFIG,
        select_options={
            0: "None",
            1: "EuropeanSummerTime",
            2: "UsDaylightTime",
            3: "AUEasternDaylightTime",
        },
    ),
    WattpilotSelectEntityDescription(
        key="cable_unlock",
        charger_key="ust",
        translation_key="cable_unlock_select",
        entity_category=EntityCategory.CONFIG,
        select_options={0: "Normal", 1: "AutoUnlock", 2: "AlwaysLock"},
    ),
    # mk-maddin parity (car profile select, not in the deysel fork). Present
    # on firmware 42.5: device_properties.json's "ct" is a real, anonymized
    # probe value -- scripts/anonymize_probe.py replaces the owner's actual
    # vehicle model with "default" for privacy, it does not drop the key.
    # Option labels verified against mk-maddin/wattpilot-HA's select.yaml at
    # pinned commit 8ea4782 (its "ct" entry); two labels differ from a plain
    # transcription and were corrected to match that source exactly:
    # "citroenCZero" -> "Citroen c zero" (not "Citroen C-Zero") and
    # "cupraBornAlternative" -> "Cupra Born Alternativ", no trailing "e"
    # (upstream's own spelling, kept verbatim for parity).
    WattpilotSelectEntityDescription(
        key="car_profile",
        charger_key="ct",
        translation_key="car_profile",
        entity_category=EntityCategory.CONFIG,
        select_options={
            "default": "Standard",
            "MitsubishiImiev": "Mitsubishi iMiev",
            "SkodaEnyaq": "Skoda Enyaq",
            "citroenCZero": "Citroen c zero",
            "cupraBornAlternative": "Cupra Born Alternativ",
            "cupraBornStandard": "Cupra Born Standard",
            "daciaSpring": "Dacia Spring",
            "ecorsa": "Opel Corsa-e",
            "fordExplorer": "Ford Explorer",
            "kiaSoul": "Kia Soul",
            "mercedes": "Mercedes",
            "peugeotIon": "Peugeot iOn",
            "porscheTaycan": "Porsche Taycan",
            "renaultZoe": "Renault Zoe/Twingo",
            "ssangyong": "Ssangyong",
            "vwID3_2": "VW ID (SW <3.2)",
            "vwID3_4": "VW ID (SW 3.2-4.1)",
            "vwID5": "VW ID (SW 5.x)",
        },
    ),
)


# ---------------------------------------------------------------------------
# Button descriptions
# ---------------------------------------------------------------------------


# Verified against the fork's BUTTON_DESCRIPTIONS at pinned commit 1decee7:
# all 5 entries match on uid/charger_key/device_class/enabled_default and on
# every press_value (frc 0/1/2, rst 1, trx 0). All five set requires_property
# because their charger keys are write-only trigger commands, never present
# in a status snapshot -- without that flag the presence filter in
# filter_supported() would silently delete every button.
@dataclass(frozen=True, kw_only=True)
class WattpilotButtonEntityDescription(
    WattpilotDescriptionMixin, ButtonEntityDescription
):
    """Button description: pressing writes press_value to charger_key."""

    press_value: Any = None


BUTTON_DESCRIPTIONS: tuple[WattpilotButtonEntityDescription, ...] = (
    WattpilotButtonEntityDescription(
        key="start_charging",
        charger_key="frc",
        uid="frc0",
        requires_property=False,
        translation_key="start_charging",
        press_value=0,
    ),
    WattpilotButtonEntityDescription(
        key="stop_charging",
        charger_key="frc",
        uid="frc1",
        requires_property=False,
        translation_key="stop_charging",
        press_value=1,
    ),
    WattpilotButtonEntityDescription(
        key="start_charging_force",
        charger_key="frc",
        uid="frc2",
        requires_property=False,
        translation_key="start_charging_force",
        press_value=2,
    ),
    WattpilotButtonEntityDescription(
        key="restart",
        charger_key="rst",
        requires_property=False,
        translation_key="restart",
        device_class=ButtonDeviceClass.RESTART,
        press_value=1,
    ),
    WattpilotButtonEntityDescription(
        key="authenticate",
        charger_key="trx",
        requires_property=False,
        translation_key="authenticate",
        entity_registry_enabled_default=False,
        press_value=0,
    ),
)


# ---------------------------------------------------------------------------
# Time descriptions
# ---------------------------------------------------------------------------


# Verified against the fork's TIME_DESCRIPTIONS at pinned commit 1decee7: the
# single entry matches on uid/charger_key, and the seconds-since-midnight ->
# datetime.time conversion mirrors the fork's runtime ChargerTime exactly.
# Writes go through hub.async_set_next_trip (not a plain property write): the
# vendor client applies a daylight-saving correction based on the charger's
# "tds" property before sending "ftt".
@dataclass(frozen=True, kw_only=True)
class WattpilotTimeEntityDescription(WattpilotDescriptionMixin, TimeEntityDescription):
    """Time description."""


TIME_DESCRIPTIONS: tuple[WattpilotTimeEntityDescription, ...] = (
    WattpilotTimeEntityDescription(
        key="next_trip_time",
        charger_key="ftt",
        translation_key="next_trip_time",
    ),
)


# ---------------------------------------------------------------------------
# Update descriptions
# ---------------------------------------------------------------------------


# Verified against the fork's UPDATE_DESCRIPTIONS at pinned commit 1decee7:
# the single entry matches on uid/charger_key/device_class/entity_category.
# Ordering is NOT guaranteed here: our client's `available_firmware_versions`
# (wattpilot-api==1.4.0, see site-packages/wattpilot_api/client.py) is an
# unsorted passthrough of the raw "onv" property, and its own
# install_firmware_update() picks index 0 when no version is given -- that
# only proves the vendor's default install matches its own display order,
# not that the charger emits versions newest-first on the wire. update.py
# sorts the raw entries with packaging.version.Version before picking
# "latest" (always keeping the original wire string, never a re-serialized
# one), the same defensive approach the fork's own regex+Version sort took.
@dataclass(frozen=True, kw_only=True)
class WattpilotUpdateEntityDescription(
    WattpilotDescriptionMixin, UpdateEntityDescription
):
    """Update description; installed version comes from installed_key."""

    installed_key: str = "fwv"


UPDATE_DESCRIPTIONS: tuple[WattpilotUpdateEntityDescription, ...] = (
    WattpilotUpdateEntityDescription(
        key="firmware_update",
        charger_key="onv",
        translation_key="firmware_update",
        device_class=UpdateDeviceClass.FIRMWARE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

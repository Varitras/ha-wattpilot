"""Sensor conversions against real firmware 42.5 data + fork parity."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory

from custom_components.wattpilot import sensor as sensor_platform
from custom_components.wattpilot.descriptions import (
    SENSOR_DESCRIPTIONS,
    filter_supported,
)
from custom_components.wattpilot.hub import WattpilotHub
from custom_components.wattpilot.readings import describe_shape
from custom_components.wattpilot.sensor import WattpilotSensor

from .parity import assert_platform_parity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"
ENERGY_SPLIT_UIDS = {"whs", "whb", "whg", "who"}
SOURCE_POWER_UIDS = ("ws", "wb", "wg", "wo")
# Which source each one is named after, so a swap cannot pass (F31-05).
SOURCE_POWER_NAMES = dict(
    zip(SOURCE_POWER_UIDS, ("solar", "battery", "grid", "other_sources"), strict=True)
)
# uid, nrg slot and unit; N carries no current.
PHASE_READINGS = [
    *(
        (f"{phase}_voltage", slot, "V")
        for slot, phase in enumerate(("l1", "l2", "l3", "n"))
    ),
    *(
        (f"{phase}_current", slot, "A")
        for slot, phase in enumerate(("l1", "l2", "l3"), 4)
    ),
    *(
        (f"{phase}_power", slot, "W")
        for slot, phase in enumerate(("l1", "l2", "l3", "n"), 7)
    ),
    *(
        (f"{phase}_power_factor", slot, "%")
        for slot, phase in enumerate(("l1", "l2", "l3", "n"), 12)
    ),
]

# Additions this project chose on top of the frozen fork uids: the energy
# split, the phase count (pnp), charging permission and current (alw, acu),
# average power (tpa), grid frequency (fhz), the electricity price (awcp),
# the charging duration (cdi) and the readings per phase.
# See tests/parity.py before widening this to make a test pass.
EXTRA_SENSOR_UIDS = ENERGY_SPLIT_UIDS | {
    "pnp",
    "alw",
    "acu",
    "tpa",
    "fhz",
    "awcp",
    "cdi",
    # The ID chips of a charger without a `cards` list (Wattpilot Flex).
    *(f"chip_{i}" for i in range(10)),
    # The readings per phase, beside Charging power's attributes.
    *(uid for uid, _, _ in PHASE_READINGS),
    # The current limits and the temperature limit among them.
    "cll",
    "amt",
    # Charging power per source, which a Wattpilot Flex reports.
    *SOURCE_POWER_UIDS,
}


def by_uid(uid: str) -> Any:
    return next(d for d in SENSOR_DESCRIPTIONS if d.uid_suffix == uid)


async def make_sensor(
    hass: HomeAssistant, charger: FakeWattpilot, uid: str
) -> WattpilotSensor:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()  # required: pushes route through the hub's dispatch
    # The class setup picks, so a special sensor is tested as it runs.
    description = by_uid(uid)
    sensor = sensor_platform._sensor_class(description)(hub, ENTRY_ID, description)
    sensor.hass = hass
    sensor.entity_id = f"sensor.test_{uid}"
    await sensor.async_added_to_hass()
    return sensor


def test_sensor_parity_with_fork() -> None:
    assert_platform_parity("sensor", SENSOR_DESCRIPTIONS, EXTRA_SENSOR_UIDS)


def test_energy_split_sensors_are_created_without_being_asked() -> None:
    """They started opt-in, as reverse-engineered extras. They are the answer
    to "how much of this session came from the sun", which is the question
    this integration exists for, so they ship enabled."""
    for uid in ENERGY_SPLIT_UIDS:
        assert by_uid(uid).entity_registry_enabled_default is True


def test_firmware_filter_drops_legacy_sensors(
    device_properties: dict[str, Any],
) -> None:
    # Exclusion of qsw/wcch/wccw is over-determined: (a) they're absent from the
    # fixture anyway, so the presence check alone would exclude them; (b) the
    # firmware-constraint mechanism itself is tested load-bearingly in
    # tests/test_descriptions.py::test_filter_supported_against_device, which
    # uses a key that IS present in the fixture.
    kept = filter_supported(
        SENSOR_DESCRIPTIONS,
        firmware="42.5",
        variant=11,
        properties=device_properties,
    )
    suffixes = {d.uid_suffix for d in kept}
    assert {"wh", "eto", "nrg", "car", "cards_0"} <= suffixes
    assert {"qsw", "wcch", "wccw"} & suffixes == set()


async def test_setup_filters_with_this_device_firmware_and_variant(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The platform must feed the real device's firmware and variant into the
    filter. Every sensor shipped today passes both gates, so a hardcoded None
    would go unnoticed until the first gated sensor is added -- this pins the
    wiring with a description that is gated on purpose."""
    hub = WattpilotHub(hass, ENTRY_ID, fake_charger)  # type: ignore[arg-type]
    await hub.async_connect()
    gated = replace(by_uid("wh"), key="gated", firmware=">=41.7", variant="11")
    added: list[WattpilotSensor] = []
    entry = SimpleNamespace(entry_id=ENTRY_ID, runtime_data=hub)
    with patch.object(sensor_platform, "SENSOR_DESCRIPTIONS", [gated]):
        await sensor_platform.async_setup_entry(
            hass,
            entry,  # type: ignore[arg-type]
            added.extend,  # type: ignore[arg-type]
        )
    assert len(added) == 1
    # Same reason as in test_hub: the entry id namespaces the push signals.
    assert added[0]._entry_id == ENTRY_ID


async def test_enum_sensor_maps_value(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["car"] = 2
    sensor = await make_sensor(hass, fake_charger, "car")
    assert sensor.native_value == "Charging"


async def test_enum_sensor_passes_unmapped_value_through(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A future firmware state the enum does not know yet must still be
    visible, not silently become None."""
    fake_charger._properties["car"] = 2
    sensor = await make_sensor(hass, fake_charger, "car")
    fake_charger.push("car", 99)
    await hass.async_block_till_done()
    assert sensor.native_value == 99


async def test_nrg_array_maps_power_and_attributes(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    nrg = [235.6, 234.0, 234.0, 1.5, 0, 0, 0, 0, 0, 0, 0, 4200, 0, 0, 0, 0]
    fake_charger._properties["nrg"] = nrg
    sensor = await make_sensor(hass, fake_charger, "nrg")
    assert sensor.native_value == 4200
    assert sensor.extra_state_attributes["L1_Voltage"] == 235.6
    assert sensor.extra_state_attributes["TotalPower"] == 4200


async def test_session_energy_reset_not_suppressed(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Regression: fork 2026.6.2 froze `wh` at the session max until restart.

    A reset to 0 when the car unplugs is correct TOTAL_INCREASING behavior
    and must reach HA unchanged."""
    fake_charger._properties["wh"] = 22000.0
    sensor = await make_sensor(hass, fake_charger, "wh")
    assert sensor.native_value == 22000.0
    fake_charger.push("wh", 0.0)
    await hass.async_block_till_done()
    assert sensor.native_value == 0.0


async def test_session_energy_noise_suppressed(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Measured on a live charger (firmware 42.5): a -0.007 Wh backward step
    mid-session is measurement noise, not a reset, and must not reach HA."""
    fake_charger._properties["wh"] = 603.5895424
    sensor = await make_sensor(hass, fake_charger, "wh")
    assert sensor.native_value == 603.59
    fake_charger.push("wh", 603.5828552)
    await hass.async_block_till_done()
    assert sensor.native_value == 603.59


async def test_session_energy_noise_suppressed_second_dip(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Same live-charger log, the second of the two observed -0.007 Wh dips."""
    fake_charger._properties["wh"] = 847.8703793
    sensor = await make_sensor(hass, fake_charger, "wh")
    assert sensor.native_value == 847.87
    fake_charger.push("wh", 847.8634398)
    await hass.async_block_till_done()
    assert sensor.native_value == 847.87


async def test_session_energy_real_reset_not_suppressed(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A genuine reset (car unplugged, next session starts) must still reach
    HA even with the noise-suppression threshold in place."""
    fake_charger._properties["wh"] = 847.87
    sensor = await make_sensor(hass, fake_charger, "wh")
    fake_charger.push("wh", 0.0)
    await hass.async_block_till_done()
    assert sensor.native_value == 0


async def test_session_energy_noise_clamped(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["wh"] = -0.003  # fork issue #62
    sensor = await make_sensor(hass, fake_charger, "wh")
    assert sensor.native_value == 0


async def test_session_energy_noise_suppressed_exactly_at_threshold(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The 90 % mark itself counts as noise, not as a reset -- the same way
    Home Assistant's own recorder draws the line (`fstate < 0.9 * previous`)."""
    fake_charger._properties["wh"] = 1000.0
    sensor = await make_sensor(hass, fake_charger, "wh")
    fake_charger.push("wh", 900.0)
    await hass.async_block_till_done()
    assert sensor.native_value == 1000.0


async def test_plain_energy_sensor_follows_a_decrease(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Only the two guarded sensors hold values back; the energy splits have
    neither guard and must report whatever the charger reports."""
    fake_charger._properties["whs"] = 100.0
    sensor = await make_sensor(hass, fake_charger, "whs")
    fake_charger.push("whs", 50.0)
    await hass.async_block_till_done()
    assert sensor.native_value == 50.0


async def test_plain_energy_sensor_is_not_rounded(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Rounding is only there to make the reset/noise decision match what HA
    displays. A sensor without that decision keeps the charger's full
    precision -- HA rounds for display on its own."""
    fake_charger._properties["whs"] = 123.456789
    sensor = await make_sensor(hass, fake_charger, "whs")
    assert sensor.native_value == 123.456789


async def test_total_energy_is_monotonic(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["eto"] = 2494780.0
    sensor = await make_sensor(hass, fake_charger, "eto")
    fake_charger.push("eto", 2494779.9)  # firmware float noise
    await hass.async_block_till_done()
    assert sensor.native_value == 2494780.0
    fake_charger.push("eto", 2494781.0)
    await hass.async_block_till_done()
    assert sensor.native_value == 2494781.0


async def test_unchanged_value_is_not_treated_as_a_decrease(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Equal is not less. The charger reports the same reading as int and as
    float interchangeably, so the type of what reaches HA is the only visible
    proof that the re-report was applied rather than suppressed."""
    fake_charger._properties["eto"] = 2494780
    sensor = await make_sensor(hass, fake_charger, "eto")
    assert isinstance(sensor.native_value, int)
    fake_charger.push("eto", 2494780.0)
    await hass.async_block_till_done()
    assert isinstance(sensor.native_value, float)


async def test_local_time_parses_charger_format(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["loc"] = "2026-08-10 23:27:03.866 +02:00"
    sensor = await make_sensor(hass, fake_charger, "loc")
    value = sensor.native_value
    assert value is not None
    assert value.tzinfo is not None
    assert value.hour == 23


async def test_local_time_parses_negative_utc_offset(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Same format, west of Greenwich: the space before the offset has to be
    collapsed for the minus sign too, not just for the plus sign."""
    fake_charger._properties["loc"] = "2026-08-10 23:27:03.866 -05:00"
    sensor = await make_sensor(hass, fake_charger, "loc")
    value = sensor.native_value
    assert value is not None
    assert value.utcoffset() == timedelta(hours=-5)


async def test_local_time_rejects_a_non_string_value(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A timestamp that is not text has no defensible reading; the sensor
    must clear itself rather than keep showing a stale time."""
    fake_charger._properties["loc"] = "2026-08-10 23:27:03.866 +02:00"
    sensor = await make_sensor(hass, fake_charger, "loc")
    assert sensor.native_value is not None
    fake_charger.push("loc", 1234567890)
    await hass.async_block_till_done()
    assert sensor.native_value is None


async def test_inverter_namespace_unescapes_label(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["cci"] = {
        "label": "Symo GEN24 &amp; Friends",
        "model": "Gen24",
        "connected": True,
    }
    sensor = await make_sensor(hass, fake_charger, "cci")
    assert sensor.native_value == "Symo GEN24 & Friends"
    assert sensor.extra_state_attributes["model"] == "Gen24"


async def test_inverter_namespace_reads_object_payloads(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The vendor client hands nested payloads over as objects, not dicts.
    Fields the object does not carry must read as None instead of raising."""
    fake_charger._properties["cci"] = SimpleNamespace(label="Symo", model="Gen24")
    sensor = await make_sensor(hass, fake_charger, "cci")
    assert sensor.native_value == "Symo"
    assert sensor.extra_state_attributes["model"] == "Gen24"
    assert sensor.extra_state_attributes["paired"] is None


async def test_card_sensor_reads_energy_and_name(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["cards"] = [
        {"name": "Card 0", "cardId": True, "energy": 1234.5}
    ]
    sensor = await make_sensor(hass, fake_charger, "cards_0")
    assert sensor.native_value == 1234.5
    assert sensor.extra_state_attributes == {"name": "Card 0", "cardId": True}
    # The index is what tells the four otherwise identically named ID-chip
    # sensors apart in the UI.
    assert sensor.translation_placeholders == {"index": "0"}


FLEX_CHIPS = {
    f"c{i}{field}": value
    for i in range(10)
    for field, value in (("e", 0), ("i", False), ("n", "n/a"))
}


async def test_a_flex_chip_reports_its_energy(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A Wattpilot Flex has no `cards` list; each chip has its own keys,
    c0e..c9e for the energy (go-e's key list, firmware 60.x: uint64, the
    successor of cards[].energy). All ten chip sensors were dropped."""
    fake_charger._properties.pop("cards", None)
    fake_charger._properties.update({**FLEX_CHIPS, "c1e": 10})
    sensor = await make_sensor(hass, fake_charger, "chip_1")
    assert sensor.native_value == 10
    assert sensor.translation_placeholders == {"index": "1"}


def test_each_charger_gets_the_chip_sensors_of_its_own_shape(
    device_properties: dict[str, Any],
) -> None:
    def chip_uids(properties: dict[str, Any]) -> set[str]:
        kept = filter_supported(
            SENSOR_DESCRIPTIONS, firmware="43.5", variant=11, properties=properties
        )
        return {d.uid_suffix for d in kept if d.translation_key == "id_chip"}

    flex = {k: v for k, v in device_properties.items() if k != "cards"} | FLEX_CHIPS
    assert chip_uids(device_properties) == {f"cards_{i}" for i in range(10)}
    assert chip_uids(flex) == {f"chip_{i}" for i in range(10)}


def test_the_log_names_a_shape_without_repeating_the_value() -> None:
    """What the warning is allowed to say about a payload it cannot use."""
    assert describe_shape([1, 2, 3]) == "list of 3"
    assert describe_shape((1,)) == "tuple of 1"
    assert describe_shape({"a": 1, "b": 2}) == "dict of 2"
    assert describe_shape("garbage") == "str"


async def test_a_missing_card_slot_leaks_no_names_into_the_log(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ten card sensors exist, and a charger may report fewer cards. Indexing
    past the end raised, and the generic handler logged the whole payload
    with %r -- card holder names included, in a log the diagnostics redaction
    never touches (audit A12-05). The absent slot is a normal state, not a
    fault worth a warning.
    """
    fake_charger._properties["cards"] = [
        {"name": "Alex Example", "cardId": True, "energy": 1.0},
        {"name": "Sam Sample", "cardId": True, "energy": 2.0},
    ]
    with caplog.at_level(logging.DEBUG):
        sensor = await make_sensor(hass, fake_charger, "cards_9")

    assert sensor.native_value is None
    # An empty dict, not None: Home Assistant reads these attributes, and a
    # None there is not the same thing as "this slot has nothing to show".
    assert sensor.extra_state_attributes == {}
    assert "Alex Example" not in caplog.text
    assert "Sam Sample" not in caplog.text
    assert "WARNING" not in caplog.text, "an absent card slot is not a fault"


async def test_the_first_card_slot_the_charger_lacks_is_already_empty(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The boundary itself: two cards means slot 2 is the first missing one.

    Off by one here and the sensor indexes past the end again, which is the
    exact path that put holder names into the log (audit A12-05).
    """
    fake_charger._properties["cards"] = [
        {"name": "Alex Example", "cardId": True, "energy": 1.0},
        {"name": "Sam Sample", "cardId": True, "energy": 2.0},
    ]
    with caplog.at_level(logging.DEBUG):
        sensor = await make_sensor(hass, fake_charger, "cards_2")

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}
    assert "Sam Sample" not in caplog.text
    assert "WARNING" not in caplog.text


async def test_non_card_sensor_has_no_index_placeholder(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    sensor = await make_sensor(hass, fake_charger, "car")
    assert sensor.translation_placeholders == {}


async def test_malformed_value_keeps_previous_state(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_charger._properties["nrg"] = [1] * 16
    sensor = await make_sensor(hass, fake_charger, "nrg")
    # As a real charger names it: the device is named after its host name.
    sensor.entity_id = "sensor.wattpilot_123456_charging_power"
    with caplog.at_level(logging.WARNING):
        fake_charger.push("nrg", "garbage")
        await hass.async_block_till_done()
    assert sensor.native_value == 1
    # The warning is the only trace a dropped push leaves; it has to name
    # which sensor, what shape arrived, which charger key and what went wrong.
    # The sensor by its key: the entity ID carries the serial (R1-02).
    # It deliberately does NOT carry the value, nor the exception's message,
    # which can quote the value too: nothing redacts this sink, and a card
    # list logged that way put holder names into it (audit A12-05).
    # Compared whole rather than as a substring: a substring check passes just
    # as happily when the message has grown extra text around it.
    assert any(
        record.getMessage() == "charging_power: unexpected str for nrg (IndexError)"
        for record in caplog.records
    )
    # Only our own sink: Home Assistant's dispatcher logs its signal payload
    # at DEBUG too, and that is not this integration's contract to keep.
    ours = [
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.wattpilot.sensor"
    ]
    assert not any("garbage" in message for message in ours)
    assert not any("123456" in message for message in ours)


async def test_the_temperature_is_the_hottest_reading(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["tma"] = [38.0, 40.5]
    sensor = await make_sensor(hass, fake_charger, "tma")
    assert sensor.native_value == 40.5
    assert sensor.extra_state_attributes == {"readings": [38.0, 40.5]}


async def test_a_flex_temperature_with_empty_slots_shows_the_hottest(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A Wattpilot Flex (FW 43.4/43.5) reports six slots, the first two
    null, and reading slot 0 left the sensor unknown for good. What each
    slot measures is documented nowhere, so the hottest reading is shown --
    the one that matters for derating -- with all of them as an attribute."""
    readings = [None, None, 34.75, 36.375, 36.875, 35]
    fake_charger._properties["tma"] = readings
    sensor = await make_sensor(hass, fake_charger, "tma")
    assert sensor.native_value == 36.875
    assert sensor.extra_state_attributes == {"readings": readings}


async def test_a_temperature_that_is_no_number_is_skipped(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """max() over a NaN is NaN (post-check PC6-06)."""
    fake_charger._properties["tma"] = [float("nan"), 40.0]
    sensor = await make_sensor(hass, fake_charger, "tma")
    assert sensor.native_value == 40.0


async def test_no_temperature_reading_is_unknown(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Not the last reading kept: a stale temperature reads as a real one."""
    fake_charger._properties["tma"] = [None, 40.0]
    sensor = await make_sensor(hass, fake_charger, "tma")
    fake_charger.push("tma", [None, None])
    assert sensor.native_value is None


def test_the_temperature_readings_stay_out_of_the_recorder() -> None:
    assert "readings" in WattpilotSensor._unrecorded_attributes


async def test_car_connected_reports_plug_state_not_charge_state(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """`car_connected` answers "is a car plugged in", not "what is it doing".

    Both sensors read the same "car" property, so mapping both through the
    charge-state labels made them literal duplicates. Every state that means
    a plugged-in cable -- charging (2), waiting for the car (3), complete
    (4) -- collapses to Connected here; car_state keeps the detail.
    """
    fake_charger._properties["car"] = 3
    sensor = await make_sensor(hass, fake_charger, "car_connected")
    assert sensor.native_value == "Connected"

    for value in (2, 4):
        fake_charger.push("car", value)
        await hass.async_block_till_done()
        assert sensor.native_value == "Connected"

    fake_charger.push("car", 1)
    await hass.async_block_till_done()
    assert sensor.native_value == "Disconnected"


async def test_car_state_keeps_the_detailed_labels(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The counterpart to the test above: the detail must survive somewhere."""
    fake_charger._properties["car"] = 3
    sensor = await make_sensor(hass, fake_charger, "car")
    assert sensor.native_value == "Wait Car"


# pha as measured: L1-L3 switched through to the car, then the supply phases.
IDLE = [False, False, False, True, True, True]
ONE_PHASE = [True, False, False, True, True, True]
THREE_PHASES = [True, True, True, True, True, True]


@pytest.mark.parametrize(
    ("pha", "phases"),
    # Only the first three are read, so three are enough.
    [(IDLE, 0), (ONE_PHASE, 1), (THREE_PHASES, 3), (THREE_PHASES[:3], 3)],
    ids=["idle", "one_phase", "three_phases", "car_phases_only"],
)
async def test_phases_in_use_counts_the_phases_switched_to_the_car(
    hass: HomeAssistant, fake_charger: FakeWattpilot, pha: list[bool], phases: int
) -> None:
    """pnp stayed at 1 through whole three-phase sessions, on a Home and on
    a Flex, and followed a switch about 30 s late; pha switches with the
    contactor. The unique ID stays the one pnp had."""
    fake_charger._properties["pnp"] = 1
    fake_charger._properties["pha"] = IDLE
    sensor = await make_sensor(hass, fake_charger, "pnp")
    fake_charger.push("pha", pha)
    await hass.async_block_till_done()

    assert sensor.native_value == phases


@pytest.mark.parametrize(
    "pha",
    [[True, True], [1, 1, 1, True, True, True], "TTT", None],
    ids=["too_short", "not_booleans", "text", "none"],
)
async def test_phases_in_use_is_unknown_for_a_value_of_another_shape(
    hass: HomeAssistant, fake_charger: FakeWattpilot, pha: object
) -> None:
    """Counting anything truthy would turn a malformed value into a phase count."""
    fake_charger._properties["pha"] = THREE_PHASES
    sensor = await make_sensor(hass, fake_charger, "pnp")
    fake_charger.push("pha", pha)
    await hass.async_block_till_done()

    assert sensor.native_value is None


def test_new_sensors_declare_their_property_and_unit() -> None:
    """The four readings added on top of the fork's set: alw, acu, tpa, fhz."""
    expected = {
        "alw": (None, None),
        "acu": (SensorDeviceClass.CURRENT, "A"),
        "tpa": (SensorDeviceClass.POWER, "W"),
        "fhz": (SensorDeviceClass.FREQUENCY, "Hz"),
    }
    for uid, (device_class, unit) in expected.items():
        description = by_uid(uid)
        assert description.charger_key == uid
        assert description.device_class == device_class
        assert description.native_unit_of_measurement == unit


async def test_charging_allowed_maps_the_boolean(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """`alw` is a bare boolean; unmapped it would render as True/False."""
    fake_charger._properties["alw"] = False
    sensor = await make_sensor(hass, fake_charger, "alw")
    assert sensor.native_value == "Blocked"

    fake_charger.push("alw", value=True)
    await hass.async_block_till_done()
    assert sensor.native_value == "Allowed"


async def test_allowed_current_is_unknown_while_no_car_charges(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """`acu` is null whenever the charger allows nothing -- idle included."""
    fake_charger._properties["acu"] = None
    sensor = await make_sensor(hass, fake_charger, "acu")
    assert sensor.native_value is None

    fake_charger.push("acu", 6)
    await hass.async_block_till_done()
    assert sensor.native_value == 6


async def test_a_null_start_value_is_applied_when_the_property_exists(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Audit VA-13: the initial read skipped None, treating "no value" and
    "the value None" as the same thing. For `trx` they are not -- None is the
    enum's own "No Transaction" -- so the entity sat at unknown until the
    charger happened to push the field again.
    """
    fake_charger._properties["trx"] = None
    sensor = await make_sensor(hass, fake_charger, "trx")
    assert sensor.native_value == "No Transaction"


PRICE_HOUR = {"start": 1786395600, "end": 1786399200, "marketprice": 15.144}


async def test_electricity_price_is_the_current_market_price(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties.update(
        {
            "awcp": PRICE_HOUR,
            "awpl": [
                PRICE_HOUR,
                {"start": 1786399200, "end": 1786402800, "marketprice": 13.92},
            ],
        }
    )
    sensor = await make_sensor(hass, fake_charger, "awcp")
    assert sensor.native_value == 15.144
    assert sensor.native_unit_of_measurement == "ct/kWh"
    prices = sensor.extra_state_attributes["prices"]
    assert [entry["price"] for entry in prices] == [15.144, 13.92]
    assert prices[1]["start"].timestamp() == 1786399200
    assert prices[1]["end"].timestamp() == 1786402800


async def test_a_flex_price_list_object_is_expanded(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """On a Wattpilot Flex `awpl` is one object: the prices, the start of
    the first and the interval. Its start and first price equal `awcp` in a
    Flex's diagnostics (FW 43.5), so it is the same list in another shape --
    and the attribute stayed empty."""
    fake_charger._properties.update(
        {
            "awcp": PRICE_HOUR,
            "awpl": {
                "marketprice": [15.144, 13.92],
                "start": 1786395600,
                "interval": 3600,
            },
        }
    )
    sensor = await make_sensor(hass, fake_charger, "awcp")
    prices = sensor.extra_state_attributes["prices"]
    assert [entry["price"] for entry in prices] == [15.144, 13.92]
    assert prices[1]["start"].timestamp() == 1786399200
    assert prices[1]["end"].timestamp() == 1786402800


@pytest.mark.parametrize(
    "schedule",
    [
        {"marketprice": [15.144], "start": 1786395600},
        {"marketprice": [15.144], "interval": 3600},
        {"marketprice": None, "start": 1786395600, "interval": 3600},
    ],
)
async def test_an_incomplete_price_schedule_gives_no_prices(
    hass: HomeAssistant, fake_charger: FakeWattpilot, schedule: dict[str, Any]
) -> None:
    fake_charger._properties.update({"awcp": PRICE_HOUR, "awpl": schedule})
    sensor = await make_sensor(hass, fake_charger, "awcp")
    assert sensor.extra_state_attributes == {"prices": []}


@pytest.mark.parametrize(
    "bad",
    [
        {"start": "n/a", "end": 1786399200, "marketprice": 9.0},
        {"start": 18446744073709551615, "end": 1786399200, "marketprice": 9.0},
        {"start": 1e13, "end": 1786399200, "marketprice": 9.0},  # past year 9999
    ],
    ids=["text", "out_of_range", "past_the_calendar"],
)
async def test_one_bad_price_costs_only_itself(
    hass: HomeAssistant, fake_charger: FakeWattpilot, bad: dict[str, Any]
) -> None:
    """A bad entry in the price list took the current price with it, and an
    out-of-range start escaped the bounded catch (reaudit RF-D-03)."""
    fake_charger._properties.update({"awcp": PRICE_HOUR, "awpl": [bad, PRICE_HOUR]})
    sensor = await make_sensor(hass, fake_charger, "awcp")

    assert sensor.native_value == PRICE_HOUR["marketprice"]
    assert [entry["price"] for entry in sensor.extra_state_attributes["prices"]] == [
        PRICE_HOUR["marketprice"]
    ]


@pytest.mark.parametrize("start", ["n/a", 10**400], ids=["text", "beyond_float"])
async def test_a_flex_schedule_with_a_bad_start_gives_no_prices(
    hass: HomeAssistant, fake_charger: FakeWattpilot, start: Any
) -> None:
    schedule = {"marketprice": [15.144], "start": start, "interval": 3600}
    fake_charger._properties.update({"awcp": PRICE_HOUR, "awpl": schedule})
    sensor = await make_sensor(hass, fake_charger, "awcp")

    assert sensor.native_value == PRICE_HOUR["marketprice"]
    assert sensor.extra_state_attributes == {"prices": []}


async def test_a_new_price_list_reaches_the_attribute(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The list is its own property; the current price does not change when
    the next day's prices arrive, so the list has to refresh the entity."""
    fake_charger._properties.update({"awcp": PRICE_HOUR, "awpl": [PRICE_HOUR]})
    sensor = await make_sensor(hass, fake_charger, "awcp")
    tomorrow = {"start": 1786482000, "end": 1786485600, "marketprice": 9.5}
    fake_charger.push("awpl", [PRICE_HOUR, tomorrow])
    assert len(sensor.extra_state_attributes["prices"]) == 2


async def test_no_tariff_no_price(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties.update({"awcp": None, "awpl": None})
    sensor = await make_sensor(hass, fake_charger, "awcp")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"prices": []}


def test_the_price_list_stays_out_of_the_recorder() -> None:
    assert "prices" in WattpilotSensor._unrecorded_attributes
    assert by_uid("awcp").entity_registry_enabled_default is False


def test_per_phase_readings_stay_out_of_the_recorder() -> None:
    """The per-phase readings differ in nearly every update while
    charging, so the recorder stored a new attribute row for nearly every
    state row. Their history is what the dedicated sensors are for."""
    indexed = {
        name
        for description in SENSOR_DESCRIPTIONS
        for name in (description.index_attributes or {})
    }
    assert indexed
    assert indexed <= WattpilotSensor._unrecorded_attributes


# cdi as recorded on firmware 42.5 on 2026-09-25, with the rbt beside it.
# While charging it holds the boot clock at the start, not a duration.
@pytest.mark.parametrize(
    ("cdi", "rbt", "minutes"),
    [
        ({"type": 0, "value": 2217901}, 19322632, 285),  # charging
        ({"type": 0, "value": -8301127}, 41170, 139),  # charging, after a reboot
        ({"type": 1, "value": 17465940}, 19683841, 291),  # finished
        ({"type": 1, "value": 0}, 14857, 0),  # after a power cut
        (None, 14857, None),
        ({"type": 7, "value": 1}, 14857, None),
        # json.loads makes NaN and infinity of what the charger could send.
        ({"type": 0, "value": 2217901}, float("inf"), None),
        ({"type": 1, "value": float("nan")}, 14857, None),
        # Each finite, their difference not (post-check PC6-01).
        ({"type": 0, "value": -1.7e308}, 1.7e308, None),
    ],
)
async def test_charging_duration_in_minutes(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    cdi: Any,
    rbt: float,
    minutes: int | None,
) -> None:
    fake_charger._properties.update({"cdi": cdi, "rbt": rbt})
    sensor = await make_sensor(hass, fake_charger, "cdi")
    assert sensor.native_value == minutes
    assert sensor.native_unit_of_measurement == "min"


async def test_a_running_charge_counts_up_with_the_boot_clock(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """cdi does not change while charging; only the clock beside it does."""
    fake_charger._properties.update(
        {"cdi": {"type": 0, "value": 2217901}, "rbt": 19322632}
    )
    sensor = await make_sensor(hass, fake_charger, "cdi")
    fake_charger.push("rbt", 19322632 + 10 * 60_000)
    assert sensor.native_value == 295


def test_the_charging_duration_is_created_without_being_asked() -> None:
    assert by_uid("cdi").entity_registry_enabled_default is True


async def test_only_a_price_companion_becomes_the_prices_attribute(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The boot clock beside cdi is a companion too; read as a price list it
    would put rbt into a "prices" attribute."""
    fake_charger._properties.update({"cdi": {"type": 1, "value": 60000}, "rbt": 14857})
    hub = WattpilotHub(hass, ENTRY_ID, fake_charger)  # type: ignore[arg-type]
    await hub.async_connect()
    sensor = WattpilotSensor(hub, ENTRY_ID, by_uid("cdi"))
    sensor.hass = hass
    sensor.entity_id = "sensor.test_cdi_plain"
    await sensor.async_added_to_hass()
    assert "prices" not in (sensor.extra_state_attributes or {})


def nrg_reading(total_power: float) -> list[float]:
    """An nrg reading, as on firmware 42.5, with *total_power* at slot 11."""
    nrg = [235.6, 233.7, 234.0, 1.5] + [0.0] * 12
    nrg[11] = total_power
    return nrg


async def _average_power_while_charging(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> WattpilotSensor:
    fake_charger._properties.update({"tpa": 4121.5, "nrg": nrg_reading(4100)})
    return await make_sensor(hass, fake_charger, "tpa")


async def test_average_power_is_zero_while_nothing_flows(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """tpa keeps the last session's average after charging -- recorded on
    firmware 42.5 with the car plugged in and every power slot at 0, and
    reported on a Flex -- so threshold automations kept firing for hours
    (audit A18-13). Nothing flowing when the entry starts is already idle."""
    fake_charger._properties.update({"tpa": 4121.5, "nrg": nrg_reading(0)})
    sensor = await make_sensor(hass, fake_charger, "tpa")

    assert sensor.native_value == 0


async def test_a_single_watt_counts_as_charging(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The charger reports exactly 0 W while idle; any power is a session."""
    fake_charger._properties.update({"tpa": 4121.5, "nrg": nrg_reading(1)})
    sensor = await make_sensor(hass, fake_charger, "tpa")

    assert sensor.native_value == 4121.5


@pytest.mark.parametrize(
    "nrg",
    [nrg_reading(None), nrg_reading("n/a"), nrg_reading(0)[:11], None],
    ids=["null", "text", "short", "missing"],  # type: ignore[arg-type]
)
async def test_unknown_live_power_makes_the_average_unknown(
    hass: HomeAssistant, fake_charger: FakeWattpilot, nrg: Any
) -> None:
    """Without the live reading there is no telling a stale average apart."""
    sensor = await _average_power_while_charging(hass, fake_charger)

    fake_charger.push("nrg", nrg)

    assert sensor.native_value is None


@pytest.mark.parametrize(("uid", "index", "unit"), PHASE_READINGS)
async def test_each_phase_reading_has_a_sensor_of_its_own(
    hass: HomeAssistant, fake_charger: FakeWattpilot, uid: str, index: int, unit: str
) -> None:
    """The per-phase readings were only attributes of Charging power, and
    those stay out of the recorder: no history of them existed at all."""
    fake_charger._properties["nrg"] = [float(slot) for slot in range(16)]
    sensor = await make_sensor(hass, fake_charger, uid)

    assert sensor.native_value == index
    assert sensor.native_unit_of_measurement == unit
    assert by_uid(uid).entity_registry_enabled_default is False, "recorder load"


@pytest.mark.parametrize(
    "description", SENSOR_DESCRIPTIONS, ids=[d.uid_suffix for d in SENSOR_DESCRIPTIONS]
)
def test_the_numeric_rule_is_home_assistants(description: Any) -> None:
    """The conversion checks for a number exactly where Home Assistant
    will insist on one when it writes the state (reaudit K-A)."""
    entity = SensorEntity()
    entity.entity_description = description

    expected = entity._numeric_state_expected and not description.enum
    assert sensor_platform._expects_number(description) is expected


async def test_the_temperature_current_limit_is_its_own_sensor(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["amt"] = 32
    sensor = await make_sensor(hass, fake_charger, "amt")

    assert sensor.native_value == 32
    assert sensor.native_unit_of_measurement == "A"
    assert by_uid("amt").entity_registry_enabled_default is False
    assert by_uid("amt").entity_category is EntityCategory.DIAGNOSTIC


async def test_the_current_limit_shows_why_no_more_current_flows(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The state is the hardware ceiling; the single limits say which one
    holds it, and pvSurplus is there only while PV surplus charging runs."""
    limits = dict(fake_charger._properties["cll"])
    sensor = await make_sensor(hass, fake_charger, "cll")

    assert sensor.native_value == limits["currentLimitMax"]
    assert sensor.native_unit_of_measurement == "A"
    assert sensor.extra_state_attributes == {
        "adapterCurrentLimit": limits["adapterCurrentLimit"],
        "cableCurrentLimit": limits["cableCurrentLimit"],
        "temperatureCurrentLimit": limits["temperatureCurrentLimit"],
        "unsymetryCurrentLimit": limits["unsymetryCurrentLimit"],
        "requestedCurrent": limits["requestedCurrent"],
        "pvSurplus": None,
    }

    fake_charger.push("cll", {**limits, "pvSurplus": 7})
    assert sensor.extra_state_attributes["pvSurplus"] == 7
    assert by_uid("cll").entity_registry_enabled_default is False
    assert by_uid("cll").entity_category is EntityCategory.DIAGNOSTIC


async def test_the_current_limit_is_the_hardware_ceiling_not_the_request(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Measured: currentLimitMax stayed 16 A while PV surplus charging
    requested 6; the fixture has all limits at 16 and cannot tell them apart
    (post-check F31-05)."""
    fake_charger._properties["cll"] = {
        "adapterCurrentLimit": 20,
        "cableCurrentLimit": 32,
        "currentLimitMax": 16,
        "requestedCurrent": 6,
        "temperatureCurrentLimit": 32,
        "unsymetryCurrentLimit": 32,
        "pvSurplus": 6,
    }
    sensor = await make_sensor(hass, fake_charger, "cll")

    assert sensor.native_value == 16


@pytest.mark.parametrize("uid", SOURCE_POWER_UIDS)
async def test_charging_power_per_source(
    hass: HomeAssistant, fake_charger: FakeWattpilot, uid: str
) -> None:
    fake_charger._properties[uid] = 1500
    sensor = await make_sensor(hass, fake_charger, uid)

    assert sensor.native_value == 1500
    assert sensor.native_unit_of_measurement == "W"
    assert by_uid(uid).entity_registry_enabled_default is False
    assert (
        by_uid(uid).translation_key == f"charging_power_from_{SOURCE_POWER_NAMES[uid]}"
    )


def test_charging_power_per_source_needs_a_charger_that_reports_it(
    device_properties: dict[str, Any],
) -> None:
    """Firmware 42.5 does not report it; only a Wattpilot Flex was seen to."""
    kept = filter_supported(
        SENSOR_DESCRIPTIONS, firmware="42.5", variant=11, properties=device_properties
    )
    assert {d.uid_suffix for d in kept} & set(SOURCE_POWER_UIDS) == set()

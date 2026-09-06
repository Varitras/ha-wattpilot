"""Sensor conversions against real firmware 42.5 data + fork parity."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.wattpilot import sensor as sensor_platform
from custom_components.wattpilot.descriptions import (
    SENSOR_DESCRIPTIONS,
    filter_supported,
)
from custom_components.wattpilot.hub import WattpilotHub
from custom_components.wattpilot.sensor import WattpilotSensor

from .parity import assert_platform_parity

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"
ENERGY_SPLIT_UIDS = {"whs", "whb", "whg", "who"}
# Kept in sync with tests/test_parity_capstone.py; see its docstring for why
# each addition is there.
EXTRA_SENSOR_UIDS = ENERGY_SPLIT_UIDS | {"pnp", "alw", "acu", "tpa", "fhz"}


def by_uid(uid: str) -> Any:
    return next(d for d in SENSOR_DESCRIPTIONS if d.uid_suffix == uid)


async def make_sensor(
    hass: HomeAssistant, charger: FakeWattpilot, uid: str
) -> WattpilotSensor:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()  # required: pushes route through the hub's dispatch
    sensor = WattpilotSensor(hub, ENTRY_ID, by_uid(uid))
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
    assert "Alex Example" not in caplog.text
    assert "Sam Sample" not in caplog.text
    assert "WARNING" not in caplog.text, "an absent card slot is not a fault"


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
    with caplog.at_level(logging.WARNING):
        fake_charger.push("nrg", "garbage")
        await hass.async_block_till_done()
    assert sensor.native_value == 1
    # The warning is the only trace a dropped push leaves; it has to name
    # which entity, what shape arrived, which charger key and what went wrong.
    # It deliberately does NOT carry the value, nor the exception's message,
    # which can quote the value too: nothing redacts this sink, and a card
    # list logged that way put holder names into it (audit A12-05).
    # Compared whole rather than as a substring: a substring check passes just
    # as happily when the message has grown extra text around it.
    assert any(
        record.getMessage() == "sensor.test_nrg: unexpected str for nrg (IndexError)"
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


async def test_charger_temp_array_extracts_index_without_attributes(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Test value_index branch without index_attributes (tma sensor shape)."""
    fake_charger._properties["tma"] = [40.5, 38.0]
    sensor = await make_sensor(hass, fake_charger, "tma")
    assert sensor.native_value == 40.5
    assert sensor.extra_state_attributes is None or sensor.extra_state_attributes == {}


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


async def test_phases_in_use_reports_the_switched_phase_count(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """`pnp` is the phase count the charger switched to (1 or 3)."""
    fake_charger._properties["pnp"] = 1
    sensor = await make_sensor(hass, fake_charger, "pnp")
    assert sensor.native_value == 1

    fake_charger.push("pnp", 3)
    await hass.async_block_till_done()
    assert sensor.native_value == 3


def test_new_sensors_declare_their_property_and_unit() -> None:
    """The four charging-state sensors added on top of the fork's set."""
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

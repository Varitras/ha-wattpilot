"""Number behavior: float mapping, int writes, fte special setter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from custom_components.wattpilot.descriptions import NUMBER_DESCRIPTIONS
from custom_components.wattpilot.hub import WattpilotHub
from custom_components.wattpilot.number import WattpilotNumber

from .parity import assert_platform_parity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"


def by_uid(uid: str) -> Any:
    return next(d for d in NUMBER_DESCRIPTIONS if d.uid_suffix == uid)


async def make_number(
    hass: HomeAssistant, charger: FakeWattpilot, uid: str
) -> WattpilotNumber:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()  # required: pushes route through the hub's dispatch
    number = WattpilotNumber(hub, ENTRY_ID, by_uid(uid))
    number.hass = hass
    number.entity_id = f"number.test_{uid}"
    await number.async_added_to_hass()
    return number


def test_number_parity_with_fork() -> None:
    # Additions this project chose: car consumption, the energy limit and
    # the four timings set in seconds.
    assert_platform_parity("number", NUMBER_DESCRIPTIONS, {"cco", "dwo", *SECOND_UIDS})


def test_amp_variants_are_disjoint() -> None:
    assert by_uid("amp").variant == "11"
    assert by_uid("amp").native_max_value == 16.0
    assert by_uid("amp_22kw").variant == "22"
    assert by_uid("amp_22kw").native_max_value == 32.0


async def test_amp_reflects_and_writes_int(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["amp"] = 13
    number = await make_number(hass, fake_charger, "amp")
    assert number.native_value == 13.0
    await number.async_set_native_value(15.0)
    assert fake_charger.set_calls[-1] == ("amp", 15)  # int, not 15.0
    # The == above doesn't distinguish 15 from 15.0 (Python numeric equality
    # is cross-type), so pin the actual wire type explicitly too.
    assert isinstance(fake_charger.set_calls[-1][1], int)


async def test_fte_uses_next_trip_energy_setter(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["fte"] = 50000
    number = await make_number(hass, fake_charger, "fte")
    await number.async_set_native_value(60000.0)
    assert fake_charger.next_trip_energy_calls == [60000.0]
    assert fake_charger.set_calls == []


async def test_non_numeric_value_reports_none(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["amp"] = "garbage"
    number = await make_number(hass, fake_charger, "amp")
    assert number.native_value is None


async def test_push_updates_native_value(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Task 9's review found start_dispatch() was called but never
    exercised by a push -- cover the live-push path here."""
    number = await make_number(hass, fake_charger, "fam")
    fake_charger.push("fam", 42)
    assert number.native_value == 42.0


async def test_awp_is_labelled_cent_not_euro(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    The device stores awattarMaxPrice in cent (vendor schema: 'in ct'), so
    labelling it EUR overstated every price 100x -- a device value of 50 (0.50
    EUR) read as 50 EUR, and a written value was off by the same factor. The
    value is passed through unscaled, so the unit has to name what it is: ct.
    """
    number = by_uid("awp")
    assert number.native_unit_of_measurement == "ct"

    fake_charger._properties["awp"] = 50
    entity = await make_number(hass, fake_charger, "awp")
    assert entity.native_value == 50.0
    await entity.async_set_native_value(30.0)
    assert fake_charger.set_calls[-1] == ("awp", 30.0)


async def test_no_energy_limit_reads_as_zero(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["dwo"] = None
    number = await make_number(hass, fake_charger, "dwo")
    assert number.native_value == 0
    fake_charger.push("dwo", 20000.0)
    assert number.native_value == 20000.0


async def test_zero_switches_the_energy_limit_off_instead_of_sending_it(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A limit of 0 Wh would stop every charge; null is the charger's "off"."""
    fake_charger._properties["dwo"] = 20000.0
    number = await make_number(hass, fake_charger, "dwo")
    await number.async_set_native_value(0.0)
    assert fake_charger.set_calls[-1] == ("dwo", None)
    await number.async_set_native_value(5000.0)
    assert fake_charger.set_calls[-1] == ("dwo", 5000.0)


def test_car_consumption_is_there_but_not_created_unasked() -> None:
    """Only the app uses it, so it waits in the registry until enabled."""
    description = by_uid("cco")
    assert description.entity_registry_enabled_default is False
    assert description.native_unit_of_measurement == "kWh/100km"


# The charger keeps these three in milliseconds; 900000 said nothing to a
# person looking for "15 minutes", and Home Assistant converts no units for
# number entities, so the integration shows and takes minutes.
MINUTE_UIDS = ("fmt", "mpwst", "mptwt")
# The same for four more, short enough to be set in seconds.
SECOND_UIDS = ("mcpd", "mci", "psmd", "sumd")


@pytest.mark.parametrize("uid", MINUTE_UIDS)
async def test_durations_are_shown_in_minutes(
    hass: HomeAssistant, fake_charger: FakeWattpilot, uid: str
) -> None:
    fake_charger._properties[uid] = 900000
    number = await make_number(hass, fake_charger, uid)
    assert number.native_unit_of_measurement == "min"
    assert number.native_value == 15.0


@pytest.mark.parametrize("uid", MINUTE_UIDS)
async def test_minutes_reach_the_charger_as_milliseconds(
    hass: HomeAssistant, fake_charger: FakeWattpilot, uid: str
) -> None:
    fake_charger._properties[uid] = 900000
    number = await make_number(hass, fake_charger, uid)
    await number.async_set_native_value(20.0)
    assert fake_charger.set_calls[-1] == (uid, 1200000)
    assert isinstance(fake_charger.set_calls[-1][1], int)


@pytest.mark.parametrize(
    ("minutes", "milliseconds"), [(0.5, 30000), (0.1, 6000), (2.01, 120600)]
)
async def test_a_fraction_of_a_minute_is_kept(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    minutes: float,
    milliseconds: int,
) -> None:
    """An automation may send 0.5 -- Home Assistant checks only the range,
    not the step. Cutting to whole numbers before scaling sends 0 ms, and
    cutting after it turns 2.01 min (120599.99999999999 ms) into 120599."""
    fake_charger._properties["mpwst"] = 300000
    number = await make_number(hass, fake_charger, "mpwst")
    await number.async_set_native_value(minutes)
    assert fake_charger.set_calls[-1] == ("mpwst", milliseconds)


@pytest.mark.parametrize("uid", SECOND_UIDS)
async def test_short_timings_are_set_in_seconds(
    hass: HomeAssistant, fake_charger: FakeWattpilot, uid: str
) -> None:
    fake_charger._properties[uid] = 120000
    number = await make_number(hass, fake_charger, uid)
    assert number.native_unit_of_measurement == "s"
    assert number.native_value == 120.0
    await number.async_set_native_value(90.5)
    assert fake_charger.set_calls[-1] == (uid, 90500)


@pytest.mark.parametrize("uid", SECOND_UIDS)
def test_the_second_timings_wait_to_be_enabled(uid: str) -> None:
    """Fine-tuning for a few; the owner's call: not created unasked."""
    assert by_uid(uid).entity_registry_enabled_default is False

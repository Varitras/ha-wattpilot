"""Number behavior: float mapping, int writes, fte special setter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    assert_platform_parity("number", NUMBER_DESCRIPTIONS)


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

"""Select behavior: wire-value mapping in both directions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.wattpilot.descriptions import SELECT_DESCRIPTIONS
from custom_components.wattpilot.hub import WattpilotHub
from custom_components.wattpilot.select import WattpilotSelect

from .parity import assert_platform_parity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"


def by_uid(uid: str) -> Any:
    return next(d for d in SELECT_DESCRIPTIONS if d.uid_suffix == uid)


async def make_select(
    hass: HomeAssistant, charger: FakeWattpilot, uid: str
) -> WattpilotSelect:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()  # required: pushes route through the hub's dispatch
    select = WattpilotSelect(hub, ENTRY_ID, by_uid(uid))
    select.hass = hass
    select.entity_id = f"select.test_{uid}"
    await select.async_added_to_hass()
    return select


def test_select_parity_with_fork() -> None:
    # "ct" (car profile) is mk-maddin parity, not present in the deysel fixture.
    assert_platform_parity("select", SELECT_DESCRIPTIONS, {"ct"})


async def test_charging_mode_roundtrip(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["lmo"] = 4
    select = await make_select(hass, fake_charger, "lmo")
    assert select.current_option == "Eco"
    assert select.options == ["Default", "Eco", "Next Trip"]
    await select.async_select_option("Next Trip")
    assert fake_charger.set_calls[-1] == ("lmo", 5)
    # lmo's wire values are plain ints (unlike ebo's real bools below); == alone
    # can't tell int 5 from a stray bool, so pin the actual wire type too.
    assert isinstance(fake_charger.set_calls[-1][1], int)
    assert not isinstance(fake_charger.set_calls[-1][1], bool)


async def test_boolean_valued_select(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["ebo"] = True
    select = await make_select(hass, fake_charger, "ebo")
    assert select.current_option == "One-Time"
    await select.async_select_option("Repeat for as long as vehicle is plugged in")
    assert fake_charger.set_calls[-1] == ("ebo", False)
    # `False == 0` in Python, so the tuple equality above wouldn't catch a
    # write that coerced the wire value to an int -- ebo is the one select
    # whose wire values are real booleans, so pin the type explicitly.
    assert isinstance(fake_charger.set_calls[-1][1], bool)


async def test_unknown_wire_value_reports_none(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["lmo"] = 99
    select = await make_select(hass, fake_charger, "lmo")
    assert select.current_option is None


async def test_unknown_label_raises_validation_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    select = await make_select(hass, fake_charger, "lmo")
    with pytest.raises(ServiceValidationError):
        await select.async_select_option("Not A Real Option")


async def test_push_updates_current_option(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Cover the live-push path: start_dispatch() must actually be exercised
    by a push, not just called (task 9's review found this gap once already;
    see test_number.py's test_push_updates_native_value)."""
    select = await make_select(hass, fake_charger, "psm")
    fake_charger.push("psm", 2)
    assert select.current_option == "3 Phases"


async def test_car_profile_roundtrip(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """ct is the mk-maddin-only extra (see the parity allowlist above). It is
    present on firmware 42.5: device_properties.json's "ct" is a real,
    anonymized probe value (scripts/anonymize_probe.py replaces the owner's
    actual vehicle with "default" for privacy; it does not remove the key)."""
    fake_charger._properties["ct"] = "renaultZoe"
    select = await make_select(hass, fake_charger, "ct")
    assert select.current_option == "Renault Zoe/Twingo"
    await select.async_select_option("Kia Soul")
    assert fake_charger.set_calls[-1] == ("ct", "kiaSoul")
    assert isinstance(fake_charger.set_calls[-1][1], str)

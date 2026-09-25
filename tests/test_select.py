"""Select behavior: wire-value mapping in both directions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.wattpilot.descriptions import (
    SELECT_DESCRIPTIONS,
    filter_supported,
)
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
    # Additions this project chose: the car profile (ct), the readable force
    # state (frc) and the current presets (amp_preset).
    assert_platform_parity("select", SELECT_DESCRIPTIONS, {"ct", "frc", "amp_preset"})


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
    with pytest.raises(ServiceValidationError) as raised:
        await select.async_select_option("Not A Real Option")
    assert raised.value.translation_key == "unknown_option"
    assert raised.value.translation_placeholders == {"option": "Not A Real Option"}


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


async def test_force_state_reads_what_the_buttons_write(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["frc"] = 1
    select = await make_select(hass, fake_charger, "frc")
    assert select.current_option == "Off"
    await select.async_select_option("On")
    assert fake_charger.set_calls[-1] == ("frc", 2)
    fake_charger.push("frc", 0)
    assert select.current_option == "Neutral"


async def test_current_presets_are_the_chargers_own(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties.update({"clp": [6, 10, 12, 14, 16], "amp": 10})
    select = await make_select(hass, fake_charger, "amp_preset")
    assert select.options == ["6 A", "10 A", "12 A", "14 A", "16 A"]
    assert select.current_option == "10 A"
    await select.async_select_option("16 A")
    assert fake_charger.set_calls[-1] == ("amp", 16)
    assert isinstance(fake_charger.set_calls[-1][1], int)


async def test_a_current_between_presets_is_no_option(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """13 A, set by hand or by the number entity, has no preset: showing the
    nearest one would claim a current the car is not getting."""
    fake_charger._properties.update({"clp": [6, 10, 12, 14, 16], "amp": 13})
    select = await make_select(hass, fake_charger, "amp_preset")
    assert select.current_option is None


async def test_changed_presets_reach_the_options(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The presets are edited in the app; the entity must follow without a
    reload, and a current that is now a preset must show as one."""
    fake_charger._properties.update({"clp": [6, 10, 12, 14, 16], "amp": 13})
    select = await make_select(hass, fake_charger, "amp_preset")
    fake_charger.push("clp", [6, 13, 16])
    assert select.options == ["6 A", "13 A", "16 A"]
    assert select.current_option == "13 A"


def test_no_presets_no_preset_select() -> None:
    kept = filter_supported(
        SELECT_DESCRIPTIONS, firmware="42.5", variant=11, properties={"amp": 16}
    )
    assert "amp_preset" not in {d.uid_suffix for d in kept}

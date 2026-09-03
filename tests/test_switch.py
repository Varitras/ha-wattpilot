"""Switch behavior: bool mapping, invert semantics, writes via hub."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.wattpilot.descriptions import SWITCH_DESCRIPTIONS
from custom_components.wattpilot.hub import WattpilotHub
from custom_components.wattpilot.switch import WattpilotSwitch

from .parity import assert_platform_parity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"


def by_uid(uid: str) -> Any:
    return next(d for d in SWITCH_DESCRIPTIONS if d.uid_suffix == uid)


async def make_switch(
    hass: HomeAssistant, charger: FakeWattpilot, uid: str
) -> WattpilotSwitch:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()  # required: pushes route through the hub's dispatch
    switch = WattpilotSwitch(hub, ENTRY_ID, by_uid(uid))
    switch.hass = hass
    switch.entity_id = f"switch.test_{uid}"
    await switch.async_added_to_hass()
    return switch


def test_switch_parity_with_fork() -> None:
    assert_platform_parity("switch", SWITCH_DESCRIPTIONS)


async def test_plain_switch_reflects_and_writes(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["fup"] = True
    switch = await make_switch(hass, fake_charger, "fup")
    assert switch.is_on is True
    await switch.async_turn_off()
    assert fake_charger.set_calls[-1] == ("fup", False)


async def test_inverted_switch(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """nmo (Norway Mode) disables ground check on the wire; the entity is
    shown as "Ground Check" and is inverted relative to the wire value
    (vendor doc: "ground check enabled when norway mode is disabled")."""
    fake_charger._properties["nmo"] = True
    switch = await make_switch(hass, fake_charger, "nmo")
    assert switch.is_on is False
    await switch.async_turn_on()
    assert fake_charger.set_calls[-1] == ("nmo", False)


async def test_lock_level_selection_invert(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """bac (buttonAllowCurrentChange) true means the button is unlocked; the
    entity is shown as "Lock Level Selection" (locked = on) and is inverted
    relative to the wire value. Safety-relevant: reversing this would show a
    locked device as unlocked or vice versa."""
    fake_charger._properties["bac"] = True
    switch = await make_switch(hass, fake_charger, "bac")
    assert switch.is_on is False
    await switch.async_turn_on()
    assert fake_charger.set_calls[-1] == ("bac", False)


async def test_unknown_value_reports_none(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["fup"] = "weird"
    switch = await make_switch(hass, fake_charger, "fup")
    assert switch.is_on is None

"""Buttons write their press value; they carry no state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.wattpilot.button import WattpilotButton
from custom_components.wattpilot.descriptions import BUTTON_DESCRIPTIONS
from custom_components.wattpilot.hub import WattpilotHub

from .parity import assert_platform_parity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"


def by_uid(uid: str) -> Any:
    return next(d for d in BUTTON_DESCRIPTIONS if d.uid_suffix == uid)


async def make_button(
    hass: HomeAssistant, charger: FakeWattpilot, uid: str
) -> WattpilotButton:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()  # required: pushes route through the hub's dispatch
    button = WattpilotButton(hub, ENTRY_ID, by_uid(uid))
    button.hass = hass
    button.entity_id = f"button.test_{uid}"
    await button.async_added_to_hass()
    return button


def test_button_parity_with_fork() -> None:
    assert_platform_parity("button", BUTTON_DESCRIPTIONS)


def test_buttons_skip_property_presence_check() -> None:
    assert all(not d.requires_property for d in BUTTON_DESCRIPTIONS)


async def test_press_writes_value(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    for uid, expected in [
        ("frc0", ("frc", 0)),
        ("frc1", ("frc", 1)),
        ("frc2", ("frc", 2)),
        ("rst", ("rst", 1)),
        ("trx", ("trx", 0)),
    ]:
        button = await make_button(hass, fake_charger, uid)
        await button.async_press()
        assert fake_charger.set_calls[-1] == expected
        # == alone can't distinguish int from bool (1 == True); buttons
        # write plain ints to the wire, so pin the actual type too.
        assert isinstance(fake_charger.set_calls[-1][1], int)
        assert not isinstance(fake_charger.set_calls[-1][1], bool)

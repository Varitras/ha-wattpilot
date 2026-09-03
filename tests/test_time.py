"""Time entity: seconds-since-midnight wire format, lib setter for writes."""

from __future__ import annotations

from datetime import time as dt_time
from typing import TYPE_CHECKING

from custom_components.wattpilot.descriptions import TIME_DESCRIPTIONS
from custom_components.wattpilot.hub import WattpilotHub
from custom_components.wattpilot.time import WattpilotTime

from .parity import assert_platform_parity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"


async def make_time(hass: HomeAssistant, charger: FakeWattpilot) -> WattpilotTime:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()  # required: pushes route through the hub's dispatch
    entity = WattpilotTime(hub, ENTRY_ID, TIME_DESCRIPTIONS[0])
    entity.hass = hass
    entity.entity_id = "time.test_ftt"
    await entity.async_added_to_hass()
    return entity


def test_time_parity_with_fork() -> None:
    assert_platform_parity("time", TIME_DESCRIPTIONS)


async def test_seconds_since_midnight_conversion(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["ftt"] = 6 * 3600 + 30 * 60  # 06:30
    entity = await make_time(hass, fake_charger)
    assert entity.native_value == dt_time(6, 30)
    # == alone would also accept a look-alike (e.g. a string); the code's
    # job here is producing a real datetime.time, so pin the type too.
    assert isinstance(entity.native_value, dt_time)


async def test_set_value_uses_lib_next_trip(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["ftt"] = 0
    entity = await make_time(hass, fake_charger)
    await entity.async_set_value(dt_time(7, 15))
    assert fake_charger.next_trip_calls == [dt_time(7, 15)]


async def test_malformed_value_reports_none(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["ftt"] = "soon"
    entity = await make_time(hass, fake_charger)
    assert entity.native_value is None


async def test_push_updates_native_value(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Cover the live-push path: start_dispatch() must actually be exercised
    by a push, not just called (see test_number.py's test_push_updates_native_value)."""
    entity = await make_time(hass, fake_charger)
    fake_charger.push("ftt", 3600)  # 01:00
    assert entity.native_value == dt_time(1, 0)

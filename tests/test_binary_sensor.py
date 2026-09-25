"""Binary sensors: which car states mean plugged in, and which mean charging."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from custom_components.wattpilot.binary_sensor import WattpilotBinarySensor
from custom_components.wattpilot.descriptions import (
    BINARY_SENSOR_DESCRIPTIONS,
    filter_supported,
)
from custom_components.wattpilot.hub import WattpilotHub

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"


def by_uid(uid: str) -> Any:
    return next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.uid_suffix == uid)


async def make_binary_sensor(
    hass: HomeAssistant, charger: FakeWattpilot, uid: str
) -> WattpilotBinarySensor:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()
    entity = WattpilotBinarySensor(hub, ENTRY_ID, by_uid(uid))
    entity.hass = hass
    entity.entity_id = f"binary_sensor.test_{uid}"
    await entity.async_added_to_hass()
    return entity


def test_uids_are_unique() -> None:
    uids = [description.uid_suffix for description in BINARY_SENSOR_DESCRIPTIONS]
    assert len(uids) == len(set(uids))


def test_both_are_created_for_the_reference_charger(
    device_properties: dict[str, Any],
) -> None:
    kept = filter_supported(
        BINARY_SENSOR_DESCRIPTIONS,
        firmware="42.5",
        variant=11,
        properties=device_properties,
    )
    assert {d.uid_suffix for d in kept} == {"car_plugged_in", "car_charging"}


# car: 0 unknown, 1 idle (no car), 2 charging, 3 waiting for the car,
# 4 complete, 5 error. Unknown and error are no answer, so neither on nor off.
@pytest.mark.parametrize(
    ("car", "expected"),
    [
        (1, (False, False)),
        (2, (True, True)),
        (3, (True, False)),
        (4, (True, False)),
        (0, (None, None)),
        (5, (None, None)),
        (None, (None, None)),
        ([2], (None, None)),
    ],
)
async def test_car_state_answers_both_questions(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    car: Any,
    expected: tuple[bool | None, bool | None],
) -> None:
    """expected: (plugged in, charging)."""
    fake_charger._properties["car"] = car
    plug = await make_binary_sensor(hass, fake_charger, "car_plugged_in")
    load = await make_binary_sensor(hass, fake_charger, "car_charging")
    assert (plug.is_on, load.is_on) == expected


async def test_a_push_moves_the_state(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["car"] = 1
    load = await make_binary_sensor(hass, fake_charger, "car_charging")
    fake_charger.push("car", 2)
    assert load.is_on is True
    fake_charger.push("car", 4)
    assert load.is_on is False

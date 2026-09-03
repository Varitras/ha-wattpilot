"""Base entity: unique_id, device info, push subscription, availability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import EntityDescription

from custom_components.wattpilot.const import (
    DOMAIN,
    signal_availability,
    signal_property,
)
from custom_components.wattpilot.descriptions import WattpilotDescriptionMixin
from custom_components.wattpilot.entity import WattpilotEntity
from custom_components.wattpilot.hub import WattpilotHub

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"


@dataclass(frozen=True, kw_only=True)
class Desc(WattpilotDescriptionMixin, EntityDescription):
    """Minimal test description."""


class RecordingEntity(WattpilotEntity):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.applied: list[Any] = []

    def _apply_value(self, value: Any) -> None:
        self.applied.append(value)


async def make_added_entity(
    hass: HomeAssistant, charger: FakeWattpilot
) -> RecordingEntity:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()
    entity = RecordingEntity(hub, ENTRY_ID, Desc(key="amp_entity", charger_key="amp"))
    entity.hass = hass
    entity.entity_id = "sensor.test_amp"
    await entity.async_added_to_hass()
    return entity


async def test_unique_id_and_device_info(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entity = await make_added_entity(hass, fake_charger)
    assert entity.unique_id == "123456-amp"
    assert (DOMAIN, "123456") in entity.device_info["identifiers"]
    assert entity.device_info["sw_version"] == "42.5"


async def test_initial_value_applied_from_store(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["amp"] = 13
    entity = await make_added_entity(hass, fake_charger)
    assert entity.applied == [13]


async def test_push_reaches_only_matching_entity(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entity = await make_added_entity(hass, fake_charger)
    entity.applied.clear()
    async_dispatcher_send(hass, signal_property(ENTRY_ID, "eto"), 999)
    await hass.async_block_till_done()
    assert entity.applied == []
    async_dispatcher_send(hass, signal_property(ENTRY_ID, "amp"), 15)
    await hass.async_block_till_done()
    assert entity.applied == [15]


async def test_write_only_description_ignores_property(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """requires_property=False must skip both the initial read and the subscription.

    Uses "amp" (present with a non-None value in the fixture) rather than an
    absent key, so a regression that ignores the flag would still be caught.
    """
    hub = WattpilotHub(hass, ENTRY_ID, fake_charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()
    entity = RecordingEntity(
        hub,
        ENTRY_ID,
        Desc(key="rst_entity", charger_key="amp", requires_property=False),
    )
    entity.hass = hass
    entity.entity_id = "button.test_rst"
    await entity.async_added_to_hass()
    assert entity.applied == []
    async_dispatcher_send(hass, signal_property(ENTRY_ID, "amp"), 99)
    await hass.async_block_till_done()
    assert entity.applied == []
    assert entity.available is True


async def test_availability_follows_hub(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entity = await make_added_entity(hass, fake_charger)
    assert entity.available is True
    fake_charger.connected = False
    async_dispatcher_send(hass, signal_availability(ENTRY_ID), False)  # noqa: FBT003 -- signal payload, not a flag
    await hass.async_block_till_done()
    assert entity.available is False

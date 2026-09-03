"""Services: parity field names, validation errors, hub delegation."""

from __future__ import annotations

import logging
from datetime import time as dt_time
from typing import TYPE_CHECKING

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattpilot.const import CONF_UPDATE_INTERVAL, DOMAIN

from .test_init import V2_LOCAL_DATA, setup_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot


async def setup_with_device(
    hass: HomeAssistant, charger: FakeWattpilot
) -> tuple[MockConfigEntry, str]:
    # Throttle off: these tests assert on service behaviour and the state a
    # push produces, not on coalescing. The default interval would only make
    # them wait on a flush timer.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=V2_LOCAL_DATA,
        options={CONF_UPDATE_INTERVAL: 0},
        version=2,
        unique_id="123456",
    )
    assert await setup_entry(hass, entry, charger)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "123456")})
    assert device is not None
    return entry, device.id


async def test_set_next_trip(hass: HomeAssistant, fake_charger: FakeWattpilot) -> None:
    _entry, device_id = await setup_with_device(hass, fake_charger)
    await hass.services.async_call(
        DOMAIN,
        "set_next_trip",
        {"device_id": device_id, "trigger_time": "06:30:00"},
        blocking=True,
    )
    assert fake_charger.next_trip_calls == [dt_time(6, 30)]


async def test_set_goe_cloud_roundtrip(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    _entry, device_id = await setup_with_device(hass, fake_charger)
    # Seed the opposite of the expected end state -- the device fixture
    # already has "cae": False, which would let this test pass even if the
    # disable branch were deleted entirely. Assigning through
    # all_properties would be a silent no-op (copy-returning property), so
    # this goes through _properties directly.
    fake_charger._properties["cae"] = True
    await hass.services.async_call(
        DOMAIN,
        "set_goe_cloud",
        {"device_id": device_id, "cloud_api": False},
        blocking=True,
    )
    assert fake_charger.all_properties["cae"] is False
    assert fake_charger.disable_cloud_api_count == 1


async def test_set_goe_cloud_enable_logs_no_part_of_the_key(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audit VA-14: this used to log the key's first and last four characters
    plus the vendor URL, which embeds the full serial -- and the old test
    pinned those eight characters as intended behaviour. Neither earns its
    place: "enabled" is what an operator needs, and the key and URL are
    available from the charger itself.
    """
    _entry, device_id = await setup_with_device(hass, fake_charger)
    full_key = fake_charger.cloud_api_key
    with caplog.at_level(logging.INFO):
        await hass.services.async_call(
            DOMAIN,
            "set_goe_cloud",
            {"device_id": device_id, "cloud_api": True},
            blocking=True,
        )
    logged = [r.getMessage() for r in caplog.records if "cloud API" in r.getMessage()]
    assert logged == ["go-e cloud API enabled"], logged
    # Checked against the whole line, not the whole log: the hub names the
    # charger's serial when it connects, which is the device's identity in
    # the owner's own log and not what this finding was about.
    assert full_key not in caplog.text
    assert full_key[:4] not in logged[0], "no prefix of the key"
    assert full_key[-4:] not in logged[0], "no suffix of the key"
    assert "123456" not in logged[0], "no serial, which the vendor URL carries"


async def test_disconnect_and_reconnect(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    _entry, device_id = await setup_with_device(hass, fake_charger)
    await hass.services.async_call(
        DOMAIN, "disconnect_charger", {"device_id": device_id}, blocking=True
    )
    assert fake_charger.connected is False
    await hass.services.async_call(
        DOMAIN, "reconnect_charger", {"device_id": device_id}, blocking=True
    )
    assert fake_charger.connected is True
    assert len(fake_charger._callbacks) == 1  # no duplicate dispatch


async def test_disconnect_marks_the_entities_unavailable(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    services.yaml and both translations promise it: "entities become unavailable".

    They did not. async_shutdown tore down the dispatcher and cancelled the
    30-second availability check -- the very timer that would have noticed --
    without ever signalling the change, so nothing wrote entity state again.
    Every sensor kept displaying its last reading, and automations went on
    calculating with numbers from a charger that is no longer talking.
    """
    _entry, device_id = await setup_with_device(hass, fake_charger)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "123456-wh")
    fake_charger.push("wh", 1234.0)
    await hass.async_block_till_done()
    # wh is an energy sensor: 1234 Wh is shown as 1.234 kWh.
    assert float(hass.states.get(entity_id).state) == pytest.approx(1.234)

    await hass.services.async_call(
        DOMAIN, "disconnect_charger", {"device_id": device_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"


async def test_reconnect_picks_up_what_changed_while_disconnected(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    Reconnecting has to re-subscribe before the charger replays its state.

    The client sends its full status the moment the socket is up. Connecting
    first and only then re-registering the dispatcher meant that burst
    arrived with nobody listening, so every value that moved during the
    outage stayed stale until the device happened to send it again -- which,
    for a rarely changing property, can be never.
    """
    _entry, device_id = await setup_with_device(hass, fake_charger)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "123456-wh")
    fake_charger.push("wh", 1234.0)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN, "disconnect_charger", {"device_id": device_id}, blocking=True
    )
    # The charger keeps charging while Home Assistant is not listening.
    fake_charger._properties["wh"] = 5678.0
    await hass.services.async_call(
        DOMAIN, "reconnect_charger", {"device_id": device_id}, blocking=True
    )
    await hass.async_block_till_done()
    # wh is an energy sensor: 5678 Wh is shown as 5.678 kWh.
    assert float(hass.states.get(entity_id).state) == pytest.approx(5.678)
    assert len(fake_charger._callbacks) == 1, "no duplicate dispatch"


async def test_unknown_device_raises_validation_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    await setup_with_device(hass, fake_charger)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_next_trip",
            {"device_id": "no_such_device", "trigger_time": "06:30:00"},
            blocking=True,
        )


async def test_foreign_device_raises_validation_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A device that exists but belongs to a different integration must be
    rejected -- reaching a hub for it would be a real bug, not just an
    unknown-id typo."""
    await setup_with_device(hass, fake_charger)
    other_entry = MockConfigEntry(domain="other_domain")
    other_entry.add_to_hass(hass)
    foreign_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={("other_domain", "foreign-1")},
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_next_trip",
            {"device_id": foreign_device.id, "trigger_time": "06:30:00"},
            blocking=True,
        )


async def test_unloaded_device_raises_validation_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A device whose config entry is a real wattpilot entry but is no
    longer loaded (no runtime_data) must be rejected, not crash trying to
    reach a hub that no longer exists."""
    entry, device_id = await setup_with_device(hass, fake_charger)
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_next_trip",
            {"device_id": device_id, "trigger_time": "06:30:00"},
            blocking=True,
        )

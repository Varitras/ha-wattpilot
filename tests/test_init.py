"""Entry lifecycle: setup/unload, fork v1 migration, unique_id upgrade."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.wattpilot import async_migrate_entry, async_setup_entry
from custom_components.wattpilot.const import (
    CONF_AWAITING_SERIAL,
    CONF_CONNECTION_TYPE,
    CONF_UPDATE_INTERVAL,
    CONNECTION_CLOUD,
    CONNECTION_LOCAL,
    DOMAIN,
)

from .conftest import FakeWattpilot

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

V2_LOCAL_DATA = {
    CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
    "host": "192.168.1.50",
    "password": "secret",
}
# A fork entry as it looks right after the v1->v2 migration: still keyed on
# the IP, serial not learned yet. Only that state may adopt a serial that
# differs from the entry's unique_id -- see _adopt_serial.
V2_AWAITING_SERIAL = {**V2_LOCAL_DATA, CONF_AWAITING_SERIAL: True}
# Verbatim fork (v1) entry payloads:
V1_LOCAL_DATA = {
    "friendly_name": "Wattpilot",
    "ip_address": "192.168.1.50",
    "password": "secret",
    "timeout": 15,
    "connection": "local",
}
V1_CLOUD_DATA = {
    "friendly_name": "Wattpilot",
    "serial": "123456",
    "password": "secret",
    "timeout": 15,
    "connection": "cloud",
}


def patch_charger(charger: FakeWattpilot) -> Any:
    return patch("custom_components.wattpilot.hub.Wattpilot", return_value=charger)


async def setup_entry(
    hass: HomeAssistant, entry: MockConfigEntry, charger: FakeWattpilot
) -> bool:
    entry.add_to_hass(hass)
    with patch_charger(charger):
        result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result


async def test_setup_creates_entities_and_unloads(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)
    assert entry.state is ConfigEntryState.LOADED
    sensor_states = list(hass.states.async_all("sensor"))
    assert len(sensor_states) >= 10  # enabled-by-default sensors from fixture
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert fake_charger.disconnect_count >= 1


async def test_setup_wires_credentials_and_pushes_end_to_end(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The one test that walks the whole chain: entry data -> client, and
    charger push -> hub dispatch -> entity state. Every platform test builds
    its entity by hand, so nothing else would notice if setup handed the hub
    and the entities two different entry ids and detached them from each
    other."""
    # Throttle off: this test pins the setup -> push -> entity wiring, not the
    # coalescing. With the default interval the push would only be visible
    # after a flush timer, which this test is not about.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=V2_LOCAL_DATA,
        options={CONF_UPDATE_INTERVAL: 0},
        version=2,
        unique_id="123456",
    )
    entry.add_to_hass(hass)
    with patch_charger(fake_charger) as client:
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert client.call_args.kwargs == {"host": "192.168.1.50", "password": "secret"}

    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "123456-wh")
    assert entity_id is not None
    fake_charger.push("wh", 4242.0)
    await hass.async_block_till_done()
    # 4242 Wh shown as kWh (see test_energy_sensors_are_reported_in_kwh).
    assert float(hass.states.get(entity_id).state) == pytest.approx(4.242)


async def test_a_push_during_platform_setup_is_not_lost(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    The charger does not wait for Home Assistant to finish setting up.

    Every entity reads its initial value as it is added. If dispatching only
    starts once all seven platforms are up, a push landing in between
    updates the client's cache and reaches nobody: the entity already took
    its value and will not look again. It then shows a stale reading until
    the device happens to send that property once more -- for something like
    the charging current, which only moves when a human moves it, that can
    be days.

    Simulated by pushing right after the platforms are forwarded, which is
    exactly the window.
    """
    # Throttle off: this test is about the push window during platform setup,
    # not the coalescing -- the buffered path is covered separately.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=V2_LOCAL_DATA,
        options={CONF_UPDATE_INTERVAL: 0},
        version=2,
        unique_id="123456",
    )
    entry.add_to_hass(hass)
    forward = hass.config_entries.async_forward_entry_setups

    async def forward_then_push(config_entry: Any, platforms: Any) -> None:
        await forward(config_entry, platforms)
        fake_charger.push("wh", 9999.0)

    with (
        patch_charger(fake_charger),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", forward_then_push
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "123456-wh")
    # 9999 Wh shown as kWh (see test_energy_sensors_are_reported_in_kwh).
    assert float(hass.states.get(entity_id).state) == pytest.approx(9.999)


async def test_setup_applies_the_default_update_interval(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    With no options set, setup throttles at the 5 s default.

    This is the one test proving the default reaches the hub through setup:
    a post-setup push is held back and only lands after the interval elapses.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    entry.add_to_hass(hass)
    with patch_charger(fake_charger):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "123456-wh")
    before = hass.states.get(entity_id).state
    fake_charger.push("wh", 4242.0)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == before  # held by the default throttle

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    # 4242 Wh shown as kWh (see test_energy_sensors_are_reported_in_kwh).
    assert float(hass.states.get(entity_id).state) == pytest.approx(4.242)


async def test_energy_sensors_are_reported_in_kwh(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    The charger reports session energy in Wh; the entity must show kWh.

    Home Assistant does the conversion from the native Wh unit because the
    description asks for it -- the stored value and unit both move to kWh,
    while the native unit stays Wh so fork parity and the recorder history
    are untouched.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=V2_LOCAL_DATA,
        options={CONF_UPDATE_INTERVAL: 0},
        version=2,
        unique_id="123456",
    )
    entry.add_to_hass(hass)
    with patch_charger(fake_charger):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "123456-wh")
    fake_charger.push("wh", 22665.69)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert float(state.state) == pytest.approx(22.66569)


async def test_unique_id_upgraded_from_ip_to_serial(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_AWAITING_SERIAL, version=2, unique_id="192.168.1.50"
    )
    assert await setup_entry(hass, entry, fake_charger)
    assert entry.unique_id == "123456"


async def test_the_serial_upgrade_permission_is_consumed_once(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    """
    A fork entry may adopt a serial exactly once, then never again.

    If the permission outlived the upgrade, the entry would stay open to
    being relabelled by whatever charger answers at that address next -- the
    guard would exist but protect nothing from the second time on.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_AWAITING_SERIAL, version=2, unique_id="192.168.1.50"
    )
    assert await setup_entry(hass, entry, FakeWattpilot(device_properties))
    assert entry.unique_id == "123456"
    assert CONF_AWAITING_SERIAL not in entry.data
    assert entry.data["host"] == "192.168.1.50", "the rest of the entry must survive"
    assert await hass.config_entries.async_unload(entry.entry_id)

    swapped = FakeWattpilot(device_properties)
    swapped.serial = "999999"
    with patch_charger(swapped), pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry(hass, entry)
    assert raised.value.translation_key == "wrong_charger"


async def test_duplicate_serial_fails_setup(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    first = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, first, FakeWattpilot(device_properties))
    second = MockConfigEntry(
        domain=DOMAIN,
        data={**V2_AWAITING_SERIAL, "host": "192.168.1.51"},
        version=2,
        unique_id="192.168.1.51",
    )
    second.add_to_hass(hass)
    second_charger = FakeWattpilot(device_properties)
    # Called directly rather than through async_setup: the error carries the
    # message the user is shown, and going through the config entry manager
    # would flatten every failure reason into the same SETUP_ERROR state.
    with patch_charger(second_charger), pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry(hass, second)
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "duplicate_device"
    assert raised.value.translation_placeholders == {"serial": "123456"}
    # Rejected hub's connection must be closed before the error is raised,
    # not leaked.
    assert second_charger.disconnect_count >= 1


async def test_second_charger_with_its_own_serial_sets_up(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    """Two Wattpilots in one household are a normal install, not a duplicate."""
    first = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, first, FakeWattpilot(device_properties))
    other_charger = FakeWattpilot(device_properties)
    other_charger.serial = "999999"
    second = MockConfigEntry(
        domain=DOMAIN,
        data={**V2_AWAITING_SERIAL, "host": "192.168.1.51"},
        version=2,
        unique_id="192.168.1.51",
    )
    assert await setup_entry(hass, second, other_charger)
    assert second.unique_id == "999999"


async def test_a_different_charger_at_the_same_address_is_refused(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    """
    A serial that disagrees with the entry means a different physical charger.

    Addresses get reused -- DHCP hands the old lease to the neighbour's
    Wattpilot, or the hardware is replaced. Adopting the new serial would
    rewrite this entry's entities onto that other device: same entity ids,
    same recorder history, another charger's readings, and not one error
    anywhere. Silent, plausible, wrong data is the worst failure this
    integration can produce, so setup has to refuse.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    seeded = registry.async_get_or_create(
        "sensor", DOMAIN, "123456-wh", config_entry=entry
    )

    swapped = FakeWattpilot(device_properties)
    swapped.serial = "999999"
    with patch_charger(swapped), pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry(hass, entry)

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "wrong_charger"
    assert raised.value.translation_placeholders == {
        "expected": "123456",
        "found": "999999",
    }
    assert entry.unique_id == "123456", "the entry must keep its identity"
    # The registry is the thing actually at risk: nothing may have moved.
    assert registry.async_get(seeded.entity_id).unique_id == "123456-wh"
    assert registry.async_get_entity_id("sensor", DOMAIN, "999999-wh") is None
    assert swapped.disconnect_count >= 1, "refused hub must not leak its socket"


async def test_same_unique_id_in_another_domain_is_ignored(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The duplicate check must look at this integration's entries only --
    a bare serial is a plausible unique_id for any number of integrations."""
    MockConfigEntry(domain="light", unique_id="123456").add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_AWAITING_SERIAL, version=2, unique_id="192.168.1.50"
    )
    assert await setup_entry(hass, entry, fake_charger)
    assert entry.unique_id == "123456"


async def test_cloud_entry_is_rejected_with_a_translated_error(
    hass: HomeAssistant,
) -> None:
    """Cloud entries exist only as migrated fork leftovers; the refusal has to
    explain itself, so both halves of the translation reference are pinned."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECTION_TYPE: CONNECTION_CLOUD, "serial": "123456"},
        version=2,
    )
    entry.add_to_hass(hass)
    with pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry(hass, entry)
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "cloud_not_supported"


async def test_migrate_v1_local_entry(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=V1_LOCAL_DATA,
        version=1,
        unique_id="192.168.1.50",
        title="Garage Wattpilot",
    )
    with caplog.at_level(logging.INFO):
        assert await setup_entry(hass, entry, fake_charger)
    assert entry.version == 2
    assert entry.data == V2_LOCAL_DATA
    assert entry.title == "Garage Wattpilot"
    assert entry.unique_id == "123456"
    # A one-way schema rewrite of someone else's config entry should say so,
    # naming the entry it touched. Compared whole, not as a substring, for the
    # same reason as the sensor warning in test_sensor.py.
    assert any(
        record.getMessage()
        == f"Migrated fork config entry {entry.entry_id} to version 2"
        for record in caplog.records
    )


async def test_migrate_keeps_a_current_entry_untouched(hass: HomeAssistant) -> None:
    """Home Assistant only calls this for older entries today, but nothing in
    the integration enforces that -- an entry already on the current version
    must migrate to success, not report failure."""
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=2)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.data == V2_LOCAL_DATA


async def test_migrate_refuses_a_downgrade(hass: HomeAssistant) -> None:
    """An entry written by a newer version of this integration cannot be
    understood here; failing is the only honest answer."""
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=3)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is False


async def test_migrate_v1_entry_without_unique_id(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """mk-maddin v1 entries carry no config-entry unique_id at all (only
    entity-level unique_ids differ, see test_registry_migration.py) --
    the upgrade must still assign the serial, not crash on None != serial.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=V1_LOCAL_DATA, version=1, unique_id=None
    )
    assert await setup_entry(hass, entry, fake_charger)
    assert entry.version == 2
    assert entry.data == V2_LOCAL_DATA
    assert entry.unique_id == "123456"


async def test_migrate_v1_cloud_entry_fails_with_clear_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=V1_CLOUD_DATA, version=1, unique_id="123456"
    )
    await setup_entry(hass, entry, fake_charger)
    assert entry.version == 2
    assert entry.data[CONF_CONNECTION_TYPE] == CONNECTION_CLOUD
    assert entry.data["serial"] == "123456"
    # Carried over even though setup then refuses: the entry stays in place
    # for the user to reconfigure, and losing the password would strand it.
    assert entry.data["password"] == V1_CLOUD_DATA["password"]
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_ha_stop_disconnects(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert fake_charger.disconnect_count >= 1


async def test_a_failing_setup_hands_back_the_live_connection(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Audit VA-02: everything after async_connect() owns a live connection,
    a push callback and timers, and HA calls async_unload_entry only for an
    entry that finished setup. A failure in between must give them back, or
    the next retry adds a second hub on the same dispatcher signals.

    Driven through the real config entry manager on purpose: calling
    async_setup_entry directly would not show what the manager does or does
    not clean up afterwards."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    entry.add_to_hass(hass)
    with (
        patch_charger(fake_charger),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("platform setup exploded"),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_charger.disconnect_count == 1, (
        "the hub kept its connection after a failed setup"
    )
    assert not fake_charger.connected


async def test_a_firmware_change_reloads_the_entry(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Audit VA-05: the platforms filter their tables once, at setup, and the
    device's sw_version is frozen when the entities are built. After a
    firmware jump the newly supported entities stay missing and the device
    page keeps showing the old version until someone reloads by hand.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=V2_LOCAL_DATA,
        version=2,
        unique_id="123456",
        # Interval 0 forwards each push immediately; the default (5 s) would
        # buffer them behind a timer this test does not advance.
        options={CONF_UPDATE_INTERVAL: 0},
    )
    assert await setup_entry(hass, entry, fake_charger)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        fake_charger.push("fwv", "42.5")
        await hass.async_block_till_done()
        assert not reload.called, "the same version is not a change"

        fake_charger.push("fwv", "43.1")
        await hass.async_block_till_done()
        assert reload.called, "a firmware jump must rebuild the entity set"

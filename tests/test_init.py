"""Entry lifecycle: setup/unload, fork v1 migration, unique_id upgrade."""

from __future__ import annotations

import errno
import logging
from dataclasses import replace
from datetime import timedelta
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import State
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.wattpilot import (
    _SerialMask,
    async_migrate_entry,
    async_setup_entry,
)
from custom_components.wattpilot.api import (
    AuthenticationError,
    DeviceIdentityError,
    WattpilotError,
)
from custom_components.wattpilot.api.exceptions import WattpilotConnectionError
from custom_components.wattpilot.const import (
    CONF_AWAITING_SERIAL,
    CONF_CONNECTION_TYPE,
    CONF_UPDATE_INTERVAL,
    CONNECTION_CLOUD,
    CONNECTION_LOCAL,
    DOMAIN,
)
from custom_components.wattpilot.descriptions import (
    BINARY_SENSOR_DESCRIPTIONS,
    NUMBER_DESCRIPTIONS,
    SELECT_DESCRIPTIONS,
    SENSOR_DESCRIPTIONS,
    SWITCH_DESCRIPTIONS,
    TIME_DESCRIPTIONS,
)

from .conftest import FakeWattpilot
from .test_config_flow import DISCOVERY, start_discovery_flow
from .test_sensor import nrg_reading

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


async def test_a_password_rejected_while_running_asks_for_a_new_one(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The client stops reconnecting once the charger refuses the password
    -- retrying would only repeat the refusal. But nothing told Home
    Assistant: the entry just stayed unavailable until someone reloaded it,
    with no hint that the password was the problem (audit A14-02)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)

    fake_charger.connected = False
    fake_charger.refusal = AuthenticationError("wrong password")
    for minutes in (1, 2):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
        await hass.async_block_till_done()

    assert len(reauth_flows(hass)) == 1, (
        "no reauthentication was started, or a second one"
    )


def reauth_flows(hass: HomeAssistant) -> list[Any]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == "reauth"
    ]


async def test_another_charger_answering_while_running_is_reported(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The client stops for good when a different charger answers at the
    address -- DHCP handed it on -- rather than show that charger's readings
    as this one's. But nothing said so: the entities just stayed unavailable
    (audit A15-04). A new password would not help, so this is a repair
    notice, not reauthentication; it lasts as long as the loaded entry."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)
    issue_id = f"wrong_charger_{entry.entry_id}"

    fake_charger.connected = False
    fake_charger.refusal = DeviceIdentityError("refused", found="999999")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1))
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None, "the refusal was not reported"
    assert issue.translation_key == "wrong_charger"
    assert issue.translation_placeholders == {"serial": "123456"}
    assert not reauth_flows(hass), "a new password cannot fix another charger"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_a_charger_asking_for_an_unknown_sign_in_is_reported(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A third way the client stops for good: the charger asks for a password
    scheme it does not know. Nothing reported it; the entities just stayed
    unavailable (reaudit R1-13)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)
    issue_id = f"charger_refused_{entry.entry_id}"

    fake_charger.connected = False
    fake_charger.refusal = WattpilotConnectionError("Unsupported password hash: x9")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1))
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None, "the refusal was not reported"
    assert issue.translation_key == "charger_refused"
    # Nothing in Home Assistant can fix it; a Fix button would lead nowhere.
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"error": "Unsupported password hash: x9"}
    assert not reauth_flows(hass), "a new password cannot fix this"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    ("refusal", "kind"),
    [
        (DeviceIdentityError("refused", found="999999"), "wrong_charger"),
        (WattpilotConnectionError("Unsupported password hash: x9"), "charger_refused"),
    ],
)
async def test_a_refusal_notice_goes_once_the_charger_is_back(
    hass: HomeAssistant, fake_charger: FakeWattpilot, refusal: Any, kind: str
) -> None:
    """After a successful reconnect the notice still said the integration
    had stopped connecting, next to available entities (reaudit RF-L-03)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)
    issue_id = f"{kind}_{entry.entry_id}"
    fake_charger.connected = False
    fake_charger.refusal = refusal
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1))
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    fake_charger.refusal = None
    fake_charger.connected = True
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_the_device_takes_the_name_the_entry_was_created_with(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger.name = "Wattpilot_123456"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**V2_LOCAL_DATA, CONF_NAME: "Wattpilot 2"},
        version=2,
        unique_id="123456",
    )
    assert await setup_entry(hass, entry, fake_charger)

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.name == "Wattpilot 2"
    assert hass.states.get("sensor.wattpilot_2_total_energy") is not None


async def test_an_entry_from_before_keeps_its_device_name(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Entries created before new ones were named Wattpilot carry no name:
    their device keeps the host name, and with it their entity IDs."""
    fake_charger.name = "Wattpilot_123456"
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.name == "Wattpilot_123456"


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
    assert client.call_args.kwargs == {
        "host": "192.168.1.50",
        "password": "secret",
        "serial": "123456",
    }

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
    starts once all platforms are up, a push landing in between
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


async def test_a_refused_charger_is_reported_even_if_disconnecting_fails(
    hass: HomeAssistant,
    device_properties: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The identity check shut the hub down before raising wrong_charger,
    and the outer setup guard shut it down a second time. With a
    disconnect that also fails, that cleanup error replaced the refusal:
    the user saw "Failed to disconnect" instead of why setup was refused
    (audit A14-03)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    entry.add_to_hass(hass)
    swapped = FakeWattpilot(device_properties)
    swapped.serial = "999999"
    swapped.disconnect_error = WattpilotError("socket already gone")

    with (
        caplog.at_level(logging.DEBUG),
        patch_charger(swapped),
        pytest.raises(ConfigEntryError) as raised,
    ):
        await async_setup_entry(hass, entry)

    assert raised.value.translation_key == "wrong_charger"
    assert swapped.disconnect_attempts == 1, "the hub was shut down twice"
    # Swallowed, not lost: the cleanup failure stays findable, with its trace.
    cleanup = [
        r
        for r in caplog.records
        if r.getMessage() == "Ignoring error while closing after failed setup"
    ]
    assert len(cleanup) == 1
    # Not `is not None`: logging keeps exc_info=False as False.
    assert cleanup[0].exc_info
    assert isinstance(cleanup[0].exc_info[1], HomeAssistantError)


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
    assert raised.value.translation_placeholders == {"serial": "****56"}
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
    # Masked: Home Assistant logs this message as well.
    assert raised.value.translation_placeholders == {
        "expected": "****56",
        "found": "****99",
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
    assert entry.data == {**V2_LOCAL_DATA, CONF_NAME: "Wattpilot"}
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


async def test_a_migrated_fork_entry_names_new_entities_without_the_serial(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A fork entry kept no name, so its device took the host name and every
    entity the fork lacked got the serial in its ID (reaudit RF-P-04). The
    fork's own friendly name is what its entity IDs were made from."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**V1_LOCAL_DATA, "friendly_name": "Garage"},
        version=1,
        unique_id="192.168.1.50",
        title="Garage",
    )
    assert await setup_entry(hass, entry, fake_charger)

    assert entry.data[CONF_NAME] == "Garage"
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, "123456-tpa")
    assert entity_id == "sensor.garage_average_charging_power"


async def test_a_fork_name_another_entry_has_is_numbered_instead(
    hass: HomeAssistant,
) -> None:
    """Two devices of one name would hand out entity IDs ending in _2."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="111111",
        title="Wattpilot",
        version=2,
        data={**V2_LOCAL_DATA, CONF_NAME: "Wattpilot"},
    ).add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, data=V1_LOCAL_DATA, version=1, title="Wattpilot"
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.data[CONF_NAME] == "Wattpilot 2"


async def test_a_fork_entry_without_a_friendly_name_is_named_wattpilot(
    hass: HomeAssistant,
) -> None:
    """Its own title does not count as taken: the name is for this entry."""
    data = {k: v for k, v in V1_LOCAL_DATA.items() if k != "friendly_name"}
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=1, title="Wattpilot")
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.data[CONF_NAME] == "Wattpilot"


async def test_a_migrated_fork_cloud_entry_is_named_for_its_reconfigure(
    hass: HomeAssistant,
) -> None:
    """Setup sends a cloud entry to Reconfigure, which keeps the stored name;
    without one the device took the host name and new entity IDs carried the
    serial (post-check PC5-R-04)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**V1_CLOUD_DATA, "friendly_name": "Garage"},
        version=1,
        title="Garage",
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.data[CONF_NAME] == "Garage"


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
    assert entry.data == {**V2_LOCAL_DATA, CONF_NAME: "Wattpilot"}
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


@pytest.mark.parametrize(
    ("data", "unique_id", "expected"),
    [
        (V2_LOCAL_DATA, "123456", "123456"),
        (V2_AWAITING_SERIAL, "192.168.1.50", None),
    ],
)
async def test_the_known_serial_is_checked_before_the_password_is_sent(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    data: dict[str, Any],
    unique_id: str,
    expected: str | None,
) -> None:
    """The entry knows its charger's serial; the client checks hello against
    it before answering the authentication challenge. It used to learn the
    serial from whatever answered, so a wrong device got the password proof
    first and was refused only afterwards (audit A17-01). An entry still
    awaiting its serial has none to give."""
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=2, unique_id=unique_id)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wattpilot.hub.Wattpilot", return_value=fake_charger
    ) as constructor:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert constructor.call_args.kwargs["serial"] == expected


async def test_a_charger_refused_before_the_password_is_named(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Refused at hello, setup says which charger answered -- the same
    message as a refusal after connecting, not a generic connect failure."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    entry.add_to_hass(hass)
    fake_charger.connect_error = DeviceIdentityError("refused", found="999999")
    with patch_charger(fake_charger), pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry(hass, entry)
    assert raised.value.translation_domain == DOMAIN, "untranslated without it"
    assert raised.value.translation_key == "wrong_charger"
    # Masked: Home Assistant logs this message as well.
    assert raised.value.translation_placeholders == {
        "expected": "****56",
        "found": "****99",
    }


async def test_home_assistants_setup_messages_do_not_carry_the_serial(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The entry is titled after the charger's host name, Wattpilot_<serial>,
    on purpose, and Home Assistant logs that title on every failed setup
    attempt -- into this integration's own logger."""
    caplog.set_level(logging.DEBUG)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=V2_LOCAL_DATA,
        version=2,
        unique_id="123456",
        title="Wattpilot_123456",
    )
    fake_charger.connect_error = OSError("unreachable")

    assert not await setup_entry(hass, entry, fake_charger)
    # The retry loads the integration itself and logs to its logger instead.
    with patch_charger(fake_charger):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    retries = [
        record
        for record in caplog.records
        if record.name == "custom_components.wattpilot"
        and "Wattpilot_" in record.getMessage()
    ]
    assert retries, "the retry must log through this integration's logger"
    assert "Wattpilot_****56" in caplog.text, "the message itself must stay"
    assert "123456" not in caplog.text


async def test_an_entry_titled_by_its_address_does_not_log_it(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fork entry can be titled by its IP address, and Home Assistant logs
    the title on every failed setup; the mask knew only serials (reaudit
    RF-P-02)."""
    caplog.set_level(logging.DEBUG)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**V2_LOCAL_DATA, "host": "192.0.2.50"},
        version=2,
        unique_id="123456",
        title="192.0.2.50",
    )
    fake_charger.connect_error = OSError("unreachable")

    assert not await setup_entry(hass, entry, fake_charger)
    with patch_charger(fake_charger):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()

    assert "192.0.2.50" not in caplog.text
    assert "'<address>'" in caplog.text, "the message itself must stay"


async def test_an_entry_titled_by_a_name_keeps_it_in_the_log(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only an address is masked: a name means something else elsewhere."""
    caplog.set_level(logging.DEBUG)
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456", title="Garage"
    )
    fake_charger.connect_error = OSError("unreachable")

    assert not await setup_entry(hass, entry, fake_charger)

    assert "Garage" in caplog.text


async def test_a_setup_retry_does_not_log_the_chargers_address(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant logs each retry's traceback at DEBUG, the chained cause
    included -- and asyncio's refused connect quotes the address (R1-01)."""
    caplog.set_level(logging.DEBUG)
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    fake_charger.connect_error = ConnectionRefusedError(
        errno.ECONNREFUSED, "Connect call failed ('192.0.2.10', 80)"
    )

    assert not await setup_entry(hass, entry, fake_charger)
    with patch_charger(fake_charger):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()

    assert "Full exception" in caplog.text, "the traceback itself must stay"
    assert "ECONNREFUSED" in caplog.text, "the kind of error must stay"
    assert "192.0.2.10" not in caplog.text


def _record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        "custom_components.wattpilot", logging.INFO, "", 0, message, args, None
    )


async def test_a_record_without_a_serial_keeps_its_arguments(
    hass: HomeAssistant,
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="123456").add_to_hass(hass)
    record = _record("Config entry '%s' not ready", "Garage")

    assert _SerialMask(hass).filter(record)

    assert record.args == ("Garage",)


async def test_a_malformed_record_is_passed_on_untouched(hass: HomeAssistant) -> None:
    """Logging reports a broken record itself; the filter must not raise."""
    MockConfigEntry(domain=DOMAIN, unique_id="123456").add_to_hass(hass)
    record = _record("%s and %s", "only one")

    assert _SerialMask(hass).filter(record)

    assert record.msg == "%s and %s"


async def test_another_integrations_unique_id_is_left_alone(
    hass: HomeAssistant,
) -> None:
    """The filter sits on Home Assistant's config_entries logger, which every
    integration shares; only this integration's serials are its business."""
    MockConfigEntry(domain=DOMAIN, unique_id="123456").add_to_hass(hass)
    MockConfigEntry(domain="other", unique_id="777777").add_to_hass(hass)
    record = _record("Config entry '%s' not ready", "Other_777777")

    assert _SerialMask(hass).filter(record)

    assert record.getMessage() == "Config entry 'Other_777777' not ready"


async def test_an_entry_awaiting_its_serial_masks_nothing(
    hass: HomeAssistant,
) -> None:
    """An entry carried over from the earlier integration is keyed on its
    name or address until it connects. Masked as a serial, that name turned
    every other title holding it into `*******ot` (R1-17)."""
    MockConfigEntry(domain=DOMAIN, unique_id="Wattpilot").add_to_hass(hass)
    record = _record("Config entry '%s' not ready", "Wattpilot meter")

    assert _SerialMask(hass).filter(record)

    assert record.getMessage() == "Config entry 'Wattpilot meter' not ready"


async def test_a_setup_cancelled_by_the_stop_is_logged_masked(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant fires the stop event first and lets the setup retries
    it cancelled log their title afterwards. The mask went with the event,
    so that line carried the full serial (R1-05)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    logging.getLogger("custom_components.wattpilot").error(
        "Setup of config entry '%s' for wattpilot integration cancelled",
        "Wattpilot_123456",
    )

    assert "Wattpilot_****56" in caplog.text
    assert "123456" not in caplog.text


def _awaiting_fork_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50",
        version=2,
        data={**V2_LOCAL_DATA, CONF_AWAITING_SERIAL: True},
    )


async def test_an_ignored_card_does_not_block_the_serial_adoption(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Ignoring the card for the charger while its fork entry could not
    connect yet left an ignored entry holding the serial. Every setup after
    that refused the charger as configured elsewhere, with no other entry in
    sight, and the legacy history never migrated."""
    MockConfigEntry(
        domain=DOMAIN, source=config_entries.SOURCE_IGNORE, unique_id="123456"
    ).add_to_hass(hass)
    entry = _awaiting_fork_entry()

    assert await setup_entry(hass, entry, fake_charger)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == "123456"
    assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == [
        entry.entry_id
    ]


async def test_adopting_the_serial_closes_the_charger_s_open_card(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The card would stay offered for a charger that is configured now --
    and only that card: closing every flow of the domain passed too
    (reaudit RF-E-03)."""
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=DISCOVERY,
    )
    other = replace(
        DISCOVERY,
        ip_address=ip_address("192.168.1.61"),
        ip_addresses=[ip_address("192.168.1.61")],
        properties={**DISCOVERY.properties, "serial": "654321"},
    )
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=other
    )
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    entry = _awaiting_fork_entry()

    assert await setup_entry(hass, entry, fake_charger)

    left = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert sorted(flow["context"]["source"] for flow in left) == ["user", "zeroconf"]
    assert [
        flow["context"].get("unique_id")
        for flow in left
        if flow["context"]["source"] == "zeroconf"
    ] == ["654321"]


async def test_only_the_adopted_charger_s_ignored_card_is_removed(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Another charger's ignored card is still the owner's wish, and another
    integration's ignored entries are none of this one's business."""
    other_charger = MockConfigEntry(
        domain=DOMAIN, source=config_entries.SOURCE_IGNORE, unique_id="654321"
    )
    other_charger.add_to_hass(hass)
    other_integration = MockConfigEntry(
        domain="other", source=config_entries.SOURCE_IGNORE, unique_id="123456"
    )
    other_integration.add_to_hass(hass)

    assert await setup_entry(hass, _awaiting_fork_entry(), fake_charger)

    assert hass.config_entries.async_get_entry(other_charger.entry_id) is not None
    assert hass.config_entries.async_get_entry(other_integration.entry_id) is not None


async def _total_energy_after_restore(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    restored: int,
    reported: int | None,
) -> tuple[str, int | None]:
    """Set the entry up with *restored* Wh saved and *reported* Wh live."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    entry.add_to_hass(hass)
    entity_id = (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            DOMAIN,
            "123456-eto",
            config_entry=entry,
            suggested_object_id="wattpilot_total_energy",
        )
        .entity_id
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(entity_id, str(restored)),
                {"native_value": restored, "native_unit_of_measurement": "Wh"},
            )
        ],
    )
    fake_charger._properties["eto"] = reported

    with patch_charger(fake_charger):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entity_id, _total_energy_wh(hass, entity_id)


def _total_energy_wh(hass: HomeAssistant, entity_id: str) -> int | None:
    state = hass.states.get(entity_id)
    if state.state == "unknown":
        return None
    factor = 1000 if state.attributes["unit_of_measurement"] == "kWh" else 1
    return round(float(state.state) * factor)


async def test_total_energy_does_not_fall_across_a_reload(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """After a restart the charger reported 21 Wh less (replay fixture). The
    sensor held its value -- in memory only, so a reload in that window, as
    the one after a firmware update, reported the lower total and the energy
    dashboard got a negative hour (audit A18-25)."""
    _, total = await _total_energy_after_restore(
        hass, fake_charger, restored=2681214, reported=2681193
    )

    assert total == 2681214


async def test_a_restored_total_holds_a_step_back_down_to_its_limit(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Exactly at 90 % of the restored total it is still a step back."""
    _, total = await _total_energy_after_restore(
        hass, fake_charger, restored=1000, reported=900
    )

    assert total == 1000


async def test_a_restored_total_from_another_charger_does_not_freeze_it(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A replacement charger that took over the old entity ID restored the
    old charger's total and held it until its own passed it -- years at
    zero in the energy dashboard (R1-03). Only a step back as small as a
    restart's is held; a drop this large is a different meter."""
    entity_id, total = await _total_energy_after_restore(
        hass, fake_charger, restored=2681214, reported=1200
    )
    assert total == 1200

    fake_charger.push("eto", 1300)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert _total_energy_wh(hass, entity_id) == 1300

    # From the first live reading on, the hold is the strict one again.
    fake_charger.push("eto", 100)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()
    assert _total_energy_wh(hass, entity_id) == 1300
    assert not [
        record
        for record in caplog.records
        if record.name.startswith("custom_components.wattpilot")
        and record.levelno >= logging.WARNING
    ], "every reading must be read without complaint"


async def test_an_unknown_total_keeps_the_hold_against_a_step_back(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The hold compared against what was shown, so one null wiped it and
    the next small step back reached the energy dashboard (RF-D-02)."""
    entity_id, total = await _total_energy_after_restore(
        hass, fake_charger, restored=2681214, reported=2681214
    )
    assert total == 2681214

    for value in (None, 2681193):
        fake_charger.push("eto", value)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()

    assert _total_energy_wh(hass, entity_id) == 2681214


async def test_an_infinite_total_does_not_freeze_it(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The hold took infinity as the last number and held every later
    reading back until a reload (post-check PC5-R-01)."""
    entity_id, _ = await _total_energy_after_restore(
        hass, fake_charger, restored=2681214, reported=2681214
    )

    for value in (float("inf"), 2681300):
        fake_charger.push("eto", value)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()

    assert _total_energy_wh(hass, entity_id) == 2681300


async def test_a_restored_total_takes_the_first_number_after_an_unknown(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A restored total followed by a non-number dropped the next real
    reading, with a TypeError warning about the wrong key (post-check PC-03)."""
    entity_id, total = await _total_energy_after_restore(
        hass, fake_charger, restored=2681214, reported=None
    )
    assert total is None

    fake_charger.push("eto", 2681300)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()

    assert _total_energy_wh(hass, entity_id) == 2681300
    assert "TypeError" not in caplog.text


async def _average_power_while_charging(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> tuple[MockConfigEntry, str]:
    fake_charger._properties.update({"tpa": 4121.5, "nrg": nrg_reading(4100)})
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "123456-tpa")
    assert entity_id is not None
    return entry, entity_id


async def _live_power(
    hass: HomeAssistant, fake_charger: FakeWattpilot, watts: float, *, after: int
) -> None:
    """Report *watts* as the total power, then let *after* seconds pass."""
    fake_charger.push("nrg", nrg_reading(watts))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=after))
    await hass.async_block_till_done()


async def test_a_short_pause_keeps_the_average_power(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A phase switch stops the current for a moment, inside one session."""
    _, entity_id = await _average_power_while_charging(hass, fake_charger)

    await _live_power(hass, fake_charger, 0, after=50)
    assert hass.states.get(entity_id).state == "4121.5"

    await _live_power(hass, fake_charger, 4000, after=120)
    assert hass.states.get(entity_id).state == "4121.5", "the pause ended"

    await _live_power(hass, fake_charger, 0, after=5)
    await _live_power(hass, fake_charger, 0, after=60)
    assert hass.states.get(entity_id).state == "0", "a later stop still counts"


async def test_the_average_power_falls_to_zero_a_minute_after_charging_stops(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    _, entity_id = await _average_power_while_charging(hass, fake_charger)

    await _live_power(hass, fake_charger, 0, after=5)
    await _live_power(hass, fake_charger, 0, after=60)
    assert hass.states.get(entity_id).state == "0"

    await _live_power(hass, fake_charger, 4100, after=5)
    assert hass.states.get(entity_id).state == "0", "not the last session's average"
    fake_charger.push("tpa", 3800.5)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "3800.5", "the next session's own"
    await _live_power(hass, fake_charger, 0, after=5)
    await _live_power(hass, fake_charger, 0, after=60)
    assert hass.states.get(entity_id).state == "0", "and its end"


async def test_a_reading_without_total_power_keeps_the_last_sessions_average_away(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A push without a total power between sessions forgot which average
    was stale, and the next session's first watt showed it (post-check
    PC5-R-03)."""
    _, entity_id = await _average_power_while_charging(hass, fake_charger)
    await _live_power(hass, fake_charger, 0, after=5)
    await _live_power(hass, fake_charger, 0, after=60)
    assert hass.states.get(entity_id).state == "0"

    fake_charger.push("nrg", [])
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    await _live_power(hass, fake_charger, 4100, after=5)

    assert hass.states.get(entity_id).state == "0"


async def test_a_null_average_keeps_the_stale_one_known(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A null average in a new session forgot which average was the last
    session's, and it came back after (post-check PC7-01)."""
    _, entity_id = await _average_power_while_charging(hass, fake_charger)
    await _live_power(hass, fake_charger, 0, after=5)
    await _live_power(hass, fake_charger, 0, after=60)
    await _live_power(hass, fake_charger, 4100, after=5)

    for value in (None, 4121.5):
        fake_charger.push("tpa", value)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
        await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "0"


async def test_a_null_average_while_charging_is_unknown_not_zero(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """None stood for "nothing stale" too, so a null average while power
    flowed read 0 W (post-check PC6-03)."""
    _, entity_id = await _average_power_while_charging(hass, fake_charger)

    fake_charger.push("tpa", None)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "unknown"


async def test_a_total_power_that_is_no_number_is_not_idle(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A NaN total power counted as no current, and the average fell to
    0 W a minute later (post-check PC6-06)."""
    _, entity_id = await _average_power_while_charging(hass, fake_charger)

    await _live_power(hass, fake_charger, float("nan"), after=5)
    await _live_power(hass, fake_charger, float("nan"), after=65)

    assert hass.states.get(entity_id).state == "unknown"


async def test_an_average_that_is_no_number_mid_session_is_reported(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the last session's average is skipped silently, nothing else."""
    _, entity_id = await _average_power_while_charging(hass, fake_charger)

    fake_charger.push("tpa", "")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "4121.5"
    assert "unexpected str for tpa" in caplog.text


async def test_a_session_after_an_idle_start_waits_for_its_own_average(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """At the first watt the last session's average came back until the
    charger sent this session's (reaudit RF-D-04); also after an idle start."""
    fake_charger._properties.update({"tpa": 4121.5, "nrg": nrg_reading(0)})
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "123456-tpa")
    assert hass.states.get(entity_id).state == "0"

    await _live_power(hass, fake_charger, 4100, after=5)

    assert hass.states.get(entity_id).state == "0"


async def test_an_unloaded_entry_leaves_no_average_power_timer_behind(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The pending switch to 0 W goes with the entity; the test harness fails
    any test that ends with a timer still scheduled."""
    entry, _ = await _average_power_while_charging(hass, fake_charger)
    await _live_power(hass, fake_charger, 0, after=5)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# What a charger could send where a number belongs (reaudit K-A). Never seen
# on a device, but each one escaped the bounded catch: Home Assistant raised
# when it wrote the state, and at setup the entity was never added.
# NaN and infinity are what json.loads makes of NaN, Infinity and a number
# beyond the float range; Home Assistant refuses those too (post-check PC5-R-01).
NOT_A_NUMBER = {
    "text": "n/a",
    "object": {"bogus": "n/a"},
    "list": ["n/a"] * 16,
    "nan": float("nan"),
    "infinity": float("inf"),
    "minus infinity": float("-inf"),
    "infinities": [float("inf")] * 16,
    # An integer literal past the float range stays an int, and converting
    # it raises OverflowError (post-check PC6-01).
    "beyond_float": 10**400,
    # A list of lists: a select's option list keyed on it (post-check PC7-02).
    "nested": [["n/a"]] * 2,
}
HOLDER = "Holder Example"


# Every platform with a state: time and number raised OverflowError past
# their catch (post-check PC6-02), a select an unhashable error (PC7-02).
# The update entity has its own test: its installed version gates firmware.
PLATFORM_DESCRIPTIONS = {
    "sensor": list(SENSOR_DESCRIPTIONS),
    "number": list(NUMBER_DESCRIPTIONS),
    "time": list(TIME_DESCRIPTIONS),
    "select": list(SELECT_DESCRIPTIONS),
    "switch": list(SWITCH_DESCRIPTIONS),
    "binary_sensor": list(BINARY_SENSOR_DESCRIPTIONS),
}


@pytest.mark.parametrize("kind", NOT_A_NUMBER)
async def test_no_value_a_charger_sends_keeps_an_entity_away(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    """Every entity, every malformed shape, at the boundary that raised: the
    state write, or the entity's own setup. A card's holder name never
    reaches the log -- Home Assistant logged the raw card list when the
    write failed (RF-P-01)."""
    bad = NOT_A_NUMBER[kind]
    for descriptions in PLATFORM_DESCRIPTIONS.values():
        for description in descriptions:
            for key in (description.charger_key, description.companion_key):
                if key is not None and key in fake_charger._properties:
                    fake_charger._properties[key] = bad
    fake_charger._properties["cards"] = [{"name": HOLDER, "energy": bad}] * 10
    # A Flex's chips and power per source, which the fixture does not have
    # (post-checks PC5-T-01, F31-05).
    fake_charger._properties.update({f"c{i}e": bad for i in range(10)})
    fake_charger._properties.update(dict.fromkeys(("ws", "wb", "wg", "wo"), bad))
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity_ids = {
        (platform, description.uid_suffix): registry.async_get_or_create(
            platform, DOMAIN, f"123456-{description.uid_suffix}", config_entry=entry
        ).entity_id
        for platform, descriptions in PLATFORM_DESCRIPTIONS.items()
        for description in descriptions
    }

    assert await setup_entry(hass, entry, fake_charger)

    assert HOLDER not in caplog.text
    supported = {
        (platform, description.uid_suffix)
        for platform, descriptions in PLATFORM_DESCRIPTIONS.items()
        for description in _supported(fake_charger, descriptions)
    }
    never_added = sorted(
        key for key in supported if hass.states.get(entity_ids[key]) is None
    )
    platforms = {platform for platform, _ in supported}
    assert platforms == PLATFORM_DESCRIPTIONS.keys(), "the sweep missed a platform"
    assert not never_added, f"never added: {never_added}"


@pytest.mark.parametrize("kind", NOT_A_NUMBER)
async def test_a_card_energy_that_is_no_number_logs_no_holder_name(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    """Home Assistant logged the raw card list, holder names included, each
    time the state write of an enabled ID-chip sensor failed (RF-P-01)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "123456-cards_0", config_entry=entry
    )
    assert await setup_entry(hass, entry, fake_charger)

    fake_charger.push(
        "cards", [{"name": HOLDER, "energy": NOT_A_NUMBER[kind], "cardId": True}]
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()

    assert HOLDER not in caplog.text


def _supported(charger: FakeWattpilot, descriptions: list[Any]) -> list[Any]:
    from custom_components.wattpilot.descriptions import (  # noqa: PLC0415
        filter_supported,
    )

    return filter_supported(
        descriptions,
        firmware=charger._properties.get("fwv"),
        variant=charger._properties.get("var"),
        properties=charger._properties,
    )


async def test_a_failed_entry_whose_charger_announces_a_new_address_recovers(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    """Two chargers swapped addresses: this entry met the other charger and
    failed. Discovery stored the right address, but Home Assistant reloads
    only a loaded or retrying entry, so it stayed failed (reaudit RF-E-01)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    swapped = FakeWattpilot(device_properties)
    swapped.serial = "999999"
    assert not await setup_entry(hass, entry, swapped)
    assert entry.state is ConfigEntryState.SETUP_ERROR

    with patch_charger(FakeWattpilot(device_properties)):
        await start_discovery_flow(hass)
        await hass.async_block_till_done()

    assert entry.data["host"] == DISCOVERY.ip_address.compressed
    assert entry.state is ConfigEntryState.LOADED


async def test_a_failed_entry_is_not_reloaded_by_every_announcement(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    """Only a new address reloads: the charger announces itself again and
    again, and each reload of an entry that fails would fail once more."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**V2_LOCAL_DATA, "host": DISCOVERY.ip_address.compressed},
        version=2,
        unique_id="123456",
    )
    swapped = FakeWattpilot(device_properties)
    swapped.serial = "999999"
    assert not await setup_entry(hass, entry, swapped)

    with (
        patch_charger(swapped),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        await start_discovery_flow(hass)
        await hass.async_block_till_done()

    reload.assert_not_called()

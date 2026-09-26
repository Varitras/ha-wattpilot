"""Config flow: happy path, errors, duplicates, reauth, reconfigure."""

from __future__ import annotations

import json
from ipaddress import ip_address
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattpilot.api import AuthenticationError
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

USER_INPUT = {"host": "192.168.1.50", "password": "secret"}
MANIFEST = (
    Path(__file__).parent.parent / "custom_components" / "wattpilot" / "manifest.json"
)


def patch_charger(charger: FakeWattpilot) -> Any:
    return patch("custom_components.wattpilot.hub.Wattpilot", return_value=charger)


async def start_user_flow(hass: HomeAssistant) -> Any:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_creates_entry(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    result = await start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Wattpilot"
    assert result["data"] == {
        CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
        "host": "192.168.1.50",
        "password": "secret",
    }
    assert result["result"].unique_id == "123456"
    # The validation connection must not stay open.
    assert fake_charger.disconnect_count == 1


async def test_user_flow_invalid_auth_then_recovers(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    result = await start_user_flow(hass)
    fake_charger.connect_error = AuthenticationError("wrong password")
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    # The probe connection must be closed even on auth failure.
    assert fake_charger.disconnect_count == 1
    fake_charger.connect_error = None
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Second probe succeeds and also closes; total is two.
    assert fake_charger.disconnect_count == 2


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    result = await start_user_flow(hass)
    fake_charger.connect_error = ConnectionError("no route")
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    # The probe connection must be closed even on transport failure.
    assert fake_charger.disconnect_count == 1


async def test_user_flow_aborts_on_duplicate(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="123456", data={}).add_to_hass(hass)
    result = await start_user_flow(hass)
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        version=2,
        data={CONF_CONNECTION_TYPE: CONNECTION_LOCAL, **USER_INPUT},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "newpass"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "newpass"  # noqa: S105 -- test value, not a secret


async def test_reauth_rejects_a_different_charger(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    """Reauth proves a password, and it has to prove it against the right
    charger. It discarded the serial the probe returned, so credentials
    accepted by whatever now answers at the address were written into the
    entry and the flow reported success -- while the setup that followed
    refused the device, leaving the entry broken with its password already
    replaced (audit A12-07). Sibling of the setup and reconnect checks.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="999999",  # a different charger than the fake (123456)
        version=2,
        data={CONF_CONNECTION_TYPE: CONNECTION_LOCAL, **USER_INPUT},
    )
    entry.add_to_hass(hass)
    original_data = dict(entry.data)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    with patch_charger(FakeWattpilot(device_properties)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "newpass"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_device"}
    assert entry.data == original_data, "the password was replaced anyway"


async def test_reconfigure_rejects_wrong_device(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="999999",  # a different charger than the fake (123456)
        version=2,
        data={CONF_CONNECTION_TYPE: CONNECTION_LOCAL, **USER_INPUT},
    )
    entry.add_to_hass(hass)
    # Capture the original entry state before the flow.
    original_data = dict(entry.data)
    original_unique_id = entry.unique_id
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    with patch_charger(FakeWattpilot(device_properties)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.60", "password": "secret"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_device"}
    # Entry must remain unchanged when rejecting a different charger.
    assert entry.data == original_data
    assert entry.unique_id == original_unique_id


async def test_reconfigure_rescues_a_fork_entry_whose_address_changed(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    A migrated fork entry is keyed on its old IP and must still be reachable.

    The v1 migration is network-free, so the entry keeps the fork's unique_id
    until a connect succeeds. If the address changed while Home Assistant was
    off, that connect never happens -- and reconfigure, the documented way to
    move an entry to a new address without losing history, compared the real
    serial against an IP and rejected the charger as a stranger. The one path
    out of a DHCP change was the one path blocked.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50",  # the fork's key: the old address
        version=2,
        data={
            CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
            CONF_AWAITING_SERIAL: True,
            **USER_INPUT,
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.60", "password": "secret"}
        )
        # Inside the patch on purpose: the abort schedules the reload rather
        # than awaiting it, so the setup it triggers must still find the fake
        # charger -- and must have finished before the entry is inspected.
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "192.168.1.60"
    # Reconfigure reloads the entry, so the setup that follows reaches the
    # charger and finally does what the missed connect could not: the entry
    # stops being keyed on an address and takes its serial, spending the
    # one-time permission on the way.
    assert entry.unique_id == "123456"
    assert CONF_AWAITING_SERIAL not in entry.data


async def test_reconfigure_turns_a_cloud_entry_into_a_local_one(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    Setup tells cloud users to reconfigure, so reconfigure has to actually fix it.

    A migrated cloud entry fails setup with "cloud connections are not
    supported yet, reconfigure with the local address". Reconfigure used to
    write only host and password, leaving connection_type on cloud -- so the
    reload it triggers hit the very same refusal. The documented way out was
    a loop with no exit.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="cloud-key-from-the-fork",
        version=2,
        data={CONF_CONNECTION_TYPE: CONNECTION_CLOUD, "serial": "123456"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()  # see the note above
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_CONNECTION_TYPE] == CONNECTION_LOCAL
    assert entry.data["host"] == USER_INPUT["host"]
    # The reload this triggers must now get past async_setup_entry.
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_updates_host(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        version=2,
        data={CONF_CONNECTION_TYPE: CONNECTION_LOCAL, **USER_INPUT},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.60", "password": "secret"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "192.168.1.60"


async def test_options_flow_sets_the_update_interval(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The options flow writes the chosen interval into entry.options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
            "host": "192.168.1.50",
            "password": "secret",
        },
        version=2,
        unique_id="123456",
    )
    entry.add_to_hass(hass)
    with patch_charger(fake_charger):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_UPDATE_INTERVAL: 10}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_UPDATE_INTERVAL] == 10


async def test_probe_shutdown_failure_does_not_mask_success(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    The probe connection is disposable. A failure closing it must not turn a
    successful validation into an error: the cleanup runs in a finally, so its
    exception would otherwise replace the entry the user just created.
    """
    fake_charger.disconnect_error = ConnectionError("socket closed")
    result = await start_user_flow(hass)
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The flow result is the whole point here; let the created entry tear down
    # cleanly rather than tripping the same disconnect error again on unload.
    fake_charger.disconnect_error = None


async def test_probe_shutdown_failure_does_not_mask_auth_error(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    The same finally must not bury the auth error the user actually needs: a
    disconnect failure on top of a wrong password has to still surface as
    invalid_auth, not as an opaque disconnect error.
    """
    fake_charger.connect_error = AuthenticationError("wrong password")
    fake_charger.disconnect_error = ConnectionError("socket closed")
    result = await start_user_flow(hass)
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


# What a charger on firmware 42.5 announces, measured (serial and address
# replaced): the instance name is the owner's friendly name, so only the TXT
# properties identify it -- and the link-local IPv6 comes first.
DISCOVERY = ZeroconfServiceInfo(
    ip_address=ip_address("192.168.1.60"),
    ip_addresses=[ip_address("fe80::1"), ip_address("192.168.1.60")],
    port=80,
    hostname="Wattpilot_123456.local.",
    type="_http._tcp.local.",
    name="Wattpilot._http._tcp.local.",
    properties={
        "proto": "3",
        "protocol": "2",
        "version": "42.5",
        "devicetype": "wattpilot_V2",
        "devicefamily": "wattpilot",
        "manufacturer": "fronius",
        "friendly_name": "Wattpilot",
        "serial": "123456",
    },
)


async def start_discovery_flow(
    hass: HomeAssistant, discovery: ZeroconfServiceInfo = DISCOVERY
) -> Any:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=discovery
    )


async def test_a_discovered_charger_only_asks_for_the_password(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    result = await start_discovery_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "secret"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
        "host": "192.168.1.60",
        "password": "secret",
    }
    assert result["result"].unique_id == "123456"


async def test_a_known_charger_announcing_a_new_address_updates_the_entry(
    hass: HomeAssistant,
) -> None:
    """DHCP moved the charger: the entry follows instead of trying the old
    address -- where another device may answer by now (audit A15-04)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        version=2,
        data={CONF_CONNECTION_TYPE: CONNECTION_LOCAL, **USER_INPUT},
    )
    entry.add_to_hass(hass)
    result = await start_discovery_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.1.60"
    assert entry.data["password"] == "secret"  # noqa: S105 -- test value


async def test_a_charger_without_ipv4_is_not_offered(hass: HomeAssistant) -> None:
    """The client connects to ws://<host>/ws, which an IPv6 address does not
    fit, and the charger's own IPv6 is link-local anyway."""
    only_ipv6 = ZeroconfServiceInfo(
        ip_address=ip_address("2001:db8::5"),
        ip_addresses=[ip_address("fe80::1"), ip_address("2001:db8::5")],
        port=80,
        hostname=DISCOVERY.hostname,
        type=DISCOVERY.type,
        name=DISCOVERY.name,
        properties=DISCOVERY.properties,
    )
    result = await start_discovery_flow(hass, only_ipv6)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_ipv4_address"


async def test_the_answering_charger_must_be_the_announced_one(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    """The announcement is unauthenticated; the entry is keyed on the serial
    the charger proves after the password, not on the one it claimed."""
    claims_another = ZeroconfServiceInfo(
        ip_address=DISCOVERY.ip_address,
        ip_addresses=DISCOVERY.ip_addresses,
        port=80,
        hostname=DISCOVERY.hostname,
        type=DISCOVERY.type,
        name=DISCOVERY.name,
        properties={**DISCOVERY.properties, "serial": "999999"},
    )
    result = await start_discovery_flow(hass, claims_another)
    with patch_charger(FakeWattpilot(device_properties)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "secret"}
        )
    # Retrying the password cannot help: the announcement was wrong.
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "announcement_mismatch"


def test_the_manifest_matcher_fits_the_measured_announcement() -> None:
    """Home Assistant only starts the flow if the manifest matches; the
    instance name is the owner's choice, so the match is on TXT properties."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    (matcher,) = manifest["zeroconf"]
    assert matcher["type"] == DISCOVERY.type
    for key, value in matcher["properties"].items():
        assert DISCOVERY.properties[key].lower() == value


async def test_an_announcement_without_a_serial_is_not_offered(
    hass: HomeAssistant,
) -> None:
    """The serial is what a discovered charger is recognised by later."""
    properties = {k: v for k, v in DISCOVERY.properties.items() if k != "serial"}
    no_serial = ZeroconfServiceInfo(
        ip_address=DISCOVERY.ip_address,
        ip_addresses=DISCOVERY.ip_addresses,
        port=80,
        hostname=DISCOVERY.hostname,
        type=DISCOVERY.type,
        name=DISCOVERY.name,
        properties=properties,
    )
    result = await start_discovery_flow(hass, no_serial)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "incomplete_discovery"


async def test_a_discovery_card_does_not_block_adding_by_hand(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Someone who ignores the card and adds the charger by hand must get
    it: the card waiting on the same serial made the manual flow abort as
    already in progress. The card goes once the entry exists."""
    card = await start_discovery_flow(hass)
    result = await start_user_flow(hass)
    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert card["flow_id"] not in {
        flow["flow_id"] for flow in hass.config_entries.flow.async_progress()
    }


async def test_an_entry_set_up_by_name_keeps_its_name(hass: HomeAssistant) -> None:
    """The name already follows the charger to its new address; replacing it
    with today's IP would give that up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        version=2,
        data={
            CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
            "host": "wattpilot.home.arpa",
            "password": "secret",
        },
    )
    entry.add_to_hass(hass)
    result = await start_discovery_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "wattpilot.home.arpa"


def announcement(**changes: Any) -> ZeroconfServiceInfo:
    """DISCOVERY with some fields replaced."""
    fields = {
        "ip_address": DISCOVERY.ip_address,
        "ip_addresses": DISCOVERY.ip_addresses,
        "port": 80,
        "hostname": DISCOVERY.hostname,
        "type": DISCOVERY.type,
        "name": DISCOVERY.name,
        "properties": DISCOVERY.properties,
    }
    return ZeroconfServiceInfo(**{**fields, **changes})


async def test_a_card_finished_after_the_entry_learned_its_serial_changes_nothing(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """A fork entry learns its serial only on its first connect, so a card for
    the same charger can be open when it does. Finishing the card then made
    a second entry with the same unique id -- and Home Assistant resolves
    that by deleting the older one, entities and all."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50",
        version=2,
        data={
            CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
            **USER_INPUT,
            CONF_AWAITING_SERIAL: True,
        },
    )
    entry.add_to_hass(hass)
    card = await start_discovery_flow(hass)
    assert card["type"] is FlowResultType.FORM
    hass.config_entries.async_update_entry(entry, unique_id="123456")

    with patch_charger(fake_charger):
        result = await hass.config_entries.flow.async_configure(
            card["flow_id"], {"password": "secret"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert hass.config_entries.async_get_entry(entry.entry_id) is not None


async def test_a_connected_entry_does_not_move_to_another_announced_address(
    hass: HomeAssistant,
) -> None:
    """Anyone on the network can announce the charger's serial -- the charger
    broadcasts it. While the entry is talking to its charger, that is proof
    of where it is, and an announcement elsewhere is stale or foreign."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        version=2,
        data={CONF_CONNECTION_TYPE: CONNECTION_LOCAL, **USER_INPUT},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(available=True)
    result = await start_discovery_flow(hass)
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.1.50"
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)  # no hub to unload


async def test_an_entry_that_lost_its_charger_follows_it_and_reloads(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="123456",
        version=2,
        data={CONF_CONNECTION_TYPE: CONNECTION_LOCAL, **USER_INPUT},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(available=False)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await start_discovery_flow(hass)
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.1.60"
    reload.assert_called_once_with(entry.entry_id)
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)  # no hub to unload


async def test_a_link_local_ipv4_is_not_taken(hass: HomeAssistant) -> None:
    """169.254.x.x is what a device gives itself without DHCP; Home
    Assistant skips it for ip_address, and so does the flow."""
    result = await start_discovery_flow(
        hass,
        announcement(
            ip_addresses=[ip_address("169.254.3.4"), ip_address("192.168.1.60")]
        ),
    )
    assert result["description_placeholders"] == {"host": "192.168.1.60"}


async def test_the_card_names_the_charger_and_its_address(
    hass: HomeAssistant,
) -> None:
    result = await start_discovery_flow(
        hass,
        announcement(properties={**DISCOVERY.properties, "friendly_name": "Garage"}),
    )
    assert result["description_placeholders"] == {"host": "192.168.1.60"}
    (flow,) = hass.config_entries.flow.async_progress()
    assert flow["context"]["title_placeholders"] == {"name": "Garage"}


async def test_a_charger_announced_without_a_name_is_still_named(
    hass: HomeAssistant,
) -> None:
    """A TXT key can be present with no value; the card must not go blank."""
    await start_discovery_flow(
        hass,
        announcement(properties={**DISCOVERY.properties, "friendly_name": None}),
    )
    (flow,) = hass.config_entries.flow.async_progress()
    assert flow["context"]["title_placeholders"] == {"name": "Wattpilot"}

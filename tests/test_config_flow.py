"""Config flow: happy path, errors, duplicates, reauth, reconfigure."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
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

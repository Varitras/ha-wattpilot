"""Config flow: local connection via host + password."""

from __future__ import annotations

from ipaddress import IPv4Address, ip_address
from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_AWAITING_SERIAL,
    CONF_CONNECTION_TYPE,
    CONF_UPDATE_INTERVAL,
    CONNECTION_LOCAL,
    DEFAULT_UPDATE_INTERVAL,
    DEVICE_NAME,
    DOMAIN,
    awaits_serial,
    entry_awaiting_serial_at,
    new_device_name,
    reload_failed_entry,
    serial_taken_by_other_entry,
)
from .hub import WattpilotHub

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

USER_SCHEMA = probatio.Schema(
    {probatio.Required("host"): str, probatio.Required("password"): str}
)
PASSWORD_SCHEMA = probatio.Schema({probatio.Required("password"): str})
OPTIONS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_UPDATE_INTERVAL): NumberSelector(
            NumberSelectorConfig(min=0, max=60, step=1, mode=NumberSelectorMode.BOX)
        )
    }
)


def _is_ipv4(host: str) -> bool:
    try:
        return isinstance(ip_address(host), IPv4Address)
    except ValueError:
        return False


def _announced_ipv4(discovery_info: ZeroconfServiceInfo) -> str | None:
    """
    Return the announced IPv4 address, if there is a usable one.

    The client connects to ws://<host>/ws, which an IPv6 address does not
    fit; the charger's own IPv6 is link-local anyway (measured). A
    link-local IPv4 is skipped the way Home Assistant skips it.
    """
    return next(
        (
            str(address)
            for address in discovery_info.ip_addresses
            if isinstance(address, IPv4Address) and not address.is_link_local
        ),
        None,
    )


def _address_update(known: ConfigEntry | None, ipv4: str) -> dict[str, str] | None:
    """
    Return what a known charger's entry takes from its announcement.

    The new address, with two exceptions. An entry set up by name keeps it:
    the name follows the charger by itself. And an entry that is talking to
    its charger right now stays where it is: anyone on the network can
    announce the serial, which the charger broadcasts, and a live
    connection is better evidence than an unauthenticated announcement.
    """
    if known is None or not _is_ipv4(known.data.get("host", "")):
        return None
    if known.state is ConfigEntryState.LOADED and known.runtime_data.available:
        return None
    return {"host": ipv4}


# What the form says for each way a probe fails. The hub raises
# ConfigEntryError only for another charger's serial. A ValueError is an
# address that cannot be built (an unbracketed IPv6 address, a bad port, an
# empty label), which the form showed as "Unknown error".
PROBE_ERRORS: dict[type[Exception], dict[str, str]] = {
    ConfigEntryError: {"base": "wrong_device"},
    ConfigEntryAuthFailed: {"base": "invalid_auth"},
    ConfigEntryNotReady: {"base": "cannot_connect"},
    ValueError: {"host": "invalid_host"},
}


class WattpilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the local setup, reauth and reconfigure flows."""

    VERSION = 2

    _discovered_host: str

    @staticmethod
    @callback
    def async_get_options_flow(_entry: ConfigEntry) -> WattpilotOptionsFlow:
        """Expose the update-interval option."""
        return WattpilotOptionsFlow()

    async def _async_validate(
        self, host: str, password: str, serial: str | None = None
    ) -> tuple[dict[str, str], str]:
        """Try to connect; return (errors, serial). The probe is always closed."""
        hub = WattpilotHub.create_local(
            self.hass, "config_flow", host, password, serial=serial
        )
        try:
            await hub.async_connect()
        except tuple(PROBE_ERRORS) as err:
            return next(
                form for kind, form in PROBE_ERRORS.items() if isinstance(err, kind)
            ), ""
        finally:
            # Disposable: a failure closing it never replaces the result.
            await hub.async_shutdown()
        return {}, hub.serial

    def _identity_error(self, entry: ConfigEntry, serial: str) -> str | None:
        """
        Name what is wrong with moving this entry onto that charger, if anything.

        An entry still awaiting its serial accepts any charger -- but not
        one that has an entry of its own: the setup after the flow would
        refuse it as a duplicate, with host and password already overwritten.
        """
        if not awaits_serial(entry) and serial != entry.unique_id:
            return "wrong_device"
        if serial_taken_by_other_entry(self.hass, entry, serial):
            return "charger_has_entry"
        return None

    async def _async_validate_for(
        self, entry: ConfigEntry, host: str, password: str
    ) -> dict[str, str]:
        """
        Probe for an existing entry, which only its own charger may take.

        A known serial is checked before the password is sent, as setup does:
        another charger is the wrong device, not a wrong password.
        """
        known = None if awaits_serial(entry) else entry.unique_id
        errors, serial = await self._async_validate(host, password, known)
        identity = None if errors else self._identity_error(entry, serial)
        return {"base": identity} if identity else errors

    def _create_local_entry(self, host: str, password: str) -> ConfigFlowResult:
        name = new_device_name(self.hass)
        return self.async_create_entry(
            title=name,
            data={
                CONF_NAME: name,
                CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
                "host": host,
                "password": password,
            },
        )

    @staticmethod
    def _local_entry_data(
        entry: ConfigEntry, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the entry data a verified local connection deserves."""
        data: dict[str, Any] = {
            CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
            "host": user_input["host"],
            "password": user_input["password"],
        }
        # Reconfigure proves the address, never the identity: it does not
        # write unique_id. So an entry that still has to learn its serial
        # keeps that permission for the setup that follows.
        if awaits_serial(entry):
            data[CONF_AWAITING_SERIAL] = True
        if CONF_NAME in entry.data:  # else the device falls back to the host name
            data[CONF_NAME] = entry.data[CONF_NAME]
        return data

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step: host and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._abort_if_awaiting_entry_at(user_input["host"])
            errors, serial = await self._async_validate(
                user_input["host"], user_input["password"]
            )
            if not errors:
                # Not raise_on_progress: a discovery card for this charger
                # must not stop adding it by hand; it goes once the entry exists.
                await self.async_set_unique_id(serial, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self._create_local_entry(
                    user_input["host"], user_input["password"]
                )
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a charger announcing itself: only the password is left."""
        ipv4 = _announced_ipv4(discovery_info)
        if ipv4 is None:
            return self.async_abort(reason="no_ipv4_address")
        serial = discovery_info.properties.get("serial")
        if not serial:
            return self.async_abort(reason="incomplete_discovery")
        known = await self.async_set_unique_id(serial)
        updates = _address_update(known, ipv4)
        reload_failed_entry(self.hass, known, updates)
        self._abort_if_unique_id_configured(updates=updates)
        self._abort_if_awaiting_entry_at(ipv4)
        self._discovered_host = ipv4
        # A TXT key can be present without a value.
        self.context["title_placeholders"] = {
            "name": discovery_info.properties.get("friendly_name") or DEVICE_NAME
        }
        return await self.async_step_zeroconf_confirm()

    def _abort_if_awaiting_entry_at(self, host: str) -> None:
        """
        Abort for an entry at this address that has yet to learn its serial.

        Keyed on its address until it connects, it was not found by the
        serial, and an entry made from a card or the form locked it out.
        shortcut: the address is compared as written, so a host name
        against an IP goes unnoticed; resolve both if that occurs.
        """
        awaiting = entry_awaiting_serial_at(self.hass, host)
        if awaiting is None:
            return
        if awaiting.state is ConfigEntryState.SETUP_RETRY:
            # Its charger is in reach right now: no need to wait for the retry.
            self.hass.config_entries.async_schedule_reload(awaiting.entry_id)
        raise AbortFlow("already_configured")

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password of the discovered charger."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, serial = await self._async_validate(
                self._discovered_host, user_input["password"]
            )
            # The announcement is unauthenticated: the entry is keyed on the
            # serial the charger reports once connected, and a mismatch is
            # not something another password could fix.
            if not errors and serial != self.unique_id:
                return self.async_abort(reason="announcement_mismatch")
            if not errors:
                # Again: a fork entry may have learned this serial while the
                # card was open, and a second entry with it would make Home
                # Assistant delete the first.
                self._abort_if_unique_id_configured()
                return self._create_local_entry(
                    self._discovered_host, user_input["password"]
                )
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=PASSWORD_SCHEMA,
            errors=errors,
            description_placeholders={"host": self._discovered_host},
        )

    async def async_step_ignore(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """
        Ignore a card -- unless its charger has an entry by now.

        A fork entry learns its serial only once it connects, so a card for
        the same charger can still be open. Home Assistant resolves an
        ignored card with that unique_id by replacing the entry with it,
        entities and entity ids gone.
        """
        await self.async_set_unique_id(user_input["unique_id"], raise_on_progress=False)
        self._abort_if_unique_id_configured()
        return await super().async_step_ignore(user_input)

    async def async_step_reauth(
        self, _entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth: only the charger's password may have changed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password only."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            errors = await self._async_validate_for(
                entry, entry.data["host"], user_input["password"]
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={"password": user_input["password"]}
                )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=PASSWORD_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Handle reconfigure: host may have changed; reject a different charger.

        Rejecting is only right for an entry that knows which charger it
        belongs to. An entry still awaiting its serial would have its real
        serial compared against the fork's key -- an IP address, or a cloud
        identifier -- and every charger on earth would read as the wrong one.

        The entry is rewritten rather than patched: a reconfigure always
        produces a local entry, so a migrated cloud entry loses both its
        connection type and its now-meaningless cloud fields here. Leaving
        the type on "cloud" made the reload this triggers fail with the very
        error that sent the user to reconfigure in the first place.
        """
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            errors = await self._async_validate_for(
                entry, user_input["host"], user_input["password"]
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data=self._local_entry_data(entry, user_input)
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {"host": entry.data.get("host", "")}
            ),
            errors=errors,
        )


class WattpilotOptionsFlow(OptionsFlowWithReload):
    """Change the update interval; OptionsFlowWithReload reloads the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the interval, store the choice."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, {CONF_UPDATE_INTERVAL: current}
            ),
        )

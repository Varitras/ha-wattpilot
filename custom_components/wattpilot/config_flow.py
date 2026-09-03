"""Config flow: local connection via host + password."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
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
    DOMAIN,
    awaits_serial,
)
from .hub import WattpilotHub

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema({vol.Required("host"): str, vol.Required("password"): str})
PASSWORD_SCHEMA = vol.Schema({vol.Required("password"): str})
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_UPDATE_INTERVAL): NumberSelector(
            NumberSelectorConfig(min=0, max=60, step=1, mode=NumberSelectorMode.BOX)
        )
    }
)


class WattpilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the local setup, reauth and reconfigure flows."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(_entry: ConfigEntry) -> WattpilotOptionsFlow:
        """Expose the update-interval option."""
        return WattpilotOptionsFlow()

    async def _async_validate(
        self, host: str, password: str
    ) -> tuple[dict[str, str], str, str]:
        """
        Try to connect; return (errors, serial, device name).

        The probe connection is always closed before returning -- on the
        success path as well as every failure path -- via the `finally`,
        which is the one point every path through this function converges
        on, rather than repeating the shutdown call in each branch.
        """
        hub = WattpilotHub.create_local(self.hass, "config_flow", host, password)
        try:
            await hub.async_connect()
        except ConfigEntryAuthFailed:
            return {"base": "invalid_auth"}, "", ""
        except ConfigEntryNotReady:
            return {"base": "cannot_connect"}, "", ""
        finally:
            # The probe connection is disposable; a failure closing it must
            # not replace the validation result -- a success, or the auth/
            # connect error the user needs to see -- with a disconnect error.
            try:
                await hub.async_shutdown()
            except HomeAssistantError:
                _LOGGER.debug(
                    "Ignoring error while closing the probe connection",
                    exc_info=True,
                )
        return {}, hub.serial, hub.name

    @staticmethod
    def _is_another_charger(entry: ConfigEntry, serial: str) -> bool:
        """Whether the connected charger contradicts the entry's own identity."""
        return not awaits_serial(entry) and serial != entry.unique_id

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
        return data

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step: host and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, serial, name = await self._async_validate(
                user_input["host"], user_input["password"]
            )
            if not errors:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
                        "host": user_input["host"],
                        "password": user_input["password"],
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

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
            errors, _serial, _name = await self._async_validate(
                entry.data["host"], user_input["password"]
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
            errors, serial, _name = await self._async_validate(
                user_input["host"], user_input["password"]
            )
            if not errors and self._is_another_charger(entry, serial):
                errors = {"base": "wrong_device"}
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

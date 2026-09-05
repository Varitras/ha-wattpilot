"""The Fronius Wattpilot integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    CONF_AWAITING_SERIAL,
    CONF_CONNECTION_TYPE,
    CONF_UPDATE_INTERVAL,
    CONNECTION_CLOUD,
    CONNECTION_LOCAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PLATFORMS,
    awaits_serial,
    signal_property,
)
from .hub import WattpilotConfigEntry, WattpilotHub
from .registry_migration import async_migrate_legacy_unique_ids
from .services import async_setup_services

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

ENTRY_VERSION = 2

# async_setup exists only to register the actions, and there is nothing to
# configure in YAML. Saying so explicitly is what hassfest asks of every
# integration that implements async_setup, and it makes a `wattpilot:` block
# in configuration.yaml an error instead of a silently ignored line.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register integration services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: WattpilotConfigEntry) -> bool:
    """Connect the charger and set up platforms."""
    # No default: the question is only whether this entry is a cloud one,
    # and a missing key is not. Spelling out a default that cannot change
    # the answer invited two mutants nobody could ever kill.
    if entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_CLOUD:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="cloud_not_supported"
        )

    update_interval = timedelta(
        seconds=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
    )
    hub = WattpilotHub.create_local(
        hass,
        entry.entry_id,
        entry.data["host"],
        entry.data["password"],
        update_interval,
    )
    await hub.async_connect()

    # From here the hub owns a live connection, a push callback and timers.
    # Home Assistant calls async_unload_entry only for an entry that finished
    # setup, so every failure below has to hand them back itself -- otherwise
    # the entry sits in SETUP_ERROR with the connection still open, and the
    # next retry puts a second hub on the same dispatcher signals (audit
    # VA-02, measured through the config entry manager).
    #
    # BaseException, not Exception: Home Assistant cancels a setup that runs
    # too long, and CancelledError is not an Exception -- the narrower catch
    # handed nothing back on that path (audit A11-05).
    try:
        await _finish_setup(hass, entry, hub)
    except BaseException:
        await hub.async_shutdown()
        raise
    return True


def _reload_when_firmware_changes(
    hass: HomeAssistant, entry: WattpilotConfigEntry, hub: WattpilotHub
) -> None:
    """
    Rebuild the entry when the charger reports a different firmware.

    Two things are decided once and never revisited: the platforms filter
    their description tables at setup, and an entity freezes the device's
    sw_version when it is built. So a firmware jump leaves the newly
    supported entities missing and the device page showing the old version
    until someone reloads by hand (audit VA-05). A reload redoes both.
    """
    known = hub.firmware

    @callback
    def _on_firmware(value: object) -> None:
        nonlocal known
        if not isinstance(value, str) or value == known:
            return
        known = value
        _LOGGER.info("Charger firmware changed to %s, reloading", value)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, signal_property(entry.entry_id, "fwv"), _on_firmware
        )
    )


async def _finish_setup(
    hass: HomeAssistant, entry: WattpilotConfigEntry, hub: WattpilotHub
) -> None:
    """
    Everything that runs with a connected hub.

    Split out so its caller can put a single cleanup guard around all of it.
    """
    serial = hub.serial
    if entry.unique_id != serial:
        await _adopt_serial(hass, entry, hub, serial)
    await async_migrate_legacy_unique_ids(hass, entry, serial, hub.variant)

    entry.runtime_data = hub
    # Before the platforms, not after. The charger keeps pushing throughout
    # setup, and an entity takes its initial value once, as it is added --
    # so a push arriving between that read and a later start_dispatch()
    # reaches nobody and leaves the entity stale until the device repeats
    # itself. Starting first cannot lose anything instead: entities not yet
    # added simply read the fresher cache when they arrive, and each one
    # subscribes before it reads.
    hub.start_dispatch()
    _reload_when_firmware_changes(hass, entry, hub)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _on_ha_stop(_event: Event) -> None:
        await hub.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
    )


async def _adopt_serial(
    hass: HomeAssistant, entry: WattpilotConfigEntry, hub: WattpilotHub, serial: str
) -> None:
    """
    Take the charger's serial as the entry's unique_id -- once, and only once.

    An entry that has not learned its serial yet (see awaits_serial) may take
    whatever the charger reports. Any other entry already knows its serial,
    so a charger reporting a different one IS a different charger: the address
    was reused (DHCP) or the hardware was replaced. Adopting it would hand
    this entry's entities, entity ids and recorder history to that other
    device -- and because async_migrate_legacy_unique_ids matches on the uid
    suffix, it would rewrite even the correct `<old serial>-<suffix>` ids. No
    error, no warning, just plausible readings from the wrong charger forever.
    """
    if not awaits_serial(entry):
        await hub.async_shutdown()
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="wrong_charger",
            translation_placeholders={
                "expected": str(entry.unique_id),
                "found": serial,
            },
        )
    if _serial_taken_by_other_entry(hass, entry, serial):
        await hub.async_shutdown()
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="duplicate_device",
            translation_placeholders={"serial": serial},
        )
    hass.config_entries.async_update_entry(
        entry,
        unique_id=serial,
        data={
            key: value
            for key, value in entry.data.items()
            if key != CONF_AWAITING_SERIAL
        },
    )


def _serial_taken_by_other_entry(
    hass: HomeAssistant, entry: ConfigEntry, serial: str
) -> bool:
    """
    Return whether another entry already claims this charger's serial.

    A second entry for the same charger would fight over the device.
    """
    return any(
        other.entry_id != entry.entry_id and other.unique_id == serial
        for other in hass.config_entries.async_entries(DOMAIN)
    )


async def async_unload_entry(hass: HomeAssistant, entry: WattpilotConfigEntry) -> bool:
    """Unload platforms and disconnect."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Migrate fork (v1) entries to the v2 schema; drop-in replacement.

    Network-free by design: the serial that both fork installs are keyed to
    (config-entry unique_id) is only known after connecting, so that upgrade
    happens later, in async_setup_entry.
    """
    if entry.version > ENTRY_VERSION:
        return False  # downgrade from a future version: not supported
    if entry.version == 1:
        old = entry.data
        # CONF_AWAITING_SERIAL on the local branch only: that entry is keyed
        # on the fork's unique_id (an IP, a friendly name, or nothing), so the
        # first successful connect may replace it with the serial -- see
        # _adopt_serial. A cloud entry never gets that far, because setup
        # refuses it outright, so the flag would be unreachable and untestable
        # here. Whoever makes cloud entries reconfigurable to local adds it
        # then, together with the test that makes it observable.
        if old.get("connection") == "cloud":
            new_data = {
                CONF_CONNECTION_TYPE: CONNECTION_CLOUD,
                "serial": old["serial"],
                "password": old["password"],
            }
        else:
            new_data = {
                CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
                "host": old["ip_address"],
                "password": old["password"],
                CONF_AWAITING_SERIAL: True,
            }
        hass.config_entries.async_update_entry(
            entry, data=new_data, version=ENTRY_VERSION
        )
        _LOGGER.info("Migrated fork config entry %s to version 2", entry.entry_id)
    return True

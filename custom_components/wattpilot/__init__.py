"""The Fronius Wattpilot integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from ipaddress import ip_address
from typing import TYPE_CHECKING

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
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
    new_device_name,
    serial_taken_by_other_entry,
    signal_property,
)
from .hub import WattpilotConfigEntry, WattpilotHub, mask_serial
from .registry_migration import async_clear_collisions, async_migrate_legacy_unique_ids
from .services import async_setup_services

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)
# Where Home Assistant logs an entry's setup failures, title included.
_ENTRY_LOGGERS = (logging.getLogger(config_entries.__name__), _LOGGER)

ENTRY_VERSION = 2

# async_setup exists only to register the actions, and there is nothing to
# configure in YAML. Saying so explicitly is what hassfest asks of every
# integration that implements async_setup, and it makes a `wattpilot:` block
# in configuration.yaml an error instead of a silently ignored line.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


class _SerialMask(logging.Filter):
    """
    Mask this integration's serials and address titles in HA's log about it.

    Entries created before new ones were named Wattpilot are titled after
    the charger's host name, Wattpilot_<serial>, and Home Assistant logs
    the title on every failed setup attempt: to
    its config_entries logger, or to this package's logger once it has
    loaded the integration itself (retries, reloads). Only records logged on
    those two loggers pass here; this package's modules mask their own.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__()
        self._hass = hass

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record if it quotes a serial; never drop it."""
        try:
            message = record.getMessage()
        except TypeError, ValueError:
            return True  # a malformed record is logging's to report
        masked = message
        for found, stand_in in self._replacements():
            masked = masked.replace(found, stand_in)
        if masked != message:
            record.msg, record.args = masked, None
        return True

    def _replacements(self) -> list[tuple[str, str]]:
        entries = self._hass.config_entries.async_entries(DOMAIN)
        return [*_serial_masks(entries), *_address_title_masks(entries)]


def _serial_masks(entries: list[ConfigEntry]) -> list[tuple[str, str]]:
    # A serial is all digits; an entry awaiting it is keyed on a name or
    # address, which got masked in every other title as well (R1-17).
    return [
        (entry.unique_id, mask_serial(entry.unique_id))
        for entry in entries
        if entry.unique_id and entry.unique_id.isdigit()
    ]


def _address_title_masks(entries: list[ConfigEntry]) -> list[tuple[str, str]]:
    # A fork entry titled by its address put it into every setup message
    # (RF-P-02). An address literal, unlike a name, means nothing else.
    return [(entry.title, "<address>") for entry in entries if _is_address(entry.title)]


def _is_address(text: str) -> bool:
    try:
        ip_address(text)
    except ValueError:
        return False
    return True


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register integration services and mask serials in setup messages."""
    async_setup_services(hass)
    # Never removed: the setups the stop cancels log their title after the
    # stop event, and lost the mask with it (R1-05).
    serial_mask = _SerialMask(hass)
    for logger in _ENTRY_LOGGERS:
        logger.addFilter(serial_mask)
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
        serial=None if awaits_serial(entry) else entry.unique_id,
        # None for entries from before new ones were named Wattpilot: their
        # device keeps the host name their entity IDs were made from.
        name=entry.data.get(CONF_NAME),
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
        # A failed disconnect must not replace the error that ended setup --
        # that one says what the user has to fix (audit A14-03).
        try:
            await hub.async_shutdown()
        except HomeAssistantError:
            _LOGGER.debug(
                "Ignoring error while closing after failed setup", exc_info=True
            )
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
        await _adopt_serial(hass, entry, serial)
    await async_migrate_legacy_unique_ids(
        hass, entry, serial, hub.variant, hub.properties
    )

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
    hass: HomeAssistant, entry: WattpilotConfigEntry, serial: str
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
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="wrong_charger",
            translation_placeholders={
                "expected": mask_serial(entry.unique_id),
                "found": mask_serial(serial),
            },
        )
    if serial_taken_by_other_entry(hass, entry, serial):
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="duplicate_device",
            translation_placeholders={"serial": mask_serial(serial)},
        )
    await _remove_ignored_card(hass, serial)
    hass.config_entries.async_update_entry(
        entry,
        unique_id=serial,
        data={
            key: value
            for key, value in entry.data.items()
            if key != CONF_AWAITING_SERIAL
        },
    )
    _close_open_cards(hass, serial)


async def _remove_ignored_card(hass: HomeAssistant, serial: str) -> None:
    """
    Drop an ignored card for the charger this entry is about to own.

    It only kept the card from coming back, which the entry does now; left
    in place it would share this entry's unique_id (audit A18-07).
    """
    for ignored in hass.config_entries.async_entries(DOMAIN):
        if (
            ignored.source == config_entries.SOURCE_IGNORE
            and ignored.unique_id == serial
        ):
            await hass.config_entries.async_remove(ignored.entry_id)


@callback
def _close_open_cards(hass: HomeAssistant, serial: str) -> None:
    """
    Close the discovery cards still offering the charger just adopted.

    One opened before the entry knew its serial stays up otherwise, and
    ignoring it makes Home Assistant replace the entry (audit A18-01).
    """
    flows = hass.config_entries.flow
    for card in flows.async_progress_by_handler(
        DOMAIN,
        match_context={"source": config_entries.SOURCE_ZEROCONF, "unique_id": serial},
    ):
        flows.async_abort(card["flow_id"])


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear what this entry left in the repair list."""
    async_clear_collisions(hass, entry)


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
        # _adopt_serial. A cloud entry awaits it by its connection type
        # (awaits_serial), until Reconfigure makes it local.
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
        # Without a name the device took the host name, and every entity the
        # fork lacked got the serial in its ID (RF-P-04); a cloud entry too,
        # once Reconfigure made it local (PC5-R-04).
        new_data[CONF_NAME] = new_device_name(
            hass, preferred=old.get("friendly_name"), exclude=entry.entry_id
        )
        hass.config_entries.async_update_entry(
            entry, data=new_data, version=ENTRY_VERSION
        )
        _LOGGER.info("Migrated fork config entry %s to version 2", entry.entry_id)
    return True

"""Update platform (charger firmware)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from packaging.version import InvalidVersion, Version

from .const import signal_property
from .descriptions import (
    UPDATE_DESCRIPTIONS,
    WattpilotUpdateEntityDescription,
    filter_supported,
)
from .entity import WattpilotEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .hub import WattpilotConfigEntry

PARALLEL_UPDATES = 1


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: WattpilotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the firmware update entity."""
    hub = entry.runtime_data
    descriptions = filter_supported(
        UPDATE_DESCRIPTIONS,
        firmware=hub.firmware,
        variant=hub.variant,
        properties=hub.properties,
    )
    async_add_entities(
        WattpilotUpdate(hub, entry.entry_id, description)
        for description in descriptions
    )


def _newest_version(raw_versions: list[str]) -> str | None:
    """
    Return the raw string for the newest entry in raw_versions.

    The client's `available_firmware_versions` is an unsorted passthrough of
    the wire's "onv" property (see descriptions.py), so entries are ranked
    here by parsed Version and the original string is returned -- never a
    re-serialized one, since the charger expects its own spelling back on
    install. Entries that fail to parse are skipped for ranking; if none
    parse, the first raw entry is returned so an update still surfaces
    instead of silently vanishing.
    """
    ranked: list[tuple[str, Version]] = []
    for raw in raw_versions:
        try:
            ranked.append((raw, Version(raw)))
        except InvalidVersion:
            continue
    if ranked:
        return max(ranked, key=lambda entry: entry[1])[0]
    return raw_versions[0] if raw_versions else None


class WattpilotUpdate(WattpilotEntity, UpdateEntity):
    """Firmware update driven by onv (available) and fwv (installed)."""

    entity_description: WattpilotUpdateEntityDescription
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.SPECIFIC_VERSION
    )

    async def async_added_to_hass(self) -> None:
        """
        Also track installed_version, which moves independently of onv.

        The base entity subscribes to charger_key (onv, the available
        versions). installed_version comes from a different property (fwv)
        that changes on its own once an install completes -- with no onv push
        to carry it. Without this second subscription installed_version would
        stay frozen at whatever it was when onv last changed.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_property(self._entry_id, self.entity_description.installed_key),
                self._handle_installed_push,
            )
        )

    @callback
    def _handle_installed_push(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload
        self._attr_installed_version = value
        self.async_write_ha_state()

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- dynamically shaped charger payload
        self._attr_installed_version = self._hub.get_property(
            self.entity_description.installed_key
        )
        raw_versions: list[str]
        if isinstance(value, list):
            raw_versions = value
        elif value:
            raw_versions = [value]
        else:
            raw_versions = []
        self._attr_latest_version = (
            _newest_version(raw_versions) or self._attr_installed_version
        )

    async def async_install(
        self,
        version: str | None,
        backup: bool,  # noqa: ARG002, FBT001 -- HA's UpdateEntity base signature; called by keyword
        **_kwargs: Any,  # noqa: ANN401 -- HA's UpdateEntity base signature requires **kwargs: Any
    ) -> None:
        """Install the given version, defaulting to the displayed latest."""
        if version is None:
            version = self.latest_version
        await self._hub.async_install_firmware(version)

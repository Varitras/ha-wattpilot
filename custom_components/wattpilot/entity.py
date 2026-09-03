"""Push-subscribed base entity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, signal_availability, signal_property

if TYPE_CHECKING:
    from .descriptions import WattpilotDescriptionMixin
    from .hub import WattpilotHub


_NOT_SENT = object()


class WattpilotEntity(Entity):
    """Base for all Wattpilot entities: one property, push-updated."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hub: WattpilotHub,
        entry_id: str,
        description: WattpilotDescriptionMixin,
    ) -> None:
        """Set unique_id and device info from the hub; store refs for platforms."""
        self._hub = hub
        self._entry_id = entry_id
        self.entity_description = description  # type: ignore[assignment]
        self._description = description
        self._attr_unique_id = f"{hub.serial}-{description.uid_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hub.serial)},
            manufacturer=(hub.manufacturer or "Fronius").capitalize(),
            model=hub.model,
            name=hub.name,
            sw_version=hub.firmware,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to push signals and load the initial value."""
        await super().async_added_to_hass()
        description = self._description
        if description.requires_property:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    signal_property(self._entry_id, description.charger_key),
                    self._handle_push,
                )
            )
            # A sentinel, not None: "the charger has not sent this" and "the
            # charger sent null" are different answers, and for `trx` null is
            # the enum's own "No Transaction". Skipping it left the entity at
            # unknown until the charger repeated the field (audit VA-13).
            initial = self._hub.get_property(description.charger_key, _NOT_SENT)
            if initial is not _NOT_SENT:
                self._apply_value(initial)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_availability(self._entry_id),
                self._handle_availability,
            )
        )

    @property
    def available(self) -> bool:
        """Return whether the underlying charger is currently reachable."""
        return self._hub.available

    @callback
    def _handle_push(self, value: Any) -> None:  # noqa: ANN401 -- charger values are dynamically typed
        self._apply_value(value)
        self.async_write_ha_state()

    @callback
    def _handle_availability(self, _available: bool) -> None:  # noqa: FBT001 -- dispatcher callback arg
        self.async_write_ha_state()

    def _apply_value(self, value: Any) -> None:  # noqa: ANN401 -- charger values are dynamically typed
        """Platform hook: convert and store the pushed value."""

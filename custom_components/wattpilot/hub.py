"""
Connection hub: owns the charger client, dispatches push updates.

The ONLY module in this package allowed to import the client in api/
(architecture guard). Everything device-facing funnels through here.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from websockets.exceptions import WebSocketException

from .api import AuthenticationError, CloudInfo, Wattpilot, WattpilotError
from .const import signal_availability, signal_property

_LOGGER = logging.getLogger(__name__)

_AVAILABILITY_CHECK_INTERVAL = timedelta(seconds=30)
# Transport-error roots the client is expected to raise:
# WattpilotError: the vendor library's own errors.
# WebSocketException: raw websockets errors the vendor doesn't wrap.
# OSError: socket failures (also covers builtin ConnectionError/TimeoutError).
# Not only transport any more: since the vendored client waits for the
# charger's acknowledgement, a write can also fail because the device
# refused it (CommandError, a WattpilotError). Both end as the same
# HomeAssistantError -- the action did not take effect either way.
_WRITE_ERRORS = (WattpilotError, WebSocketException, OSError)

type WattpilotConfigEntry = ConfigEntry[WattpilotHub]


class WattpilotHub:
    """One hub per config entry; fans device pushes out via dispatcher."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        charger: Wattpilot,
        update_interval: timedelta = timedelta(0),
    ) -> None:
        """Wrap an already-constructed client; see create_local for the usual path."""
        self._hass = hass
        self._entry_id = entry_id
        self.charger = charger
        self._update_interval = update_interval
        self._last_available: bool | None = None
        self._unsubscribe_properties: CALLBACK_TYPE | None = None
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._pending: dict[str, Any] = {}
        self._cancel_flush: CALLBACK_TYPE | None = None
        # Set by async_shutdown, cleared by async_connect: see `available`.
        self._torn_down = False

    @classmethod
    def create_local(
        cls,
        hass: HomeAssistant,
        entry_id: str,
        host: str,
        password: str,
        update_interval: timedelta = timedelta(0),
    ) -> WattpilotHub:
        """Build a hub for a local WebSocket connection."""
        return cls(
            hass, entry_id, Wattpilot(host=host, password=password), update_interval
        )

    # ---- lifecycle ----

    async def async_connect(self) -> None:
        """Connect and authenticate; translate errors for config entry setup."""
        # Before the I/O, not after: the client replays its whole state the
        # moment the socket is up, and the first pushed property already
        # announces the charger as available. Clearing this afterwards would
        # leave `available` False for that burst while _last_available had
        # already gone True -- and _update_availability suppresses a repeat,
        # so nothing would ever correct the entities again.
        self._torn_down = False
        try:
            await self.charger.connect()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except _WRITE_ERRORS as err:
            raise ConfigEntryNotReady(f"Cannot connect to charger: {err}") from err
        self._update_availability(available=True)

    def start_dispatch(self) -> None:
        """
        Start forwarding property pushes and watching availability.

        Call after async_connect. During setup this runs BEFORE the platforms
        are forwarded, on purpose: an entity subscribes before it reads its
        initial value, so starting early cannot lose a push, while starting
        late loses any push that lands between an entity's read and this call
        (see async_setup_entry). Availability signalling does not run until
        this runs. Idempotent: reconnect_charger calls it again after
        reconnecting, and a second call must not register a second set of
        callbacks.
        """
        if self._unsubscribe_properties is not None:
            return
        self._unsubscribe_properties = self.charger.on_property_change(
            self._handle_property_change
        )
        # cancel_on_shutdown: this is a self-check, not device state -- it
        # must not outlive Home Assistant itself if async_shutdown is ever
        # skipped on the way down.
        self._cancel_timer = async_track_time_interval(
            self._hass,
            self._check_availability,
            _AVAILABILITY_CHECK_INTERVAL,
            cancel_on_shutdown=True,
        )
        if self._update_interval:
            self._cancel_flush = async_track_time_interval(
                self._hass,
                self._flush_pending,
                self._update_interval,
                cancel_on_shutdown=True,
            )

    async def async_reconnect(self) -> None:
        """
        Re-establish the connection without missing the charger's replay.

        Subscribing before connecting is the whole point. The client pushes
        its complete property snapshot the moment the socket is up, and that
        burst is the only chance to learn what moved while nobody was
        listening. Connect first and it lands on an empty callback list, so
        anything that changed during the outage stays stale until the device
        happens to send it again -- for a rarely changing property, never.

        The opposite order is right during setup, where the entities do not
        exist yet; that is why neither caller assembles this itself.
        """
        self.start_dispatch()
        await self.async_connect()

    async def async_shutdown(self) -> None:
        """
        Stop dispatching and close the connection.

        Handle cleanup (unsubscribe, cancel timer) runs first and
        unconditionally, before the I/O call that can fail -- a failed
        disconnect must still leave the hub's callbacks fully torn down,
        not half-alive.
        """
        self._torn_down = True
        if self._unsubscribe_properties is not None:
            self._unsubscribe_properties()
            self._unsubscribe_properties = None
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._cancel_flush is not None:
            self._cancel_flush()
            self._cancel_flush = None
        # Drop whatever was buffered: the next line marks the charger
        # unavailable, so flushing stale values into entities on the way out
        # would only contradict that.
        self._pending.clear()
        try:
            await self.charger.disconnect()
        except _WRITE_ERRORS as err:
            raise HomeAssistantError(f"Failed to disconnect: {err}") from err
        finally:
            # Announcing this is not optional and not conditional. The
            # subscription above is gone, so no update will ever reach an
            # entity again -- including after a disconnect that raised,
            # where `available` would still read True. And it has to happen
            # here rather than before the I/O, because the timer that would
            # otherwise have noticed was just cancelled: nothing else is
            # left to write entity state, and every sensor would keep
            # displaying its last reading for good. expected=True: this teardown
            # is deliberate, so it must not log as a fault.
            self._update_availability(available=False, expected=True)

    # ---- state ----

    @property
    def serial(self) -> str:
        """Return the charger's serial number."""
        return self.charger.serial

    @property
    def firmware(self) -> str | None:
        """Return the charger's firmware version, if known."""
        return self.charger.firmware

    @property
    def variant(self) -> object:
        """Return the charger's hardware variant (11 kW / 22 kW)."""
        return self.charger.variant

    @property
    def manufacturer(self) -> str:
        """Return the charger's manufacturer name."""
        return self.charger.manufacturer

    @property
    def model(self) -> str | None:
        """Return the charger's model name, if known."""
        return self.charger.model

    @property
    def name(self) -> str:
        """Return the charger's device name."""
        return self.charger.name

    @property
    def available(self) -> bool:
        """Return whether the charger is currently reachable."""
        # Not the client's flag alone: it clears `connected` only after a
        # successful ws.close(), so a close that raised leaves it True while
        # the message loop behind it is already gone. Once this hub has torn
        # itself down, no update can reach an entity again whatever the
        # client believes (audit VA-04).
        return not self._torn_down and bool(self.charger.connected)

    @property
    def properties(self) -> dict[str, Any]:
        """Return the last known value of every charger property."""
        return self.charger.all_properties

    def get_property(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        """Return one charger property, or default if not seen yet."""
        return self.charger.all_properties.get(key, default)

    # ---- push handling ----

    @callback
    def _handle_property_change(self, key: str, value: Any) -> None:  # noqa: ANN401
        # Availability is never buffered: a value push is proof the charger is
        # reachable, and delaying that up to an interval would let a recovered
        # charger keep showing as unavailable.
        self._update_availability(available=True)
        if self._update_interval:
            # Held until the next flush; a newer value for the same key
            # overwrites the buffered one, so only the latest is delivered.
            self._pending[key] = value
        else:
            async_dispatcher_send(
                self._hass, signal_property(self._entry_id, key), value
            )

    @callback
    def _flush_pending(self, _now: datetime) -> None:
        pending, self._pending = self._pending, {}
        for key, value in pending.items():
            async_dispatcher_send(
                self._hass, signal_property(self._entry_id, key), value
            )

    @callback
    def _check_availability(self, _now: datetime) -> None:
        self._update_availability(available=self.available)

    @callback
    def _update_availability(self, *, available: bool, expected: bool = False) -> None:
        if available == self._last_available:
            return
        # None means nothing has been claimed about this charger yet, so the
        # first connect is not a recovery and must not be announced as one.
        never_reported = self._last_available is None
        self._last_available = available
        if never_reported:
            _LOGGER.debug("Charger %s connected", self.serial)
        elif available:
            _LOGGER.info("Charger %s is back online", self.serial)
        elif expected:
            # A deliberate teardown (unload, HA stop, config-flow probe): the
            # charger going away is the expected outcome, not a fault.
            _LOGGER.debug("Charger %s disconnected", self.serial)
        else:
            _LOGGER.warning("Charger %s is unavailable", self.serial)
        async_dispatcher_send(
            self._hass, signal_availability(self._entry_id), available
        )

    # ---- writes ----

    async def async_set_property(self, key: str, value: Any) -> None:  # noqa: ANN401
        """Push a single property value to the charger."""
        try:
            await self.charger.set_property(key, value)
        except _WRITE_ERRORS as err:
            raise HomeAssistantError(f"Failed to set {key}: {err}") from err

    async def async_set_next_trip(self, departure: time) -> None:
        """Set the next scheduled departure time."""
        try:
            await self.charger.set_next_trip(departure)
        except _WRITE_ERRORS as err:
            raise HomeAssistantError(f"Failed to set next trip: {err}") from err

    async def async_set_next_trip_energy(self, energy_wh: float) -> None:
        """Set the energy target for the next scheduled trip."""
        # Verified on firmware 42.5: device holds fte=25000 with esk=True,
        # i.e. Wh -- despite the vendor's parameter being named energy_kwh.
        try:
            await self.charger.set_next_trip_energy(energy_wh)
        except _WRITE_ERRORS as err:
            raise HomeAssistantError(f"Failed to set next trip energy: {err}") from err

    async def async_enable_cloud_api(self) -> CloudInfo:
        """Enable the charger's cloud API and return its connection info."""
        try:
            return await self.charger.enable_cloud_api()
        except _WRITE_ERRORS as err:
            raise HomeAssistantError(f"Failed to enable cloud API: {err}") from err

    async def async_disable_cloud_api(self) -> None:
        """Disable the charger's cloud API."""
        try:
            await self.charger.disable_cloud_api()
        except _WRITE_ERRORS as err:
            raise HomeAssistantError(f"Failed to disable cloud API: {err}") from err

    async def async_install_firmware(self, version: str | None) -> None:
        """Trigger a firmware update, or install the latest if version is None."""
        try:
            await self.charger.install_firmware_update(version)
        except _WRITE_ERRORS as err:
            raise HomeAssistantError(f"Firmware update failed: {err}") from err

"""Async WebSocket client for Fronius Wattpilot devices."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Self

from .auth import (
    compute_auth_response,
    generate_token,
    hash_password,
    sign_secured_message,
)
from .connection import Connection
from .definition import ApiDefinition, load_api_definition
from .exceptions import (
    AuthenticationError,
    CommandError,
    DeviceIdentityError,
    PropertyError,
    WattpilotConnectionError,
)
from .models import AuthHashType, CloudInfo, DeviceInfo, LoadMode

_LOGGER = logging.getLogger(__name__)

WPFLEX_DEVICE_TYPE = "wattpilot_flex"
# Seconds between two attempts to reach a charger that is rebooting.
_REBOOT_RETRY_PAUSE = 2.0
CLOUD_API_BASE_URL = "https://app.wattpilot.io/app"

type PropertyCallback = Callable[[str, Any], Any]
type MessageCallback = Callable[[dict[str, Any]], Any]


def _plain_json(value: Any) -> Any:  # noqa: ANN401 -- recurses over untyped JSON
    """
    Turn the wire shape into plain JSON.

    The frame is parsed a second time with an object_hook that builds a
    SimpleNamespace per nested object, because the message handlers read
    fields by attribute. Everything downstream -- entities, diagnostics, the
    redaction that walks dictionaries and lists -- expects plain containers,
    and a namespace slipped past all of it (audit A11-03).
    """
    if isinstance(value, SimpleNamespace):
        return {key: _plain_json(item) for key, item in vars(value).items()}
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return value


def _nested_ssid(value: Any) -> str | None:  # noqa: ANN401 -- untyped JSON
    """Return the SSID inside a `ccw` object, if one was sent at all."""
    return value.get("ssid") if isinstance(value, dict) else None


def _correlation_key(request_id: object) -> str:
    """
    wattpilot: one spelling for a request id, whatever the wire used.

    Commands are sent with an integer; the protocol's own examples answer
    with a string. Comparing them raw left the caller waiting for an answer
    that had already arrived (audit A11-01).
    """
    return str(request_id)


def _message_type(raw: str) -> str:
    """wattpilot: the frame's type, for logging without its payload."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return "<unparsable>"
    if isinstance(parsed, dict):
        return str(parsed.get("type", "<untyped>"))
    return "<not an object>"


class Wattpilot:
    """
    Async client for a Fronius Wattpilot wallbox.

    Usage::

        async with Wattpilot("192.168.1.100", "mypassword") as wp:
            print(wp.power)
            await wp.set_power(16)
    """

    def __init__(  # noqa: PLR0913 -- every connection knob it has
        self,
        host: str,
        password: str,
        serial: str | None = None,
        *,
        cloud: bool = False,
        connect_timeout: float = 30.0,
        init_timeout: float = 30.0,
        auto_reconnect: bool = True,
        reconnect_delay_min: float = 5.0,
        reconnect_delay_max: float = 300.0,
    ) -> None:
        """Configure a client; nothing is opened until connect() is awaited."""
        self._host = host
        self._password = password
        self._cloud = cloud
        self._device = DeviceInfo(serial=serial or "")
        self._hashed_password: bytes = b""
        self._auth_hash_type = AuthHashType.PBKDF2

        if cloud:
            url = f"wss://app.wattpilot.io/app/{serial or ''}?version=1.2.9"
        else:
            url = f"ws://{host}/ws"

        self._connection = Connection(
            url,
            self._handle_message,
            self._fail_pending_commands,
            connect_timeout=connect_timeout,
            init_timeout=init_timeout,
            auto_reconnect=auto_reconnect,
            reconnect_delay_min=reconnect_delay_min,
            reconnect_delay_max=reconnect_delay_max,
        )
        self._request_id = 0
        # wattpilot: pending setValue commands by correlation key (VA-03).
        # Upstream fired and forgot, so a charger's rejection reached nobody.
        # Keyed by string: commands go out with an integer id, but the
        # protocol documents responses carrying it as a string, and an
        # unnormalized lookup missed those answers entirely (audit A11-01).
        self._pending_commands: dict[str, asyncio.Future[None]] = {}
        self.command_timeout = 10.0
        self._all_props: dict[str, Any] = {}

        # Named property caches
        self._voltage1: float | None = None
        self._voltage2: float | None = None
        self._voltage3: float | None = None
        self._voltage_n: float | None = None
        self._amps1: float | None = None
        self._amps2: float | None = None
        self._amps3: float | None = None
        self._power1: float | None = None
        self._power2: float | None = None
        self._power3: float | None = None
        self._power_n: float | None = None
        self._power: float | None = None
        self._amp: int | None = None
        self._version: str | None = None
        self._firmware: str | None = None
        self._wifi_ssid: str | None = None
        self._mode: int | None = None
        self._car_connected: int | None = None
        self._allow_charging: bool | None = None
        self._access_state: int | None = None
        self._cable_type: int | None = None
        self._cable_lock: int | None = None
        self._frequency: float | None = None
        self._phases: Any = None
        self._energy_counter_since_start: float | None = None
        self._energy_counter_total: float | None = None
        self._error_state: int | None = None
        self._cae: bool | None = None
        self._cak: str | None = None

        # Lazy-loaded API definition for type coercion
        self._api_def_cache: ApiDefinition | None = None

        # Callbacks
        self._property_callbacks: list[PropertyCallback] = []
        self._message_callbacks: list[MessageCallback] = []

    # ---- Context manager ----

    async def __aenter__(self) -> Self:
        """Connect on entry."""
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Disconnect on exit, however the block ended."""
        await self.disconnect()

    # ---- Connection lifecycle ----

    async def connect(self) -> None:
        """Open the WebSocket and authenticate."""
        await self._connection.open()
        # The definition load belongs inside the same cleanup scope as the
        # handshake. Between the two, a cancellation fell outside the
        # connection's scope and outside the integration's, leaving the
        # reader and the socket alive (audit A12-03).
        try:
            await self._load_api_definition()
        except BaseException:
            await self._connection.close()
            raise
        _LOGGER.info("Connected to Wattpilot %s", self._device.serial)

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        await self._connection.close()

    def _fail_pending_commands(self, reason: str) -> None:
        """wattpilot: never leave a caller awaiting an answer that cannot come."""
        for request_id, future in list(self._pending_commands.items()):
            if not future.done():
                future.set_exception(
                    CommandError(f"Command {request_id} abandoned: {reason}")
                )
            self._pending_commands.pop(request_id, None)

    # ---- Read-only properties ----

    @property
    def connected(self) -> bool:
        """Return whether the WebSocket connection is up."""
        return self._connection.connected

    @property
    def serial(self) -> str:
        """Return the charger's serial number."""
        return self._device.serial

    @property
    def name(self) -> str:
        """Return the charger's friendly name."""
        return self._device.name

    @property
    def hostname(self) -> str:
        """Return the charger's network hostname."""
        return self._device.hostname

    @property
    def manufacturer(self) -> str:
        """Return the manufacturer as the charger reports it."""
        return self._device.manufacturer

    @property
    def device_type(self) -> str:
        """Return the device type, e.g. "wattpilot_V2"."""
        return self._device.device_type

    @property
    def protocol(self) -> int:
        """Return the protocol version the charger speaks."""
        return self._device.protocol

    @property
    def secured(self) -> int:
        """Return whether writes must be signed (0 = no)."""
        return self._device.secured

    @property
    def version(self) -> str | None:
        """Return the charger's software version, if known."""
        return self._version or self._device.version or None

    @property
    def firmware(self) -> str | None:
        """Return the charger's firmware version, if known."""
        return self._firmware

    @property
    def voltage1(self) -> float | None:
        """Return the L1 voltage in V."""
        return self._voltage1

    @property
    def voltage2(self) -> float | None:
        """Return the L2 voltage in V."""
        return self._voltage2

    @property
    def voltage3(self) -> float | None:
        """Return the L3 voltage in V."""
        return self._voltage3

    @property
    def voltage_n(self) -> float | None:
        """Return the neutral voltage in V."""
        return self._voltage_n

    @property
    def amps1(self) -> float | None:
        """Return the L1 current in A."""
        return self._amps1

    @property
    def amps2(self) -> float | None:
        """Return the L2 current in A."""
        return self._amps2

    @property
    def amps3(self) -> float | None:
        """Return the L3 current in A."""
        return self._amps3

    @property
    def power1(self) -> float | None:
        """Return the L1 power in kW."""
        return self._power1

    @property
    def power2(self) -> float | None:
        """Return the L2 power in kW."""
        return self._power2

    @property
    def power3(self) -> float | None:
        """Return the L3 power in kW."""
        return self._power3

    @property
    def power_n(self) -> float | None:
        """Return the neutral power in kW."""
        return self._power_n

    @property
    def power(self) -> float | None:
        """Return the total charging power in kW."""
        return self._power

    @property
    def amp(self) -> int | None:
        """Return the requested charging current in A."""
        return self._amp

    @property
    def mode(self) -> int | None:
        """Return the load mode (see LoadMode)."""
        return self._mode

    @property
    def car_connected(self) -> int | None:
        """Return the vehicle state (see the `car` property)."""
        return self._car_connected

    @property
    def allow_charging(self) -> bool | None:
        """Return whether the charger currently allows charging."""
        return self._allow_charging

    @property
    def access_state(self) -> int | None:
        """Return the access control state (open / authentication required)."""
        return self._access_state

    @property
    def cable_type(self) -> int | None:
        """Return the cable's current rating in A."""
        return self._cable_type

    @property
    def cable_lock(self) -> int | None:
        """Return the cable lock state."""
        return self._cable_lock

    @property
    def frequency(self) -> float | None:
        """Return the grid frequency in Hz, or 0 if unknown."""
        return self._frequency

    @property
    def phases(self) -> Any:  # noqa: ANN401 -- charger values are dynamically typed
        """Return the raw per-phase flags the charger reports."""
        return self._phases

    @property
    def energy_counter_since_start(self) -> float | None:
        """Return the energy charged this session in Wh."""
        return self._energy_counter_since_start

    @property
    def energy_counter_total(self) -> float | None:
        """Return the charger's lifetime energy counter in Wh."""
        return self._energy_counter_total

    @property
    def error_state(self) -> int | None:
        """Return the internal error code (0 = none)."""
        return self._error_state

    @property
    def wifi_ssid(self) -> str | None:
        """Return the SSID the charger is connected to."""
        return self._wifi_ssid

    @property
    def cae(self) -> bool | None:
        """Return whether the go-e cloud API is enabled."""
        return self._cae

    @property
    def cak(self) -> str | None:
        """Return the go-e cloud API key."""
        return self._cak

    @property
    def all_properties(self) -> dict[str, Any]:
        """Return a copy of every property seen so far."""
        return dict(self._all_props)

    @property
    def properties_initialized(self) -> bool:
        """Return whether the charger has sent its full state once."""
        return self._connection.initialized

    # ---- Additional typed properties ----

    # Device info

    @property
    def variant(self) -> str | None:
        """Device variant (e.g. ``'11kW'``, ``'22kW'``)."""
        return self._all_props.get("var")

    @property
    def model(self) -> str | None:
        """Device model / type string."""
        return self._all_props.get("typ")

    # Charging state

    @property
    def car_state(self) -> int | None:
        """Car connection state (use :class:`CarStatus` enum)."""
        return self._all_props.get("car")

    @property
    def cable_unlock_status(self) -> int | None:
        """Cable unlock status."""
        return self._all_props.get("cus")

    @property
    def charging_reason(self) -> int | None:
        """Detailed charging reason / model status."""
        return self._all_props.get("modelStatus")

    @property
    def force_state(self) -> int | None:
        """Force charging state (use :class:`ForceState` enum)."""
        return self._all_props.get("frc")

    @property
    def active_transaction_chip(self) -> int | None:
        """Active RFID transaction chip ID."""
        return self._all_props.get("trx")

    # Configuration

    @property
    def button_lock(self) -> int | None:
        """Button / access lock level."""
        return self._all_props.get("bac")

    @property
    def daylight_saving(self) -> int | None:
        """Daylight saving time mode (``1`` = enabled)."""
        return self._all_props.get("tds")

    @property
    def phase_switch_mode(self) -> int | None:
        """Phase switching mode (use :class:`PhaseSwitchMode` enum)."""
        return self._all_props.get("psm")

    # Diagnostics

    @property
    def inverter_info(self) -> Any:  # noqa: ANN401 -- charger values are dynamically typed
        """Connected inverter information."""
        return self._all_props.get("cci")

    @property
    def wifi_connection_info(self) -> Any:  # noqa: ANN401 -- charger values are dynamically typed
        """WiFi connection details (SSID, IP, netmask, etc.)."""
        return self._all_props.get("ccw")

    @property
    def lock_feedback(self) -> int | None:
        """Lock feedback status."""
        return self._all_props.get("ffb")

    @property
    def effective_lock_setting(self) -> int | None:
        """Effective lock setting."""
        return self._all_props.get("lck")

    @property
    def local_time(self) -> str | None:
        """Local time as reported by the charger."""
        return self._all_props.get("loc")

    @property
    def wifi_signal_strength(self) -> int | None:
        """WiFi signal strength (RSSI in dBm)."""
        return self._all_props.get("rssi")

    @property
    def temperature(self) -> Any:  # noqa: ANN401 -- charger values are dynamically typed
        """Temperature sensor readings."""
        return self._all_props.get("tma")

    @property
    def uptime_ms(self) -> int | None:
        """Device uptime in milliseconds."""
        return self._all_props.get("rbt")

    @property
    def reboot_count(self) -> int | None:
        """Number of device reboots."""
        return self._all_props.get("rbc")

    @property
    def websocket_queue_size(self) -> int | None:
        """WebSocket send queue size."""
        return self._all_props.get("qsw")

    @property
    def http_clients(self) -> int | None:
        """Number of connected HTTP clients."""
        return self._all_props.get("wcch")

    @property
    def websocket_clients(self) -> int | None:
        """Number of connected WebSocket clients."""
        return self._all_props.get("wccw")

    @property
    def wifi_status(self) -> int | None:
        """WiFi connection status."""
        return self._all_props.get("wst")

    # RFID

    @property
    def rfid_cards(self) -> Any:  # noqa: ANN401 -- charger values are dynamically typed
        """Configured RFID cards."""
        return self._all_props.get("cards")

    # PV / Solar

    @property
    def pv_surplus_enabled(self) -> bool | None:
        """Whether PV surplus charging is enabled."""
        return self._all_props.get("fup")

    @property
    def pv_surplus_start_power(self) -> float | None:
        """PV surplus start power threshold in watts."""
        return self._all_props.get("fst")

    @property
    def pv_battery_threshold(self) -> float | None:
        """PV battery minimum threshold."""
        return self._all_props.get("fam")

    @property
    def min_charging_time(self) -> int | None:
        """Minimum charging time in seconds."""
        return self._all_props.get("fmt")

    @property
    def next_trip_energy(self) -> float | None:
        """Planned energy for next trip in Wh."""
        return self._all_props.get("fte")

    @property
    def next_trip_time(self) -> int | None:
        """Planned departure time for next trip (seconds since midnight)."""
        return self._all_props.get("ftt")

    # Firmware

    @property
    def installed_firmware_version(self) -> str | None:
        """Currently installed firmware version."""
        return self._firmware

    @property
    def available_firmware_versions(self) -> list[str]:
        """List of available firmware versions for update."""
        val = self._all_props.get("onv")
        if isinstance(val, list):
            return [str(v) for v in val]
        if isinstance(val, str) and val:
            return [val]
        return []

    @property
    def firmware_update_available(self) -> bool:
        """Whether a firmware update is available."""
        available = self.available_firmware_versions
        if not available:
            return False
        installed = self.firmware
        if not installed:
            return bool(available)
        return any(v != installed for v in available)

    # Cloud API

    @property
    def cloud_enabled(self) -> bool | None:
        """Whether the go-e Cloud API is enabled."""
        return self._cae

    @property
    def cloud_api_key(self) -> str | None:
        """Cloud API key (available when cloud is enabled)."""
        return self._cak

    @property
    def cloud_api_url(self) -> str | None:
        """Cloud API base URL for this device."""
        if not self._cae or not self.serial:
            return None
        return f"{CLOUD_API_BASE_URL}/{self.serial}"

    # ---- Commands ----

    async def set_property(self, name: str, value: Any) -> None:  # noqa: ANN401 -- charger values are dynamically typed
        """
        Set a single property on the device.

        Values are automatically coerced to the type expected by the charger
        protocol (based on the API definition's ``jsonType``).
        """
        value = self._coerce_value(name, value)
        self._request_id += 1
        message: dict[str, Any] = {
            "type": "setValue",
            "requestId": self._request_id,
            "key": name,
            "value": value,
        }
        secure = self._device.secured is not None and self._device.secured > 0
        # wattpilot: await the device's answer instead of returning once the
        # frame is on the wire (audit VA-03).
        await self._send_command(message, secure=secure)

    async def set_power(self, amperage: int) -> None:
        """Set the charging amperage."""
        await self.set_property("amp", amperage)

    async def set_mode(self, mode: LoadMode) -> None:
        """Set the load mode."""
        await self.set_property("lmo", int(mode))

    async def set_next_trip(
        self,
        departure_time: datetime.time | datetime.datetime,
    ) -> None:
        """
        Schedule the next trip departure time.

        Handles timestamp conversion and DST adjustment automatically
        based on the charger's ``tds`` (daylight-saving) property.

        The ``tds`` property indicates which DST *scheme* is configured
        (``1`` = European Summer Time, ``2`` = US Daylight Time), **not**
        whether DST is currently active.  The offset is only applied when
        the system's local timezone reports that DST is in effect.
        """
        if isinstance(departure_time, datetime.datetime):
            departure_time = departure_time.time()

        timestamp = (
            departure_time.hour * 3600
            + departure_time.minute * 60
            + departure_time.second
        )

        # wattpilot: no daylight-saving correction. The adopted client added
        # an hour whenever `tds` announced a scheme and the clock was in
        # summer time. Measured on the real charger on 2026-09-06, in summer
        # time and with `tds` = 1: a departure set to 07:30 in the app reads
        # back as 27000, not 30600. The firmware takes plain seconds since
        # local midnight, exactly as the protocol reference says, and the
        # entity reads them back the same way -- adding the hour here made
        # 07:30 come back as 08:30 (audit A12-06).
        await self.set_property("ftt", timestamp)

    async def set_next_trip_energy(self, energy_kwh: float) -> None:
        """
        Set the energy requirement for the next trip in kWh.

        Automatically sets the energy unit to kWh before updating.
        """
        # FBT003 does not apply: the bool is the value being written to the
        # charger, not a flag steering this function.
        await self.set_property("esk", True)  # noqa: FBT003
        await self.set_property("fte", energy_kwh)

    async def enable_cloud_api(
        self,
        *,
        timeout: float = 10.0,  # noqa: ASYNC109 -- part of this client's API
    ) -> CloudInfo:
        """
        Enable the go-e Cloud API and wait for the API key.

        Returns a :class:`CloudInfo` with the API key and URL.
        Raises :class:`WattpilotConnectionError` if the API key is not received
        within *timeout* seconds.
        """
        await self.set_property("cae", True)  # noqa: FBT003 -- a written value

        elapsed = 0.0
        while elapsed < timeout:
            if self._cak and self._cak != "":
                return CloudInfo(
                    enabled=True,
                    api_key=self._cak,
                    url=f"{CLOUD_API_BASE_URL}/{self.serial}",
                )
            await asyncio.sleep(1)
            elapsed += 1

        msg = "Timeout waiting for cloud API key"
        raise WattpilotConnectionError(msg)

    async def disable_cloud_api(self) -> None:
        """Disable the go-e Cloud API."""
        await self.set_property("cae", False)  # noqa: FBT003 -- a written value

    async def install_firmware_update(
        self,
        version: str | None = None,
        *,
        timeout: float = 120.0,  # noqa: ASYNC109 -- part of this client's API
    ) -> None:
        """
        Install a firmware update and wait for the charger to reboot.

        If *version* is not specified, the first available version is used.
        Raises :class:`PropertyError` if no updates are available.
        Raises :class:`WattpilotConnectionError` on timeout.
        """
        if version is None:
            versions = self.available_firmware_versions
            if not versions:
                msg = "No firmware updates available"
                raise PropertyError(msg)
            version = versions[0]

        await self.set_property("oct", version)

        # wattpilot: a monotonic deadline, not a sum of sleeps. The counter
        # this replaces advanced only by its own sleeps, so the time spent
        # waiting for each reconnect was free -- under the default budget
        # roughly sixty attempts of thirty seconds, half an hour past the two
        # minutes the caller asked for (audit A12-08).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._connection.wait_disconnected(), timeout)

        if self.connected:
            msg = "Charger did not disconnect for firmware update"
            raise WattpilotConnectionError(msg)

        await self.disconnect()
        await self._reconnect_before(deadline)

    async def _reconnect_before(self, deadline: float) -> None:
        """
        Keep trying to reach the rebooted charger until the deadline passes.

        Every attempt is bounded by what is left of the budget, and so is the
        pause between them. Both used to be free: the caller's timeout was a
        sum of sleeps, and a single attempt could sit in a full
        authentication wait without spending any of it (audit A12-08).
        """
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            with contextlib.suppress(Exception):
                async with asyncio.timeout(remaining):
                    await self.connect()
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_REBOOT_RETRY_PAUSE, remaining))

        msg = "Timeout reconnecting after firmware update"
        raise WattpilotConnectionError(msg)

    # ---- Type coercion ----

    async def _load_api_definition(self) -> None:
        """
        Read the API definition into the cache, off the event loop.

        wattpilot: the definition is an 88 kB YAML file, and the lazy path
        below used to read it during the first write -- blocking the caller's
        event loop at a moment nobody chose. Loading it while connecting
        keeps that read in a thread, and leaves the lazy path as the
        fallback it was meant to be.
        """
        if self._api_def_cache is not None:
            return
        self._api_def_cache = await asyncio.to_thread(
            load_api_definition, split_properties=False
        )

    def _get_api_def(self) -> ApiDefinition:
        """Return the cached API definition, reading it if connect did not."""
        if self._api_def_cache is None:
            self._api_def_cache = load_api_definition(split_properties=False)
        return self._api_def_cache

    def _coerce_value(self, name: str, value: Any) -> Any:  # noqa: ANN401 -- charger values are dynamically typed
        """Coerce *value* to the protocol type expected for property *name*."""
        if isinstance(value, SimpleNamespace):
            return value.__dict__

        api_def = self._get_api_def()
        prop_def = api_def.properties.get(name)
        if prop_def is None:
            return value

        json_type = prop_def.get("jsonType", "")
        if not json_type:
            return value

        return self._coerce_to_json_type(value, json_type, name)

    # A type table written as code: one branch per jsonType the charger
    # declares. Splitting it would scatter the mapping without simplifying it.
    def _coerce_to_json_type(  # noqa: C901, PLR0911, PLR0912
        self,
        value: Any,  # noqa: ANN401 -- whatever the charger declared for this key
        json_type: str,
        name: str,
    ) -> Any:  # noqa: ANN401 -- the coerced value keeps the charger's own type
        """Convert *value* to the specified JSON type for property *name*."""
        match json_type:
            case "boolean":
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    lower = value.lower()
                    if lower in ("true", "1", "yes"):
                        return True
                    if lower in ("false", "0", "no"):
                        return False
                    msg = f"Cannot convert '{value}' to bool for property '{name}'"
                    raise PropertyError(msg)
                if isinstance(value, int | float):
                    return bool(value)
                msg = (
                    f"Cannot convert {type(value).__name__} to bool "
                    f"for property '{name}'"
                )
                raise PropertyError(msg)
            case "integer":
                if isinstance(value, bool):
                    return int(value)
                if isinstance(value, int):
                    return value
                if isinstance(value, float):
                    return int(value)
                if isinstance(value, str):
                    with contextlib.suppress(ValueError):
                        return int(value)
                    with contextlib.suppress(ValueError):
                        return int(float(value))
                    msg = f"Cannot convert '{value}' to int for property '{name}'"
                    raise PropertyError(msg)
                msg = (
                    f"Cannot convert {type(value).__name__} to int "
                    f"for property '{name}'"
                )
                raise PropertyError(msg)
            case "float":
                if isinstance(value, bool):
                    return float(value)
                if isinstance(value, int | float):
                    return float(value)
                if isinstance(value, str):
                    with contextlib.suppress(ValueError):
                        return float(value)
                    msg = f"Cannot convert '{value}' to float for property '{name}'"
                    raise PropertyError(msg)
                msg = (
                    f"Cannot convert {type(value).__name__} to float "
                    f"for property '{name}'"
                )
                raise PropertyError(msg)
            case "string":
                return str(value)
            case _:
                return value

    # ---- Callbacks ----

    def on_property_change(self, callback: PropertyCallback) -> Callable[[], None]:
        """Register a property-change callback. Returns an unsubscribe function."""
        self._property_callbacks.append(callback)

        def unsubscribe() -> None:
            """Remove the callback registered above."""
            self._property_callbacks.remove(callback)

        return unsubscribe

    def on_message(self, callback: MessageCallback) -> Callable[[], None]:
        """Register a raw-message callback. Returns an unsubscribe function."""
        self._message_callbacks.append(callback)

        def unsubscribe() -> None:
            """Remove the callback registered above."""
            self._message_callbacks.remove(callback)

        return unsubscribe

    # ---- Internal: message loop ----

    # One branch per message type the charger sends; the dispatch is the
    # function's whole content. The complexity ratchet in tests/ freezes
    # the number so it cannot creep upward unnoticed.
    async def _handle_message(self, raw: str) -> None:  # noqa: C901
        # wattpilot: type only, never the payload (audit VA-01). Frames
        # carry Wi-Fi passwords, cloud tokens and OCPP keys, and DEBUG
        # logging for this integration is one toggle away in the UI.
        _LOGGER.debug("Message received: %s (%d bytes)", _message_type(raw), len(raw))
        msg = json.loads(raw)

        # Fire raw message callbacks
        for cb in self._message_callbacks:
            if inspect.iscoroutinefunction(cb):
                await cb(msg)
            else:
                cb(msg)

        msg_type = msg.get("type", "")
        ns = json.loads(raw, object_hook=lambda d: SimpleNamespace(**d))

        match msg_type:
            case "hello":
                await self._on_hello(ns)
            case "authRequired":
                await self._on_auth_required(ns)
            case "authSuccess":
                self._on_auth_success(ns)
            case "authError":
                self._on_auth_error(ns)
            case "fullStatus":
                self._on_full_status(ns)
            case "deltaStatus":
                self._on_delta_status(ns)
            case "response":
                self._on_response(ns)
            case "clearInverters" | "updateInverter":
                pass
            case _:
                _LOGGER.debug("Unhandled message type: %s", msg_type)

    async def _on_hello(self, msg: SimpleNamespace) -> None:
        if not await self._identity_accepted(msg.serial):
            return
        _LOGGER.info("Connected to Wattpilot serial %s", msg.serial)
        self._device.serial = msg.serial
        if hasattr(msg, "hostname"):
            self._device.name = msg.hostname
            self._device.hostname = msg.hostname
        if hasattr(msg, "friendly_name"):
            self._device.friendly_name = msg.friendly_name
        if hasattr(msg, "version"):
            self._device.version = msg.version
        self._device.manufacturer = getattr(msg, "manufacturer", "")
        self._device.device_type = getattr(msg, "devicetype", "")
        self._device.protocol = getattr(msg, "protocol", 0)
        if hasattr(msg, "secured"):
            self._device.secured = msg.secured

    async def _identity_accepted(self, serial: str) -> bool:
        """
        Refuse a connection that is not the charger this client already knows.

        Hello is the one frame every connection sends -- the first, an
        automatic reconnect and an explicit one alike -- so the check belongs
        here rather than in the setup path that ran once. The address can be
        reused by DHCP or the hardware replaced, and a reconnect kept the
        config entry, its entities and their history pointed at whatever
        answered (audit A11-02). Nothing is applied and no command is
        accepted afterwards: the socket goes, and the loop above stops
        reconnecting because a fatal error is set.
        """
        known = self._device.serial
        if not known or known == serial:
            return True
        msg = f"Expected charger {known}, but {serial} answered at {self._host}"
        await self._connection.reject(DeviceIdentityError(msg))
        return False

    async def _on_auth_required(self, msg: SimpleNamespace) -> None:
        if hasattr(msg, "hash"):
            self._auth_hash_type = AuthHashType(msg.hash)
        elif self._device.device_type == WPFLEX_DEVICE_TYPE:
            self._auth_hash_type = AuthHashType.BCRYPT

        await self._update_hashed_password()

        token3 = generate_token()
        auth_hash = compute_auth_response(
            msg.token1, msg.token2, token3, self._hashed_password
        )
        response = {"type": "auth", "token3": token3, "hash": auth_hash}
        await self._send(response)

    def _on_auth_success(self, _msg: SimpleNamespace) -> None:
        self._connection.mark_authenticated()
        _LOGGER.info("Authentication successful")

    def _on_auth_error(self, msg: SimpleNamespace) -> None:
        error_msg = getattr(msg, "message", "Unknown auth error")
        _LOGGER.error("Authentication failed: %s", error_msg)
        self._connection.fail(AuthenticationError(error_msg))

    def _on_full_status(self, msg: SimpleNamespace) -> None:
        props = msg.status.__dict__
        for key, value in props.items():
            self._update_property(key, value)
        if hasattr(msg, "partial") and not self._connection.initialized:
            if not msg.partial:
                self._connection.mark_initialized()
        else:
            self._connection.mark_initialized()

    def _on_delta_status(self, msg: SimpleNamespace) -> None:
        self._connection.mark_initialized()
        props = msg.status.__dict__
        for key, value in props.items():
            self._update_property(key, value)

    def _on_response(self, msg: SimpleNamespace) -> None:
        # wattpilot: resolve the waiting command (audit VA-03). An answer
        # without a waiter is still applied and logged, as before.
        future = self._pending_commands.get(_correlation_key(msg.requestId))
        if msg.success:
            props = msg.status.__dict__
            for key, value in props.items():
                self._update_property(key, value)
            if future is not None and not future.done():
                future.set_result(None)
            return
        reason = getattr(msg, "message", "unknown")
        if future is not None and not future.done():
            future.set_exception(
                CommandError(f"Charger rejected command {msg.requestId}: {reason}")
            )
            return
        _LOGGER.error("Command failed (requestId=%s): %s", msg.requestId, reason)

    # The charger's key-to-attribute table, written as a match. Every arm is
    # two lines and independent of the others -- splitting it would turn one
    # readable table into several partial ones.
    def _update_property(  # noqa: C901, PLR0912, PLR0915
        self,
        name: str,
        value: Any,  # noqa: ANN401 -- charger values are dynamically typed
    ) -> None:
        # wattpilot: every status frame arrives here, from the full replay,
        # a delta and a command response alike -- so this is the one place
        # that has to turn the wire shape into plain JSON (audit A11-03).
        value = _plain_json(value)
        self._all_props[name] = value

        match name:
            case "acs":
                self._access_state = value
            case "cbl":
                self._cable_type = value
            case "fhz":
                self._frequency = value
            case "pha":
                self._phases = value
            case "wh":
                self._energy_counter_since_start = value
            case "err":
                self._error_state = value
            case "ust":
                self._cable_lock = value
            case "eto":
                self._energy_counter_total = value
            case "cae":
                self._cae = value
            case "cak":
                self._cak = value
            case "lmo":
                self._mode = value
            case "car":
                self._car_connected = value
            case "alw":
                self._allow_charging = value
            case "nrg":
                self._voltage1 = value[0]
                self._voltage2 = value[1]
                self._voltage3 = value[2]
                self._voltage_n = value[3]
                self._amps1 = value[4]
                self._amps2 = value[5]
                self._amps3 = value[6]
                self._power1 = value[7] * 0.001
                self._power2 = value[8] * 0.001
                self._power3 = value[9] * 0.001
                self._power_n = value[10] * 0.001
                self._power = value[11] * 0.001
            case "amp":
                self._amp = value
            case "version":
                self._version = value
            case "fwv":
                self._firmware = value
            case "wss":
                self._wifi_ssid = value
            case "ccw":
                # Current firmware reports the connected AP here rather than
                # in ``wss``; the value is an object carrying the SSID.
                ssid = _nested_ssid(value)
                if ssid:
                    self._wifi_ssid = ssid

        # Fire property callbacks
        for cb in self._property_callbacks:
            if inspect.iscoroutinefunction(cb):
                task = asyncio.ensure_future(cb(name, value))
                task.add_done_callback(
                    lambda t: t.exception() if not t.cancelled() else None
                )
            else:
                cb(name, value)

    async def _update_hashed_password(self) -> None:
        """
        Hash the password for this device, off the event loop.

        PBKDF2 with 100,000 rounds measured 110-116 ms per handshake, and
        bcrypt is no cheaper. Home Assistant runs this client in its event
        loop, so every connection stalled everything else for that long
        (audit A11-08). The protocol parameters are unchanged.
        """
        if not self._password or not self._device.serial:
            return
        self._hashed_password = await asyncio.to_thread(
            hash_password, self._password, self._device.serial, self._auth_hash_type
        )

    async def _send_command(
        self, message: dict[str, Any], *, secure: bool = False
    ) -> None:
        """
        wattpilot: send a command and wait for its acknowledgement.

        Added here rather than in the caller so every command path gets it.
        A rejection and a silence are both errors -- reporting success for
        either is what made a refused write look like a completed one.
        """
        request_id = message["requestId"]
        key = _correlation_key(request_id)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending_commands[key] = future
        try:
            await self._send(message, secure=secure)
            await asyncio.wait_for(future, self.command_timeout)
        except TimeoutError:
            msg = (
                f"Charger did not answer command {request_id} "
                f"({message.get('key')}) within {self.command_timeout}s"
            )
            raise CommandError(msg) from None
        finally:
            self._pending_commands.pop(key, None)

    async def _send(self, message: dict[str, Any], *, secure: bool = False) -> None:
        if secure:
            message = sign_secured_message(message, self._hashed_password)

        # wattpilot: same reason as on receive -- a secured message carries
        # its signature, and setValue carries whatever is being written.
        _LOGGER.debug("Sending: %s (key=%s)", message.get("type"), message.get("key"))
        await self._connection.send(json.dumps(message))

    def __str__(self) -> str:
        """Return a short human-readable summary of the charger's state."""
        if not self.connected:
            return "Not connected"
        lines = [
            f"Wattpilot: {self.name}",
            f"Serial: {self.serial}",
            f"Connected: {self.connected}",
            f"Car Connected: {self.car_connected}",
            f"Charge Status: {self.allow_charging}",
            f"Mode: {self.mode}",
            f"Power: {self.amp}",
        ]
        if self.power is not None:
            lines.append(
                f"Charge: {self.power:.2f}kW -- "
                f"{self.voltage1}V/{self.voltage2}V/{self.voltage3}V -- "
                f"{self.amps1:.2f}A/{self.amps2:.2f}A/{self.amps3:.2f}A -- "
                f"{self.power1:.2f}kW/{self.power2:.2f}kW/{self.power3:.2f}kW"
            )
        return "\n".join(lines)

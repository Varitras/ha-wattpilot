"""Async WebSocket client for Fronius Wattpilot devices."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from collections.abc import Callable
from datetime import datetime, time
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
    WattpilotError,
)
from .models import AuthHashType, CloudInfo, DeviceInfo, LoadMode

_LOGGER = logging.getLogger(__name__)

WPFLEX_DEVICE_TYPE = "wattpilot_flex"
# Enough to tell a user's chargers apart in the log, too few to name one.
_SERIAL_VISIBLE_DIGITS = 2
# Seconds between two attempts to reach a charger that is rebooting.
_REBOOT_RETRY_PAUSE = 2.0
# Recorded on firmware 42.5: rst=1 is never answered -- the charger goes
# silent and drops the socket to reboot. Waiting timed out on every restart.
_NEVER_ACKNOWLEDGED = frozenset({"rst"})
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


def mask_serial(serial: str | None) -> str:
    """
    wattpilot: a serial fit for the log, which users attach to public issues.

    Only the last digits stay readable; a serial this short is masked whole.
    """
    if serial is None:
        return "<unknown>"
    if len(serial) <= _SERIAL_VISIBLE_DIGITS:
        return "*" * len(serial)
    hidden = len(serial) - _SERIAL_VISIBLE_DIGITS
    return "*" * hidden + serial[hidden:]


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

        async with Wattpilot("192.0.2.10", "mypassword") as wp:
            print(wp.all_properties.get("amp"))
            await wp.set_property("amp", 16)
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
        # Pending setValue commands, so a rejection reaches its caller (VA-03).
        # Keyed by string: ids go out as int, but answers may carry them as
        # strings, and an unnormalized lookup missed those (audit A11-01).
        self._pending_commands: dict[str, asyncio.Future[None]] = {}
        self.command_timeout = 10.0
        self._all_props: dict[str, Any] = {}

        # Named property caches
        self._amp: int | None = None
        self._version: str | None = None
        self._firmware: str | None = None
        self._wifi_ssid: str | None = None
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
        # Same cleanup scope as the handshake: a cancellation between the two
        # left reader and socket alive (audit A12-03).
        try:
            await self._load_api_definition()
        except BaseException:
            await self._connection.close()
            raise
        _LOGGER.info("Connected to Wattpilot %s", mask_serial(self._device.serial))

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
    def refusal(self) -> WattpilotError | None:
        """Return why the client stopped reconnecting for good, if it did."""
        return self._connection.fatal_error

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
        """Return the charger's name: its host name, Wattpilot_<serial>."""
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
    def amp(self) -> int | None:
        """Return the requested charging current in A."""
        return self._amp

    @property
    def wifi_ssid(self) -> str | None:
        """Return the SSID the charger is connected to."""
        return self._wifi_ssid

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

    # Firmware

    @property
    def available_firmware_versions(self) -> list[str]:
        """List of available firmware versions for update."""
        val = self._all_props.get("onv")
        if isinstance(val, list):
            return [str(v) for v in val]
        if isinstance(val, str) and val:
            return [val]
        return []

    # Cloud API

    @property
    def cloud_api_key(self) -> str | None:
        """Cloud API key (available when cloud is enabled)."""
        return self._cak

    # ---- Commands ----

    async def set_property(self, name: str, value: Any) -> None:  # noqa: ANN401 -- charger values are dynamically typed
        """Set one property, coerced to the type the API definition declares."""
        value = self._coerce_value(name, value)
        self._request_id += 1
        message: dict[str, Any] = {
            "type": "setValue",
            "requestId": self._request_id,
            "key": name,
            "value": value,
        }
        secure = self._device.secured is not None and self._device.secured > 0
        if name in _NEVER_ACKNOWLEDGED:
            await self._send(message, secure=secure)
            return
        # Wait for the answer, not just the send (audit VA-03).
        await self._send_command(message, secure=secure)

    async def set_power(self, amperage: int) -> None:
        """Set the charging amperage."""
        await self.set_property("amp", amperage)

    async def set_mode(self, mode: LoadMode) -> None:
        """Set the load mode."""
        await self.set_property("lmo", int(mode))

    async def set_next_trip(
        self,
        departure_time: time | datetime,
    ) -> None:
        """
        Schedule the next trip departure time, as plain local wall-clock time.

        No daylight-saving adjustment: the charger stores ``ftt`` as seconds
        since local midnight and reads it back the same way.
        """
        if isinstance(departure_time, datetime):
            departure_time = departure_time.time()

        timestamp = (
            departure_time.hour * 3600
            + departure_time.minute * 60
            + departure_time.second
        )

        # No DST hour. Measured 2026-09-06, summer time, `tds` = 1: 07:30 set
        # in the app reads back as 27000, not 30600; adding the hour made it
        # come back as 08:30 (audit A12-06).
        await self.set_property("ftt", timestamp)

    async def set_next_trip_energy(self, energy_wh: float) -> None:
        """
        Set the energy requirement for the next trip, in Wh.

        Sets `esk` first; measured on FW 42.5, `fte` is then Wh (25000 = 25 kWh).
        """
        # FBT003 does not apply: the bool is the value being written to the
        # charger, not a flag steering this function.
        await self.set_property("esk", True)  # noqa: FBT003
        await self.set_property("fte", energy_wh)

    async def enable_cloud_api(
        self,
        *,
        timeout: float = 10.0,  # noqa: ASYNC109 -- part of this client's API
    ) -> CloudInfo:
        """Enable the go-e Cloud API and wait up to *timeout* seconds for the key."""
        key_arrived = asyncio.Event()

        def note_key(name: str, value: Any) -> None:  # noqa: ANN401 -- wire value
            if name == "cak" and value:
                key_arrived.set()

        unsubscribe = self.on_property_change(note_key)
        try:
            await self.set_property("cae", True)  # noqa: FBT003 -- a written value
            if not self._cak:
                async with asyncio.timeout(timeout):
                    await key_arrived.wait()
        except TimeoutError as err:
            msg = "Timeout waiting for cloud API key"
            raise WattpilotConnectionError(msg) from err
        finally:
            unsubscribe()

        api_key = self._cak
        if not api_key:
            msg = "Cloud API key arrived empty"
            raise WattpilotConnectionError(msg)
        return CloudInfo(
            enabled=True,
            api_key=api_key,
            url=f"{CLOUD_API_BASE_URL}/{self.serial}",
        )

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

        # A monotonic deadline: a sum of sleeps never counted the reconnect
        # waits and overran the two-minute budget by half an hour (A12-08).
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
        # Another close (an unload) ends the wait; reconnecting outlived it (R1-11).
        closes = self._connection.close_count
        while self._connection.close_count == closes:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            if await self._reconnect_within(remaining):
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_REBOOT_RETRY_PAUSE, remaining))

        # The update closed the connection to reconnect by itself; giving up
        # without handing it back left the updated charger unreached (A18-06).
        self._connection.resume_reconnecting()
        msg = "Timeout reconnecting after firmware update"
        raise WattpilotConnectionError(msg)

    async def _reconnect_within(self, remaining: float) -> bool:
        """Make one attempt; a refusal ends the wait with its reason."""
        try:
            async with asyncio.timeout(remaining):
                await self.connect()
        except Exception:
            if self.refusal is not None:
                # Retried, it ran into the deadline and was reported as a
                # timeout (reaudit R1-13).
                raise
            return False
        return True

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
        self._api_def_cache = await asyncio.to_thread(load_api_definition)

    def _get_api_def(self) -> ApiDefinition:
        """Return the cached API definition, reading it if connect did not."""
        if self._api_def_cache is None:
            self._api_def_cache = load_api_definition()
        return self._api_def_cache

    def _coerce_value(self, name: str, value: Any) -> Any:  # noqa: ANN401 -- charger values are dynamically typed
        """Coerce *value* to the protocol type expected for property *name*."""
        if isinstance(value, SimpleNamespace):
            return value.__dict__

        api_def = self._get_api_def()
        prop_def = api_def.properties.get(name)
        if prop_def is None:
            return value

        # A value, not a failed conversion: dwo=null switches the limit off.
        if value is None and str(prop_def.get("type", "")).startswith("optional<"):
            return None

        return self._coerce_to_json_type(value, prop_def.get("jsonType", ""), name)

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
        _LOGGER.debug("Connected to Wattpilot serial %s", mask_serial(msg.serial))
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

        Hello comes on every connection, reconnects included, so the check
        lives here: DHCP can hand the address to another device, and a
        reconnect kept the entry pointed at whatever answered (audit A11-02).
        """
        known = self._device.serial
        if not known or known == serial:
            return True
        msg = (
            f"Expected charger {mask_serial(known)}, but {mask_serial(serial)} answered"
        )
        await self._connection.reject(DeviceIdentityError(msg, found=serial))
        return False

    async def _on_auth_required(self, msg: SimpleNamespace) -> None:
        if hasattr(msg, "hash"):
            if msg.hash not in AuthHashType:
                # Unanswered, this was a timeout with no cause (A18-18).
                msg_text = f"Unsupported password hash: {msg.hash}"
                await self._connection.reject(WattpilotConnectionError(msg_text))
                return
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
        _LOGGER.debug("Authentication successful")

    def _on_auth_error(self, msg: SimpleNamespace) -> None:
        error_msg = getattr(msg, "message", "Unknown auth error")
        _LOGGER.error("Authentication failed: %s", error_msg)
        self._connection.fail(AuthenticationError(error_msg))

    def _on_full_status(self, msg: SimpleNamespace) -> None:
        self._apply_status(msg.status)
        self._connection.receive_full_status(partial=getattr(msg, "partial", False))

    def _on_delta_status(self, msg: SimpleNamespace) -> None:
        self._connection.receive_delta_status()
        self._apply_status(msg.status)

    def _apply_status(self, status: object) -> None:
        # wattpilot: one property at a time. A value this client cannot
        # read cost the rest of its frame, and in the last chunk of the
        # first full status the status never completed (audit A18-16).
        # A status that is no object (`null`) has nothing to apply.
        for key, value in getattr(status, "__dict__", {}).items():
            try:
                self._update_property(key, value)
            except ValueError, TypeError, LookupError, AttributeError:
                _LOGGER.warning("Ignoring an unreadable value for %s", key)

    def _on_response(self, msg: SimpleNamespace) -> None:
        # wattpilot: resolve the waiting command (audit VA-03). An answer
        # without a waiter -- or without an id at all -- is still applied
        # and logged.
        request_id = getattr(msg, "requestId", None)
        future = self._pending_commands.get(_correlation_key(request_id))
        if msg.success:
            # Nothing in the protocol promises a status on every acceptance.
            self._apply_status(getattr(msg, "status", None))
            if future is not None and not future.done():
                future.set_result(None)
            return
        reason = getattr(msg, "message", "unknown")
        if future is not None and not future.done():
            future.set_exception(
                CommandError(f"Charger rejected command {request_id}: {reason}")
            )
            return
        _LOGGER.error("Command failed (requestId=%s): %s", request_id, reason)

    # The charger's key-to-attribute table, written as a match. Every arm is
    # two lines and independent of the others -- splitting it would turn one
    # readable table into several partial ones.
    def _update_property(
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
            case "cak":
                self._cak = value
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

        PBKDF2 measured 110-116 ms per handshake on the event loop (A11-08).
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

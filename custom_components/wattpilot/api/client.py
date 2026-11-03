"""Async WebSocket client for Fronius Wattpilot devices."""

from __future__ import annotations

import asyncio
import contextlib
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
    device_text,
)
from .models import AuthHashType, CloudInfo, DeviceInfo, missing_field

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

type PropertyCallback = Callable[[str, Any], None]


def _plain_json(value: Any) -> Any:  # noqa: ANN401 -- recurses over untyped JSON
    """
    Turn the wire shape into plain JSON.

    The frame is parsed a second time with an object_hook that builds a
    SimpleNamespace per nested object, because the message handlers read
    fields by attribute. Everything downstream -- entities, diagnostics, the
    redaction that walks dictionaries and lists -- expects plain containers,
    and a namespace slipped past all of it.
    """
    if isinstance(value, SimpleNamespace):
        return {key: _plain_json(item) for key, item in vars(value).items()}
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return value


def _status_object(msg: SimpleNamespace) -> SimpleNamespace:
    """Return a status frame's status; anything else completed it unread."""
    if isinstance(status := getattr(msg, "status", None), SimpleNamespace):
        return status
    msg_text = "Status frame without a status object, skipped as unreadable"
    raise TypeError(msg_text)


def _correlation_key(request_id: object) -> str:
    """
    Return one spelling for a request id, whatever the wire used.

    Commands are sent with an integer; the protocol's own examples answer
    with a string. Comparing them raw left the caller waiting for an answer
    that had already arrived.
    """
    return str(request_id)


def mask_serial(serial: str | None) -> str:
    """
    Return a serial fit for the log, which users attach to public issues.

    Only the last digits stay readable; a serial this short is masked whole.
    """
    if serial is None:
        return "<unknown>"
    if len(serial) <= _SERIAL_VISIBLE_DIGITS:
        return "*" * len(serial)
    hidden = len(serial) - _SERIAL_VISIBLE_DIGITS
    return "*" * hidden + serial[hidden:]


def _message_type(raw: str) -> str:
    """Return the frame's type, for logging without its payload."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return "<unparsable>"
    if isinstance(parsed, dict):
        return str(parsed.get("type", "<untyped>"))
    return "<not an object>"


# A conversion that would change the value is refused, not made: the lock
# option "never locked" (3) once went out as true, 12345.7 Wh as 12345.
_BOOL_WORDS = dict.fromkeys(("true", "1", "yes"), True) | dict.fromkeys(
    ("false", "0", "no"), False
)


def _as_bool(value: Any, name: str) -> bool:  # noqa: ANN401 -- a value to write
    """Return *value* as a bool, if it says true or false and nothing else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in _BOOL_WORDS:
        return _BOOL_WORDS[value.lower()]
    if value in (0, 1):
        return bool(value)
    msg = f"Cannot convert {value!r} to bool for property '{name}'"
    raise PropertyError(msg)


def _as_int(value: Any, name: str) -> int:  # noqa: ANN401 -- a value to write
    """Return *value* as an int, if it is a whole number."""
    if isinstance(value, str):
        value = _parse_number(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    msg = f"Cannot convert {value!r} to int for property '{name}'"
    raise PropertyError(msg)


def _as_float(value: Any, name: str) -> float:  # noqa: ANN401 -- a value to write
    """Return *value* as a float, if it is a number."""
    if isinstance(value, str):
        value = _parse_number(value)
    if isinstance(value, int | float):
        return float(value)
    msg = f"Cannot convert {value!r} to float for property '{name}'"
    raise PropertyError(msg)


def _as_text(value: Any, name: str) -> str:  # noqa: ANN401 -- a value to write
    """Return *value* if it is text already: str(5) is no text the caller meant."""
    if isinstance(value, str):
        return value
    msg = f"Cannot convert {value!r} to str for property '{name}'"
    raise PropertyError(msg)


def _parse_number(text: str) -> int | float | str:
    """Read a number from text, or give the text back unchanged."""
    for parse in (int, float):
        with contextlib.suppress(ValueError):
            return parse(text)
    return text


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
        self._hashed_from: tuple[str, str, AuthHashType] | None = None
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
        # Pending setValue commands, so a rejection reaches its caller.
        # Keyed by string: ids go out as int, but answers may carry them as
        # strings, and an unnormalized lookup missed those.
        self._pending_commands: dict[str, asyncio.Future[None]] = {}
        self.command_timeout = 10.0
        self._all_props: dict[str, Any] = {}

        # Named property caches
        self._firmware: str | None = None
        self._cak: str | None = None

        # Lazy-loaded API definition for type coercion
        self._api_def_cache: ApiDefinition | None = None

        # Callbacks
        self._property_callbacks: list[PropertyCallback] = []
        self.installing_firmware = False

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
        # left reader and socket alive.
        try:
            await self._load_api_definition()
        except BaseException:
            await self._connection.close()
            raise
        _LOGGER.debug("Connected to Wattpilot %s", mask_serial(self._device.serial))

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        await self._connection.close()

    def _fail_pending_commands(self, reason: str) -> None:
        """Never leave a caller awaiting an answer that cannot come."""
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
    def manufacturer(self) -> str:
        """Return the manufacturer as the charger reports it."""
        return self._device.manufacturer

    @property
    def firmware(self) -> str | None:
        """Return the charger's firmware version, if known."""
        return self._firmware

    def get_property(self, name: str, default: Any = None) -> Any:  # noqa: ANN401 -- a charger value
        """Return one property, without the copy all_properties makes."""
        return self._all_props.get(name, default)

    @property
    def all_properties(self) -> dict[str, Any]:
        """Return a copy of every property seen so far."""
        return dict(self._all_props)

    @property
    def properties_initialized(self) -> bool:
        """Return whether the charger has sent its full state once."""
        return self._connection.initialized

    # Device info

    @property
    def variant(self) -> object:
        """Maximum-current variant as the charger sends it: the integer 11 or 22."""
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
        # Wait for the answer, not just the send.
        await self._send_command(message, secure=secure)

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
        # come back as 08:30.
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

        Without *version*, the first one offered; :class:`PropertyError` if none.
        Raises :class:`WattpilotConnectionError` on timeout.
        """
        if version is None:
            versions = self.available_firmware_versions
            if not versions:
                msg = "No firmware updates available"
                raise PropertyError(msg)
            version = versions[0]

        self.installing_firmware = True
        try:
            await self._install_and_reconnect(version, timeout)
        finally:
            self.installing_firmware = False

    async def _install_and_reconnect(self, version: str, timeout: float) -> None:  # noqa: ASYNC109 -- part of this client's API
        # Counted from here: a close in the wait for the reboot ended that
        # wait as if the charger had gone, and the update reconnected.
        closes = self._connection.close_count
        await self.set_property("oct", version)

        # A monotonic deadline: a sum of sleeps never counted the reconnect
        # waits and overran the two-minute budget by half an hour.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._connection.wait_disconnected(), timeout)

        if self.connected:
            msg = "Charger did not disconnect for firmware update"
            raise WattpilotConnectionError(msg)

        # No disconnect() of its own: it counted as an unload.
        await self._reconnect_before(deadline, closes)

    async def _reconnect_before(self, deadline: float, closes: int) -> None:
        """
        Keep trying to reach the rebooted charger until the deadline passes.

        Every attempt is bounded by what is left of the budget, and so is the
        pause between them: the caller's timeout used to be a sum of sleeps,
        and an attempt could sit in a full authentication wait for free.
        """
        loop = asyncio.get_running_loop()
        # Another close (an unload) ends the wait; reconnecting outlived it.
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

        # A failed attempt may have stopped the reader that would reconnect.
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
                # Retried, it ended up reported as a timeout.
                raise
            return False
        return True

    # ---- Type coercion ----

    async def _load_api_definition(self) -> None:
        """
        Read the API definition into the cache, off the event loop.

        The definition is a large YAML file, and the lazy path
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

    def _coerce_to_json_type(
        self,
        value: Any,  # noqa: ANN401 -- whatever the charger declared for this key
        json_type: str,
        name: str,
    ) -> Any:  # noqa: ANN401 -- the coerced value keeps the charger's own type
        """Convert *value* to the specified JSON type for property *name*."""
        match json_type:
            case "boolean":
                return _as_bool(value, name)
            case "integer":
                return _as_int(value, name)
            case "float":
                return _as_float(value, name)
            case "string":
                return _as_text(value, name)
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

    # ---- Internal: message loop ----

    # One branch per message type the charger sends; the dispatch is the
    # function's whole content. The complexity ratchet in tests/ freezes
    # the number so it cannot creep upward unnoticed.
    async def _handle_message(self, raw: str) -> None:
        # Type only, never the payload. Frames carry Wi-Fi passwords, cloud
        # tokens and OCPP keys, and DEBUG logging is one toggle away in the UI.
        _LOGGER.debug("Message received: %s (%d bytes)", _message_type(raw), len(raw))
        msg = json.loads(raw)
        msg_type = msg.get("type", "")
        ns = json.loads(raw, object_hook=lambda d: SimpleNamespace(**d))

        match msg_type:
            case "hello" | "authRequired":
                await self._on_handshake(msg_type, ns)
            case "authSuccess":
                self._on_auth_success(ns)
            case "authError":
                await self._on_auth_error(ns)
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

    async def _on_handshake(self, msg_type: str, ns: SimpleNamespace) -> None:
        field = missing_field(msg_type, vars(ns), self._device.serial)
        if field is not None:
            text = f"Charger sent {msg_type} without a usable {field}"
            await self._connection.reject(WattpilotConnectionError(text))
        elif msg_type == "hello":
            await self._on_hello(ns)
        else:
            await self._on_auth_required(ns)

    async def _on_hello(self, msg: SimpleNamespace) -> None:
        if not await self._identity_accepted(msg.serial):
            return
        _LOGGER.debug("Connected to Wattpilot serial %s", mask_serial(msg.serial))
        self._device.serial = msg.serial
        if hasattr(msg, "hostname"):
            self._device.name = msg.hostname
        self._device.manufacturer = getattr(msg, "manufacturer", "")
        self._device.device_type = getattr(msg, "devicetype", "")
        if hasattr(msg, "secured"):
            self._device.secured = msg.secured

    async def _identity_accepted(self, serial: str) -> bool:
        """
        Refuse a connection that is not the charger this client already knows.

        Hello comes on every connection, reconnects included, so the check
        lives here: DHCP can hand the address to another device, and a
        reconnect kept the entry pointed at whatever answered.
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
                # Unanswered, this was a timeout with no cause.
                msg_text = f"Unsupported password hash: {device_text(msg.hash)}"
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

    async def _on_auth_error(self, msg: SimpleNamespace) -> None:
        error_msg = device_text(getattr(msg, "message", "Unknown auth error"))
        _LOGGER.debug("Authentication failed: %s", error_msg)
        # Rejected, not failed: after authSuccess the socket stayed open.
        await self._connection.reject(AuthenticationError(error_msg))

    def _on_full_status(self, msg: SimpleNamespace) -> None:
        self._apply_status(_status_object(msg))
        self._connection.receive_full_status(partial=getattr(msg, "partial", False))

    def _on_delta_status(self, msg: SimpleNamespace) -> None:
        status = _status_object(msg)
        self._connection.receive_delta_status()
        self._apply_status(status)

    def _apply_status(self, status: object) -> None:
        # One property at a time. A value this client cannot read cost the
        # rest of its frame, and in the last chunk of the first full status
        # the status never completed. No object (`null`): nothing to apply.
        for key, value in getattr(status, "__dict__", {}).items():
            try:
                self._update_property(key, value)
            except (
                ValueError,
                TypeError,
                LookupError,
                AttributeError,
                RecursionError,
            ) as exc:
                self._connection.skip(f"the value of {key}", exc)

    def _on_response(self, msg: SimpleNamespace) -> None:
        # Without a waiter or an id, an answer is still applied and logged.
        request_id = getattr(msg, "requestId", None)
        future = self._pending_commands.get(_correlation_key(request_id))
        # Truthy, not only a JSON true: the Home sends true, the Flex
        # was never measured, and refusing a real acceptance fails every write.
        if getattr(msg, "success", None):
            # Nothing in the protocol promises a status on every acceptance.
            self._apply_status(getattr(msg, "status", None))
            if future is not None and not future.done():
                future.set_result(None)
            return
        reason = device_text(getattr(msg, "message", "unknown"))
        if future is not None and not future.done():
            future.set_exception(
                CommandError(f"Charger rejected command {request_id}: {reason}")
            )
            return
        _LOGGER.error("Command failed (requestId=%s): %s", request_id, reason)

    # The charger's key-to-attribute table, written as a match: splitting it
    # would turn one readable table into several partial ones.
    def _update_property(
        self,
        name: str,
        value: Any,  # noqa: ANN401 -- charger values are dynamically typed
    ) -> None:
        # Every status frame arrives here, from the full replay,
        # a delta and a command response alike -- so this is the one place
        # that has to turn the wire shape into plain JSON.
        value = _plain_json(value)
        self._all_props[name] = value

        match name:
            case "cak":
                self._cak = value
            case "fwv":
                self._firmware = value

        for cb in self._property_callbacks:
            cb(name, value)

    async def _update_hashed_password(self) -> None:
        """
        Hash the password for this device, off the event loop.

        PBKDF2 measured 110-116 ms per handshake on the event loop.
        """
        if not self._password or not self._device.serial:
            return
        inputs = (self._password, self._device.serial, self._auth_hash_type)
        # The result never changes for the same inputs, and
        # recomputing it delayed every reconnect's auth reply by that much.
        if inputs == self._hashed_from:
            return
        self._hashed_password = await asyncio.to_thread(hash_password, *inputs)
        self._hashed_from = inputs

    async def _send_command(
        self, message: dict[str, Any], *, secure: bool = False
    ) -> None:
        """
        Send a command and wait for its acknowledgement.

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

        # Same reason as on receive -- a secured message carries
        # its signature, and setValue carries whatever is being written.
        _LOGGER.debug("Sending: %s (key=%s)", message.get("type"), message.get("key"))
        await self._connection.send(json.dumps(message))

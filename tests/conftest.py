"""Shared fixtures. FakeWattpilot mimics the wattpilot-api surface we use."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.wattpilot import _SerialMask

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    no_identifier_reaches_the_log: None,
    drop_serial_masks: None,
    enable_custom_integrations: None,
) -> None:
    """
    Allow HA to load custom_components/wattpilot in tests.

    After the log fixtures, which then outlast hass: autouse fixtures are set
    up by name, so the stop of Home Assistant went unchecked (PC5-T-03).
    """


# Serials and addresses the tests hand out. None may reach a log record
# unmasked, tracebacks included: the static guard was widened three rounds
# running and still missed shapes, so every test now checks the real output
# (reaudit RF-P-03). Any IPv4 literal, as a list of the tests' addresses
# missed four (post-check PC5-T-04); 192.0.2.1 is the redaction's stand-in.
IDENTIFIERS = re.compile(
    r"(?<![\d.])(?:123456|654321|999999|111111|222222|333333"
    r"|(?!192\.0\.2\.1(?![\d]))\d{1,3}(?:\.\d{1,3}){3})(?![\d])"
)
# Entity IDs of a device named after its host name carry the serial: the
# accepted risk L-027 for installations from before names were stored.
ENTITY_ID = re.compile(r"\b[a-z_]+\.[a-z0-9_]+")


class _LogLines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture(autouse=True)
def no_identifier_reaches_the_log(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail a test whose log names a charger; mark the few that must."""
    handler = _LogLines()
    # Home Assistant's setup DEBUG lines too: it logs a setup retry's
    # traceback there (post-check PC5-T-04).
    loggers = [
        logging.getLogger(name)
        for name in (
            "custom_components.wattpilot",
            "homeassistant.config_entries",
            "homeassistant.setup",
        )
    ]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    yield
    logging.getLogger().removeHandler(handler)
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)
    if request.node.get_closest_marker("logs_identifiers"):
        return
    leaked = [
        line for line in handler.lines if IDENTIFIERS.search(ENTITY_ID.sub("", line))
    ]
    assert not leaked, f"identifier(s) in the log: {leaked[:3]}"


@pytest.fixture(autouse=True)
def drop_serial_masks() -> Iterator[None]:
    """The mask stays for the process's life; tests start many Home Assistants."""
    yield
    for logger in (
        logging.getLogger("homeassistant.config_entries"),
        logging.getLogger("custom_components.wattpilot"),
    ):
        for log_filter in list(logger.filters):
            if isinstance(log_filter, _SerialMask):
                logger.removeFilter(log_filter)


@pytest.fixture(name="device_properties")
def device_properties_fixture() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "device_properties.json"
    return json.loads(path.read_text(encoding="utf-8"))


class FakeWattpilot:
    """Duck-typed stand-in for wattpilot_api.Wattpilot."""

    def __init__(self, properties: dict[str, Any] | None = None) -> None:
        self._properties: dict[str, Any] = dict(properties or {})
        self.connected = False
        # The real client's full-status flag; tests of a replay clear it.
        self.properties_initialized = True
        # Why the real client stopped reconnecting for good, if it did.
        self.refusal: Exception | None = None
        self.serial = "123456"
        self.name = "Wattpilot"
        self.manufacturer = "fronius"
        self.firmware = "42.5"
        self.model = "Wattpilot Home 11 J 2.0"
        # Deliberately an int: the real device reports "var" as a number,
        # contrary to the vendor client's `-> str | None` annotation.
        self.variant = 11
        self.connect_error: Exception | None = None
        self.set_error: Exception | None = None
        self.disconnect_error: Exception | None = None
        self.disconnect_attempts = 0
        # Raised by every write path except set_property (which has its own
        # hook above), so one test can walk them all.
        self.write_error: Exception | None = None
        self.set_calls: list[tuple[str, Any]] = []
        self.next_trip_calls: list[Any] = []
        self.next_trip_energy_calls: list[float] = []
        self.install_calls: list[str | None] = []
        self.disconnect_count = 0
        self.disable_cloud_api_count = 0
        # 32 chars: same shape as a real go-e cloud API key. Settable so
        # redaction tests can exercise short/empty keys too.
        # Prefix, body and suffix deliberately distinguishable. A uniform
        # key ("k" * 32) let the redaction test pass while 31 of its 32
        # characters were logged: the expected fragment "kkkk" matched the
        # leak just as well as it matched a correct redaction.
        self.cloud_api_key = "ABCD" + "s" * 24 + "WXYZ"
        self._callbacks: list[Callable[[str, Any], None]] = []

    @property
    def all_properties(self) -> dict[str, Any]:
        """Mirror the real client: a fresh copy per access, not a live view."""
        return dict(self._properties)

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        # The real client replays the whole property snapshot to its
        # subscribers as soon as the connection is up (_on_full_status ->
        # _update_property -> callbacks). Without that here, a reconnect
        # that registers its callbacks too late looks identical to one that
        # gets it right -- which is how FakeWattpilot hid the blocking-I/O
        # bug once already.
        for key, value in list(self._properties.items()):
            for callback in list(self._callbacks):
                callback(key, value)

    async def disconnect(self) -> None:
        self.disconnect_attempts += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error
        self.connected = False
        self.disconnect_count += 1

    def on_property_change(
        self, callback: Callable[[str, Any], None]
    ) -> Callable[[], None]:
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    def push(self, key: str, value: Any) -> None:
        """Test helper: simulate a device push."""
        self._properties[key] = value
        for callback in list(self._callbacks):
            callback(key, value)

    async def set_property(self, key: str, value: Any) -> None:
        if self.set_error is not None:
            raise self.set_error
        self.set_calls.append((key, value))
        self._properties[key] = value

    async def set_next_trip(self, value: Any) -> None:
        self._raise_write_error()
        self.next_trip_calls.append(value)

    async def set_next_trip_energy(self, energy_wh: float) -> None:
        self._raise_write_error()
        self.next_trip_energy_calls.append(energy_wh)

    async def enable_cloud_api(self) -> Any:
        self._raise_write_error()
        return type(
            "CloudInfo",
            (),
            {"enabled": True, "api_key": self.cloud_api_key, "url": "https://example"},
        )()

    async def disable_cloud_api(self) -> None:
        self._raise_write_error()
        self.disable_cloud_api_count += 1
        self._properties["cae"] = False

    async def install_firmware_update(self, version: str | None = None) -> None:
        self._raise_write_error()
        self.install_calls.append(version)
        self._properties["fwv"] = version

    def _raise_write_error(self) -> None:
        if self.write_error is not None:
            raise self.write_error


@pytest.fixture(name="fake_charger")
def fake_charger_fixture(device_properties: dict[str, Any]) -> FakeWattpilot:
    return FakeWattpilot(device_properties)

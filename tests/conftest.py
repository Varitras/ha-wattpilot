"""Shared fixtures. FakeWattpilot mimics the wattpilot-api surface we use."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow HA to load custom_components/wattpilot in tests."""


@pytest.fixture(name="device_properties")
def device_properties_fixture() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "device_properties.json"
    return json.loads(path.read_text(encoding="utf-8"))


class FakeWattpilot:
    """Duck-typed stand-in for wattpilot_api.Wattpilot."""

    def __init__(self, properties: dict[str, Any] | None = None) -> None:
        self._properties: dict[str, Any] = dict(properties or {})
        self.connected = False
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
        # Raised by every write path except set_property (which has its own
        # hook above), so one test can walk them all.
        self.write_error: Exception | None = None
        self.set_calls: list[tuple[str, Any]] = []
        self.next_trip_calls: list[Any] = []
        self.next_trip_energy_calls: list[float] = []
        self.install_calls: list[str | None] = []
        self.disconnect_count = 0
        self.disable_cloud_api_count = 0
        # Mirrors the real client's lazy-loaded API definition cache: a
        # counter (not a bool) so tests can assert it warms exactly once,
        # plus the calling thread's identity so tests can prove the warm-up
        # actually ran off the event loop thread.
        self.api_def_loads = 0
        self.api_def_load_thread_ident: int | None = None
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

    async def set_next_trip_energy(self, energy_kwh: float) -> None:
        self._raise_write_error()
        self.next_trip_energy_calls.append(energy_kwh)

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

    def _get_api_def(self) -> None:
        """Mirror the real client's lazy, instance-cached API definition load."""
        self.api_def_loads += 1
        self.api_def_load_thread_ident = threading.get_ident()


@pytest.fixture(name="fake_charger")
def fake_charger_fixture(device_properties: dict[str, Any]) -> FakeWattpilot:
    return FakeWattpilot(device_properties)

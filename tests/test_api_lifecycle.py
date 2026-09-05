"""Connection lifecycle, subscriptions and the read-only surface.

What the integration depends on beyond commands: that every getter answers,
that connect gives up rather than hangs, and that the message loop keeps
running when a frame is bad.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.api.exceptions import (
    DeviceIdentityError,
    PropertyError,
    WattpilotConnectionError,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def client() -> Wattpilot:
    return Wattpilot("192.0.2.10", "secret")


def read_only_properties() -> list[str]:
    """Derived, not listed: the client has 75 of these and the list would be
    stale by the next property. The count assertion below is what keeps the
    sweep from silently becoming empty."""
    return sorted(
        name
        for name in dir(Wattpilot)
        if isinstance(getattr(Wattpilot, name, None), property)
    )


def test_every_getter_answers_on_a_fresh_client(client: Wattpilot) -> None:
    """Before the first frame arrives, nothing may raise: Home Assistant
    builds its entities from these while the charger is still connecting,
    and one AttributeError there takes the whole platform down."""
    names = read_only_properties()
    assert len(names) > 50, "the introspection found almost nothing -- it broke"

    for name in names:
        getattr(client, name)


def test_every_getter_still_answers_once_properties_arrived(
    client: Wattpilot,
) -> None:
    """The other half: many getters read out of the property cache, and the
    conversions they do only run when there is something to convert."""
    client._all_props.update(
        {
            "nrg": [230.0] * 11 + [4230],
            "tma": [40.5, 38.0],
            "cards": [{"name": "Card", "cardId": True, "energy": 10}],
            "cci": {"label": "Inverter"},
            "ccw": {"ssid": "TestNet"},
            "ocu": ["43.4"],
            "loc": "2026-08-10T23:28:31.866 +02:00",
        }
    )
    for name in read_only_properties():
        getattr(client, name)


async def test_connect_gives_up_when_authentication_never_answers(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A charger that accepts the socket and then says nothing must end as an
    error, not as a coroutine waiting for a frame that never comes."""

    class SilentSocket:
        async def close(self) -> None:
            return

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> str:
            await asyncio.sleep(3600)
            raise StopAsyncIteration

    async def fake_connect(_url: str) -> SilentSocket:
        return SilentSocket()

    monkeypatch.setattr(
        "custom_components.wattpilot.api.client.websockets.asyncio.client.connect",
        fake_connect,
    )
    client._connect_timeout = 0.01

    with pytest.raises(WattpilotConnectionError, match="Timeout waiting for auth"):
        await client.connect()
    assert not client.connected


async def test_connect_on_an_already_connected_client_waits_for_its_state(
    client: Wattpilot,
) -> None:
    """Reconnecting an established client must not open a second socket; it
    waits for the initialisation it may still be missing."""
    client._connected = True
    client._init_timeout = 0.01

    with pytest.raises(WattpilotConnectionError, match="property initialization"):
        await client.connect()


class FakeSocket:
    """Hands out prepared frames, then blocks the way a live socket does."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> str:
        if self.closed:
            raise StopAsyncIteration
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(3600)
        raise StopAsyncIteration


async def _settle(condition: Callable[[], bool]) -> None:
    """Yield until the loop task has made progress, without a wall-clock wait."""
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)


async def test_a_malformed_frame_does_not_kill_the_message_loop(
    client: Wattpilot,
) -> None:
    """One bad frame must not end the loop -- the charger would still be
    connected, and nothing would ever update again. The predecessor of this
    test called the handler twice and never the loop, so it proved nothing
    about the claim in its own name (audit A11-06)."""
    client._ws = FakeSocket(  # type: ignore[assignment]
        ["{not json", json.dumps({"type": "deltaStatus", "status": {"amp": 6}})]
    )
    task = asyncio.create_task(client._message_loop())
    try:
        await _settle(lambda: client.amp == 6)
        assert client.amp == 6
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_disconnect_closes_the_socket_after_the_loop_failed(
    client: Wattpilot,
) -> None:
    """Whatever ended the message loop, the socket still has to be closed --
    re-raising the loop's exception first left it open (audit A11-06)."""
    socket = FakeSocket([])
    client._ws = socket  # type: ignore[assignment]

    async def failing_loop() -> None:
        msg = "loop died"
        raise RuntimeError(msg)

    client._message_loop_task = asyncio.create_task(failing_loop())
    await _settle(client._message_loop_task.done)

    await client.disconnect()

    assert socket.closed
    assert not client.connected


async def test_a_cancelled_connect_leaves_nothing_running(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant cancels a setup that takes too long. The cleanup used to
    catch Exception, which CancelledError is not, so socket and message loop
    survived the cancelled attempt (audit A11-05)."""
    socket = FakeSocket([])

    async def fake_connect(_url: str) -> FakeSocket:
        return socket

    monkeypatch.setattr(
        "custom_components.wattpilot.api.client.websockets.asyncio.client.connect",
        fake_connect,
    )

    task = asyncio.create_task(client.connect())
    await _settle(lambda: client._message_loop_task is not None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.closed
    assert client._message_loop_task is None


async def test_message_subscribers_see_every_frame(client: Wattpilot) -> None:
    # Subscribers get the decoded frame as a plain dict, before the client
    # turns it into a namespace for its own handlers.
    seen: list[Any] = []
    unsubscribe = client.on_message(lambda message: seen.append(message["type"]))

    await client._handle_message(json.dumps({"type": "deltaStatus", "status": {}}))
    unsubscribe()
    await client._handle_message(json.dumps({"type": "deltaStatus", "status": {}}))

    assert seen == ["deltaStatus"]


async def test_a_firmware_install_without_a_version_needs_an_offer(
    client: Wattpilot,
) -> None:
    """Asking for "the latest" when the charger offers nothing is a mistake
    worth naming, not a write of None."""
    with pytest.raises(PropertyError, match="No firmware updates available"):
        await client.install_firmware_update()


def test_the_string_form_says_whether_it_is_connected(client: Wattpilot) -> None:
    assert str(client) == "Not connected"

    client._connected = True
    client._update_property("nrg", [230.0] * 11 + [4230])
    client._update_property("amp", 16)
    client._update_property("car", 2)
    summary = str(client)
    assert "Serial" in summary
    assert "4.23" in summary


async def test_a_reconnect_to_a_different_charger_is_refused(
    client: Wattpilot,
) -> None:
    """Setup checks the serial once, but the address can be reused by DHCP or
    the hardware replaced. A reconnect kept the config entry, its entities and
    their history pointed at whatever answered (audit A11-02)."""
    client._device.serial = "111111"
    socket = FakeSocket(
        [
            json.dumps({"type": "hello", "serial": "222222"}),
            json.dumps({"type": "deltaStatus", "status": {"amp": 14}}),
        ]
    )
    client._ws = socket  # type: ignore[assignment]

    task = asyncio.create_task(client._message_loop())
    try:
        await _settle(task.done)
        assert socket.closed
        assert client.serial == "111111"
        assert client.amp is None, "state from the wrong charger reached the cache"
        assert not client.connected
        assert isinstance(client._fatal_error, DeviceIdentityError)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_an_explicit_reconnect_waits_for_the_new_snapshot(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit path carried _all_props_initialized over from the previous
    connection, so the first partial replay satisfied readiness and connect()
    returned before the new snapshot arrived (audit A11-07)."""
    client._all_props_initialized = True
    socket = FakeSocket(
        [
            json.dumps({"type": "authSuccess"}),
            json.dumps({"type": "fullStatus", "partial": True, "status": {"amp": 6}}),
        ]
    )

    async def fake_connect(_url: str) -> FakeSocket:
        return socket

    monkeypatch.setattr(
        "custom_components.wattpilot.api.client.websockets.asyncio.client.connect",
        fake_connect,
    )
    client._init_timeout = 0.05

    with pytest.raises(WattpilotConnectionError, match="property initialization"):
        await client.connect()
    assert socket.closed


async def test_an_explicit_reconnect_to_a_different_charger_is_refused(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service-driven path has to refuse the same way the automatic one
    does -- the check sits in the frame both of them send, so this is the
    counter-question to the automatic case rather than a second mechanism
    (audit A11-02)."""
    client._device.serial = "111111"
    socket = FakeSocket([json.dumps({"type": "hello", "serial": "222222"})])

    async def fake_connect(_url: str) -> FakeSocket:
        return socket

    monkeypatch.setattr(
        "custom_components.wattpilot.api.client.websockets.asyncio.client.connect",
        fake_connect,
    )
    client._connect_timeout = 0.5

    with pytest.raises(DeviceIdentityError, match="222222"):
        await client.connect()
    assert socket.closed
    assert client.serial == "111111"

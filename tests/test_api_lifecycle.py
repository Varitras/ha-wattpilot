"""Connection lifecycle, subscriptions and the read-only surface.

What the integration depends on beyond commands: that every getter answers,
that connect gives up rather than hangs, and that the message loop keeps
running when a frame is bad.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import time
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
    """Derived, not listed: a list would be stale by the next property. The
    count assertion below is what keeps the sweep from silently becoming
    empty."""
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
    assert len(names) > 10, "the introspection found almost nothing -- it broke"

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
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        fake_connect,
    )
    client._connection.connect_timeout = 0.01

    with pytest.raises(WattpilotConnectionError, match="Timeout waiting for auth"):
        await client.connect()
    assert not client.connected


async def test_connect_on_an_already_connected_client_waits_for_its_state(
    client: Wattpilot,
) -> None:
    """Reconnecting an established client must not open a second socket; it
    waits for the initialisation it may still be missing."""
    client._connection.mark_authenticated()
    client._connection.init_timeout = 0.01

    with pytest.raises(WattpilotConnectionError, match="property initialization"):
        await client.connect()


class FakeSocket:
    """Hands out prepared frames, then blocks the way a live socket does."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.closed = False
        self._shut = asyncio.Event()

    async def close(self) -> None:
        self.closed = True
        self._shut.set()

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> str:
        if self.closed:
            raise StopAsyncIteration
        if self._frames:
            return self._frames.pop(0)
        # Waiting on the close, not on a long sleep: a real socket ends a
        # pending receive when it is closed. A fake that keeps sleeping lets
        # a test pass that the library would have failed -- which is how the
        # readiness watchdog looked ineffective at first.
        await self._shut.wait()
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
    client._connection.socket = FakeSocket(  # type: ignore[assignment]
        ["{not json", json.dumps({"type": "deltaStatus", "status": {"amp": 6}})]
    )
    task = asyncio.create_task(client._connection._message_loop())
    try:
        await _settle(lambda: client.all_properties.get("amp") == 6)
        assert client.all_properties.get("amp") == 6
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
    client._connection.socket = socket  # type: ignore[assignment]

    async def failing_loop() -> None:
        msg = "loop died"
        raise RuntimeError(msg)

    client._connection.message_loop_task = asyncio.create_task(failing_loop())
    await _settle(client._connection.message_loop_task.done)

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
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        fake_connect,
    )

    task = asyncio.create_task(client.connect())
    await _settle(lambda: client._connection.message_loop_task is not None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.closed
    assert client._connection.message_loop_task is None


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
    client._connection.socket = socket  # type: ignore[assignment]

    task = asyncio.create_task(client._connection._message_loop())
    try:
        await _settle(task.done)
        assert socket.closed
        assert client.serial == "111111"
        assert client.all_properties.get("amp") is None, (
            "state from the wrong charger reached the cache"
        )
        assert not client.connected
        assert isinstance(client._connection.fatal_error, DeviceIdentityError)
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
    client._connection.mark_initialized()
    socket = FakeSocket(
        [
            json.dumps({"type": "authSuccess"}),
            json.dumps({"type": "fullStatus", "partial": True, "status": {"amp": 6}}),
        ]
    )

    async def fake_connect(_url: str) -> FakeSocket:
        return socket

    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        fake_connect,
    )
    client._connection.init_timeout = 0.05

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
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        fake_connect,
    )
    client._connection.connect_timeout = 0.5

    with pytest.raises(DeviceIdentityError) as raised:
        await client.connect()
    assert raised.value.found == "222222", "the setup error names it"
    # The message itself ends up in the log, which users attach to issues.
    assert "222222" not in str(raised.value)
    assert "111111" not in str(raised.value)
    assert client._host not in str(raised.value)
    assert socket.closed
    assert client.serial == "111111"


class SocketFactory:
    """Hands out a fresh FakeSocket per connect and remembers every one."""

    def __init__(self) -> None:
        self.sockets: list[FakeSocket] = []

    async def __call__(self, _url: str) -> FakeSocket:
        # A real connect suspends on the network; a fake that never yields
        # lets two callers run one after the other and hides the overlap.
        await asyncio.sleep(0)
        socket = FakeSocket([])
        self.sockets.append(socket)
        return socket

    @property
    def open_sockets(self) -> list[FakeSocket]:
        return [s for s in self.sockets if not s.closed]


async def test_an_explicit_reconnect_during_backoff_leaves_one_reader(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconnect action while the client is waiting out its retry backoff
    must not leave the old reader running. It still owns a socket, and the
    two then race on the same connection object: measured three sockets and
    two readers, two of them still open after disconnect (audit A12-01)."""
    factory = SocketFactory()
    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        factory,
    )
    connection = client._connection
    connection._reconnect_delay_min = 3600  # park the loop in its backoff

    connection.socket = FakeSocket([])
    factory.sockets.append(connection.socket)
    connection.message_loop_task = asyncio.create_task(connection._message_loop())
    await connection.socket.close()
    await _settle(lambda: not connection.connected)
    first_reader = connection.message_loop_task

    connection.connect_timeout = 0.01
    with contextlib.suppress(WattpilotConnectionError):
        await connection.open()

    assert first_reader is not connection.message_loop_task
    assert first_reader.done(), "the reader from the backoff was left running"
    await connection.close()
    assert factory.open_sockets == [], "a socket outlived the connection"


async def test_two_explicit_opens_leave_no_socket_behind(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two reconnect actions arriving together: each passed the "already
    connected" check, each opened a socket, and the second overwrote the
    references to the first. close() then reached only the last pair, and
    the first socket and its reader lived on unowned (audit A13-01)."""
    factory = SocketFactory()
    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        factory,
    )
    connection = client._connection
    connection.connect_timeout = 0.01

    outcomes = await asyncio.gather(
        connection.open(), connection.open(), return_exceptions=True
    )

    assert all(isinstance(o, WattpilotConnectionError) for o in outcomes)
    await connection.close()
    assert factory.open_sockets == [], "a socket outlived both attempts"


async def test_a_close_during_a_pending_open_stays_closed(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close() ran while open() was still waiting for the connector, found
    neither socket nor reader, and returned. The connector then delivered,
    open() installed socket and reader, and a connection that had just been
    torn down was live again behind the caller's back (audit A14-01)."""
    factory = SocketFactory()
    release = asyncio.Event()

    async def blocked_connect(url: str) -> FakeSocket:
        await release.wait()
        return await factory(url)

    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        blocked_connect,
    )
    connection = client._connection
    opening = asyncio.create_task(connection.open())
    await asyncio.sleep(0)

    await connection.close()
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    # A real charger answers on whatever socket got installed. Without this
    # the fake never authenticates, open() times out, cleans up after itself,
    # and the test passes for the wrong reason -- which is what it first did.
    if connection.socket is not None:
        connection.mark_authenticated()
        connection.mark_initialized()
    with pytest.raises(WattpilotConnectionError):
        await opening

    assert connection.message_loop_task is None
    assert connection.socket is None
    assert factory.open_sockets == [], "the late socket was left open"


async def test_an_open_queued_before_a_close_does_not_outlive_it(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two opens, then a close, all requested before any connector returned.
    The first open noticed the close; the second one, queued behind it,
    only looked afterwards and opened a live connection after close() had
    returned (audit A15-01)."""
    factory = SocketFactory()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def blocked_connect(url: str) -> FakeSocket:
        entered.set()
        await release.wait()
        return await factory(url)

    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        blocked_connect,
    )
    connection = client._connection
    first = asyncio.create_task(connection.open())
    second = asyncio.create_task(connection.open())
    # Inside the connector, not merely queued: yielding once was not enough
    # to get there, and the test proved less than it claimed.
    await entered.wait()

    await connection.close()
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    if connection.socket is not None:  # the charger answers what got opened
        connection.mark_authenticated()
        connection.mark_initialized()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert all(isinstance(o, WattpilotConnectionError) for o in outcomes)
    assert not connection.connected
    assert factory.open_sockets == [], "a socket outlived the close"


async def test_a_finished_attempt_is_not_handed_to_the_next_open(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared attempt is forgotten by a done-callback. An open() landing
    between the attempt's end and that callback was handed the old attempt
    -- and its old failure -- instead of trying again."""
    factory = SocketFactory()
    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        factory,
    )
    connection = client._connection
    connection.connect_timeout = 0.01

    async def failed_earlier() -> None:
        msg = "an attempt that already ended"
        raise WattpilotConnectionError(msg)

    stale = asyncio.create_task(failed_earlier())
    with contextlib.suppress(WattpilotConnectionError):
        await stale
    connection._opening = stale  # finished, not yet forgotten

    with pytest.raises(WattpilotConnectionError, match="authentication"):
        await connection.open()
    assert factory.sockets, "no new attempt was made"


async def test_an_open_during_the_cancel_step_of_a_close_stays_open(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close() cancelled the pending attempt and only then made itself
    visible. An open() in that gap started on its own, succeeded -- and was
    torn down by the close it had come after (audit A16-01)."""
    connection = client._connection
    entered = asyncio.Event()
    calls: list[str] = []

    async def connect(url: str) -> FakeSocket:
        calls.append(url)
        if len(calls) == 1:
            entered.set()
            await asyncio.Event().wait()  # hangs until close() cancels it
        # The charger answers at once, so a wrong order shows immediately.
        connection.mark_authenticated()
        connection.mark_initialized()
        return FakeSocket([])

    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        connect,
    )
    first = asyncio.create_task(connection.open())
    await entered.wait()
    later: list[asyncio.Future[None]] = []
    # Registered before close() waits on the attempt, so it runs in the gap.
    assert connection._opening is not None
    connection._opening.add_done_callback(
        lambda _: later.append(asyncio.ensure_future(connection.open()))
    )

    await connection.close()
    with contextlib.suppress(WattpilotConnectionError):
        await first
    await later[0]

    assert connection.connected, "an open that succeeded was torn down"
    await connection.close()


async def test_an_open_waits_out_every_close_in_progress(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """open() waited for the close it found, once. A second close starting
    as the first one ended was not checked for, so the open started while it
    ran -- and that close could take the new connection down again."""
    connection = client._connection
    connects: list[str] = []

    async def connect(url: str) -> FakeSocket:
        connects.append(url)
        connection.mark_authenticated()
        connection.mark_initialized()
        return FakeSocket([])

    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        connect,
    )
    loop = asyncio.get_running_loop()
    first_close: asyncio.Future[None] = loop.create_future()
    second_close: asyncio.Future[None] = loop.create_future()
    connection._closing = first_close  # type: ignore[assignment]
    first_close.add_done_callback(
        lambda _: setattr(connection, "_closing", second_close)
    )

    opening = asyncio.create_task(connection.open())
    await asyncio.sleep(0)
    first_close.set_result(None)
    for _ in range(5):
        await asyncio.sleep(0)
    assert connects == [], "open() started while a close was still running"

    second_close.set_result(None)
    await opening
    assert connection.connected
    await connection.close()


class SlowClosingSocket(FakeSocket):
    """A socket whose close handshake takes as long as the test says."""

    def __init__(self, release: asyncio.Event) -> None:
        super().__init__([])
        self._release = release

    async def close(self) -> None:
        await self._release.wait()
        await super().close()


async def test_an_open_during_a_slow_close_waits_for_a_new_connection(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While close() waited for the socket's close handshake, the connection
    still counted as connected -- so an open() in that window returned at
    once, reporting a connection the close then took down (audit A15-01)."""
    factory = SocketFactory()
    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        factory,
    )
    connection = client._connection
    release = asyncio.Event()
    connection.socket = SlowClosingSocket(release)  # type: ignore[assignment]
    connection.mark_authenticated()
    connection.mark_initialized()

    closing = asyncio.create_task(connection.close())
    await asyncio.sleep(0)
    opening = asyncio.create_task(connection.open())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not opening.done(), "open() reported the connection being closed"

    release.set()
    await closing
    await _settle(lambda: connection.socket is not None)
    connection.mark_authenticated()
    connection.mark_initialized()
    await opening

    assert connection.connected
    assert connection.socket is factory.sockets[-1], "not a fresh connection"
    await connection.close()


async def test_a_silent_socket_after_reconnect_is_given_up_on(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit path bounds the handshake; the automatic retry did not.
    A peer whose socket stays open but never finishes the application
    handshake held the integration unavailable for good (audit A12-04)."""
    factory = SocketFactory()
    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        factory,
    )
    connection = client._connection
    connection._reconnect_delay_min = 0
    connection.connect_timeout = 0.02
    connection.init_timeout = 0.02

    connection.socket = FakeSocket([])
    factory.sockets.append(connection.socket)
    connection.message_loop_task = asyncio.create_task(connection._message_loop())
    await connection.socket.close()

    # The retry opens a socket that says nothing at all. Its deadline has to
    # end it, which shows up as further attempts being made.
    for _ in range(200):
        if len(factory.sockets) > 2:
            break
        await asyncio.sleep(0.01)
    await connection.close()

    assert len(factory.sockets) > 2, (
        "the silent socket was never given up on: "
        f"{len(factory.sockets)} attempt(s) in total"
    )


class BufferedSocket(FakeSocket):
    """A socket that still hands over what its receive buffer already holds.

    Real WebSockets do exactly that: closing does not discard frames that
    already arrived. FakeSocket ends its iteration on close, and that
    difference hid a defect (audit A12-02).
    """

    async def __anext__(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        if self.closed:
            raise StopAsyncIteration
        await self._shut.wait()
        raise StopAsyncIteration


async def test_no_frame_is_applied_after_the_charger_was_refused(
    client: Wattpilot,
) -> None:
    """Refusing the identity closes the socket, but frames already in the
    receive buffer still arrive. They were applied: amp=14 from the wrong
    charger landed in the cache after the rejection (audit A12-02)."""
    client._device.serial = "111111"
    socket = BufferedSocket(
        [
            json.dumps({"type": "hello", "serial": "222222"}),
            json.dumps({"type": "deltaStatus", "status": {"amp": 14}}),
        ]
    )
    client._connection.socket = socket  # type: ignore[assignment]

    task = asyncio.create_task(client._connection._message_loop())
    try:
        await _settle(task.done)
        assert client.serial == "111111"
        assert client.all_properties.get("amp") is None, (
            "a buffered frame was applied after refusal"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_cancelled_definition_load_hands_the_connection_back(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect() is handshake plus definition load. A cancellation between
    the two fell outside the transport's cleanup scope and outside the
    integration's, leaving reader and socket alive (audit A12-03)."""
    socket = FakeSocket(
        [
            json.dumps({"type": "authSuccess"}),
            json.dumps({"type": "fullStatus", "partial": False, "status": {}}),
        ]
    )

    async def fake_connect(_url: str) -> FakeSocket:
        return socket

    entered = asyncio.Event()

    async def never_returns() -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect",
        fake_connect,
    )
    monkeypatch.setattr(client, "_load_api_definition", never_returns)

    task = asyncio.create_task(client.connect())
    # Waiting for the load to be entered, not merely for readiness: cancelling
    # a moment earlier lands inside open(), whose own cleanup already handles
    # it, and the test would pass without touching this gap at all.
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.closed, "the socket outlived the cancelled connect"
    assert client._connection.message_loop_task is None


async def test_the_firmware_wait_keeps_the_budget_it_was_given(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconnect loop counted only its own sleeps, so every attempt could
    burn a full authentication timeout for free -- with the default budget
    roughly half an hour past the two minutes asked for (audit A12-08).
    """
    client._all_props["onv"] = ["42.6"]

    async def slow_and_failing() -> None:
        await asyncio.sleep(0.2)
        msg = "still rebooting"
        raise WattpilotConnectionError(msg)

    monkeypatch.setattr(client, "set_property", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(client, "connect", slow_and_failing)
    # The timeout hands recovery back to the reconnect loop (A18-06); park it.
    client._connection._reconnect_delay_min = 3600

    started = time.monotonic()
    with pytest.raises(WattpilotConnectionError, match="Timeout reconnecting"):
        await client.install_firmware_update(timeout=0.05)
    took = time.monotonic() - started
    await client._connection.close()

    assert took < 1, f"a 0.05s budget took {took:.2f}s"


async def test_a_binary_frame_that_is_not_utf8_does_not_end_the_reader(
    client: Wattpilot,
) -> None:
    """The frame was decoded before the frame guard, so a byte sequence that
    is not UTF-8 ended the reader instead of being skipped (audit A19-08)."""
    client._connection.socket = FakeSocket(  # type: ignore[assignment]
        [bytes([0xFF, 0xFE]), json.dumps({"type": "deltaStatus", "status": {"amp": 6}})]  # type: ignore[list-item]
    )
    task = asyncio.create_task(client._connection._message_loop())
    try:
        await _settle(lambda: client.all_properties.get("amp") == 6)
        assert client.all_properties.get("amp") == 6
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_an_empty_array_does_not_end_the_reader(client: Wattpilot) -> None:
    """`nrg` arrives as a list the client indexes into. An empty one raised
    IndexError, which the frame guard did not catch -- so the reader stopped
    and the valid frame behind it was never read, while the socket still
    looked alive (audit 12.4, the remainder of A11-06)."""
    client._connection.socket = FakeSocket(  # type: ignore[assignment]
        [
            json.dumps({"type": "deltaStatus", "status": {"nrg": []}}),
            json.dumps({"type": "deltaStatus", "status": {"amp": 6}}),
        ]
    )
    task = asyncio.create_task(client._connection._message_loop())
    try:
        await _settle(lambda: client.all_properties.get("amp") == 6)
        assert client.all_properties.get("amp") == 6, (
            "the frame behind the malformed one was lost"
        )
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


CONNECT = "custom_components.wattpilot.api.connection.websockets.asyncio.client.connect"


class RefusingConnector:
    """A charger that is still away: every connect is refused, and counted."""

    def __init__(self) -> None:
        self.attempts = 0

    async def __call__(self, _url: str) -> FakeSocket:
        self.attempts += 1
        await asyncio.sleep(0)
        raise OSError(errno.ECONNREFUSED, "Connect call failed")


async def test_a_failed_reconnect_action_during_backoff_keeps_recovering(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconnect action stopped the loop that was waiting out its backoff
    before its own attempt had a socket. When that attempt failed, nothing
    was left to retry: the charger came back to an integration that never
    reached it again (audit A18-05)."""
    connector = RefusingConnector()
    monkeypatch.setattr(CONNECT, connector)
    connection = client._connection
    connection._reconnect_delay_min = 0.01
    connection.socket = FakeSocket([])  # type: ignore[assignment]
    connection.message_loop_task = asyncio.create_task(connection._message_loop())
    await connection.socket.close()
    await _settle(lambda: not connection.connected)

    with pytest.raises(ConnectionRefusedError):
        await connection.open()

    try:
        assert connection.message_loop_task is not None, "nobody is left to retry"
        tried = connector.attempts
        await asyncio.sleep(0.1)
        assert connector.attempts > tried, "the retries stopped"
    finally:
        await connection.close()


async def test_a_firmware_update_outlasting_its_budget_keeps_recovering(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update closes the connection to reconnect by itself. When the
    charger took longer than the budget, the action failed and left the
    client closed for good -- the updated charger was never reached again
    (audit A18-06)."""
    monkeypatch.setattr(CONNECT, RefusingConnector())
    client._all_props["onv"] = ["42.6"]
    monkeypatch.setattr(client, "set_property", lambda *_a, **_k: asyncio.sleep(0))
    client._connection._reconnect_delay_min = 3600  # park it; it only has to exist

    with pytest.raises(WattpilotConnectionError, match="Timeout reconnecting"):
        await client.install_firmware_update(timeout=0.05)

    try:
        assert client._connection.message_loop_task is not None
    finally:
        await client._connection.close()


async def test_a_refusal_ends_the_firmware_wait_with_its_reason(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying cannot help against a refusal. The wait went on until its
    deadline and reported a timeout, never the reason (reaudit R1-13)."""

    async def refused() -> None:
        client._connection.fail(WattpilotConnectionError("Unsupported password hash"))
        raise client._connection.fatal_error  # type: ignore[misc]

    client._all_props["onv"] = ["42.6"]
    monkeypatch.setattr(client, "set_property", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(client, "connect", refused)

    async with asyncio.timeout(1):
        with pytest.raises(WattpilotConnectionError, match="Unsupported password"):
            await client.install_firmware_update(timeout=30)


async def test_a_firmware_wait_with_no_time_for_an_attempt_keeps_recovering(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The charger dropped the socket only as the budget ran out, so no
    attempt ran. The update's own disconnect still counted as an unload, and
    the hand-back at the deadline was refused: nothing retried (post-check
    PC-01)."""
    monkeypatch.setattr(CONNECT, RefusingConnector())
    client._all_props["onv"] = ["42.6"]
    monkeypatch.setattr(client, "set_property", lambda *_a, **_k: asyncio.sleep(0))
    client._connection._reconnect_delay_min = 3600  # park it; it only has to exist

    async def dropped_late() -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(client._connection, "wait_disconnected", dropped_late)
    with pytest.raises(WattpilotConnectionError, match="Timeout reconnecting"):
        await client.install_firmware_update(timeout=0.05)

    try:
        assert client._connection.message_loop_task is not None
    finally:
        await client._connection.close()


async def test_a_close_before_the_attempt_runs_is_not_undone(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attempt cleared the stop flag as its first line, so a close that
    landed between open() starting the attempt and the attempt running was
    undone, and a later hand-back reconnected after an unload (PC-01)."""
    release = asyncio.Event()

    async def blocked_connect(_url: str) -> FakeSocket:
        await release.wait()
        return FakeSocket([])

    monkeypatch.setattr(CONNECT, blocked_connect)
    connection = client._connection
    opening = asyncio.create_task(connection.open())
    # One pass: open() starts its attempt, which has not run yet when the
    # close below begins (asyncio runs ready tasks in order).
    await asyncio.sleep(0)
    await connection.close()
    with contextlib.suppress(WattpilotConnectionError, asyncio.CancelledError):
        await opening

    connection.resume_reconnecting()
    try:
        assert connection.message_loop_task is None, "reconnecting after a close"
    finally:
        release.set()
        await connection.close()


async def _a_loop_in_backoff(connection: Any) -> None:
    connection.socket = FakeSocket([])
    connection.message_loop_task = asyncio.create_task(connection._message_loop())
    await connection.socket.close()
    await _settle(lambda: not connection.connected)


async def test_a_close_during_the_firmware_wait_is_not_overridden(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update reconnects by itself and, at its deadline, handed recovery
    back to a reconnect loop -- even when the entry had been unloaded in the
    meantime, so a session outlived its integration (reaudit R1-11)."""
    connector = RefusingConnector()
    monkeypatch.setattr(CONNECT, connector)
    client._all_props["onv"] = ["42.6"]
    monkeypatch.setattr(client, "set_property", lambda *_a, **_k: asyncio.sleep(0))
    client._connection._reconnect_delay_min = 0.01

    install = asyncio.create_task(client.install_firmware_update(timeout=0.3))
    await asyncio.sleep(0.05)
    await client.disconnect()
    with pytest.raises(WattpilotConnectionError):
        await install
    tried = connector.attempts
    await asyncio.sleep(0.1)

    assert client._connection.message_loop_task is None
    assert connector.attempts == tried, "it kept connecting after the close"


async def test_no_second_reader_while_an_open_is_pending(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open in progress starts its own reader; a resume beside it made a
    second one on a second socket, and the first outlived close()."""
    release = asyncio.Event()

    async def blocked_connect(_url: str) -> FakeSocket:
        await release.wait()
        return FakeSocket([])

    monkeypatch.setattr(CONNECT, blocked_connect)
    connection = client._connection
    connection.connect_timeout = 0.01
    opening = asyncio.create_task(connection.open())
    await asyncio.sleep(0)

    connection.resume_reconnecting()

    assert connection.message_loop_task is None, "the pending open owns the reader"
    release.set()
    with contextlib.suppress(WattpilotConnectionError):
        await opening
    await connection.close()


async def test_a_cancelled_reconnect_action_keeps_recovering(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling of A18-05: an action cancelled while connecting (an automation
    restarted) stopped the loop that was retrying (reaudit R1-12)."""

    connecting = asyncio.Event()

    async def hanging_connect(_url: str) -> FakeSocket:
        connecting.set()
        await asyncio.sleep(3600)
        raise AssertionError

    monkeypatch.setattr(CONNECT, hanging_connect)
    connection = client._connection
    connection._reconnect_delay_min = 3600
    await _a_loop_in_backoff(connection)

    caller = asyncio.create_task(connection.open())
    await connecting.wait()  # the old loop is gone, the new socket not there
    caller.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await caller

    try:
        assert connection.message_loop_task is not None, "nobody is left to retry"
    finally:
        await connection.close()


async def test_an_open_into_a_stalled_handshake_keeps_recovering(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconnect had authenticated but the full status stalled; an open
    then timed out and closed the only loop (reaudit R1-12)."""
    monkeypatch.setattr(CONNECT, RefusingConnector())
    connection = client._connection
    connection._reconnect_delay_min = 3600
    connection.init_timeout = 0.01
    connection.socket = FakeSocket([])  # type: ignore[assignment]
    connection.message_loop_task = asyncio.create_task(connection._message_loop())
    connection.mark_authenticated()

    with pytest.raises(WattpilotConnectionError, match="property initialization"):
        await connection.open()

    try:
        assert connection.message_loop_task is not None, "nobody is left to retry"
    finally:
        await connection.close()


# ---- One rule over every way in and every settled state (ledger L-021) ----
#
# The single interleaving tests above each came from a finding, and two of the
# fixes made the next finding (R1-11, PC-01). This matrix states the rule once:
# a deliberate close leaves nothing running; any other way in leaves recovery
# as it found it -- a reconnect loop that existed still exists, none is made
# up -- and never two readers or a socket without a reader. An open after a
# refusal is a fresh start, not recovery, so it is not part of the rule. The
# in-flight states (an open or a close still running) stay with the single
# tests above; their races need a hand-placed interleaving each.

LIFECYCLE_STATES = ["idle", "backoff", "reading", "stalled", "closed", "refused"]
LIFECYCLE_ENTRIES = [
    "failed_open",
    "cancelled_open",
    "timed_out_open",
    "refused_open",
    "close",
    "resume",
]
_LOOP = "Connection._message_loop"


class MatrixConnector:
    """Every socket this connection ever got, and how the next connect ends."""

    def __init__(self, connection: Any) -> None:
        self.sockets: list[FakeSocket] = []
        self.mode = "refuse"
        self.entered = asyncio.Event()
        self._connection = connection

    def track(self, socket: FakeSocket) -> FakeSocket:
        self.sockets.append(socket)
        return socket

    async def __call__(self, _url: str) -> FakeSocket:
        self.entered.set()
        await asyncio.sleep(0)
        if self.mode == "hang":
            await asyncio.sleep(3600)
        if self.mode == "refuse":
            raise OSError(errno.ECONNREFUSED, "Connect call failed")
        if self.mode == "refuse_auth":
            refusal = WattpilotConnectionError("refused")
            asyncio.get_running_loop().call_soon(self._connection.fail, refusal)
        return self.track(FakeSocket([]))


async def _enter_state(connection: Any, connector: MatrixConnector, state: str) -> None:
    if state == "idle":
        return
    connection.socket = connector.track(FakeSocket([]))
    connection.message_loop_task = asyncio.create_task(connection._message_loop())
    await asyncio.sleep(0)
    if state in ("reading", "stalled"):
        connection.mark_authenticated()
        if state == "reading":
            connection.mark_initialized()
        return
    if state == "refused":
        connection.fail(WattpilotConnectionError("refused"))
    await connection.socket.close()
    await _settle(lambda: not connection.connected)
    if state == "refused":
        await _settle(connection.message_loop_task.done)
    if state == "closed":
        await connection.close()


async def _run_entry(connection: Any, connector: MatrixConnector, entry: str) -> None:
    if entry == "close":
        await connection.close()
    elif entry == "resume":
        connection.resume_reconnecting()
    elif entry == "cancelled_open":
        connector.mode = "hang"
        caller = asyncio.create_task(connection.open())
        await _settle(lambda: connector.entered.is_set() or caller.done())
        caller.cancel()
        with contextlib.suppress(asyncio.CancelledError, WattpilotConnectionError):
            await caller
    else:
        modes = {"failed_open": "refuse", "refused_open": "refuse_auth"}
        connector.mode = modes.get(entry, "silent")
        with contextlib.suppress(OSError, WattpilotConnectionError):
            await connection.open()


def _expected_loops(state: str, entry: str) -> int:
    signed_in = state in ("reading", "stalled")  # an open connects nothing there
    if entry == "close" or (entry == "refused_open" and not signed_in):
        return 0
    if entry == "resume" and state in ("closed", "refused"):
        return 0
    if entry == "resume":
        return 1
    return 1 if signed_in or state == "backoff" else 0


def _running_loops() -> list[asyncio.Task[Any]]:
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_coro().__qualname__ == _LOOP
    ]


@pytest.mark.parametrize("entry", LIFECYCLE_ENTRIES)
@pytest.mark.parametrize("state", LIFECYCLE_STATES)
async def test_every_way_in_leaves_recovery_as_it_found_it(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch, state: str, entry: str
) -> None:
    if state == "refused" and entry.endswith("_open"):
        pytest.skip("an open after a refusal starts afresh")
    connection = client._connection
    connector = MatrixConnector(connection)
    monkeypatch.setattr(CONNECT, connector)
    connection._reconnect_delay_min = 3600  # a loop parks in its backoff
    connection.connect_timeout = connection.init_timeout = 0.01
    await _enter_state(connection, connector, state)

    await _run_entry(connection, connector, entry)
    for _ in range(5):
        await asyncio.sleep(0)

    try:
        loops = _running_loops()
        assert len(loops) == _expected_loops(state, entry), "reconnect owners"
        if loops:
            assert connection.message_loop_task is loops[0]
        open_sockets = [socket for socket in connector.sockets if not socket.closed]
        assert len(open_sockets) <= len(loops), "a socket without a reader"
    finally:
        await connection.close()
    assert _running_loops() == [], "a loop outlived the close"


async def test_a_password_refused_after_sign_in_drops_the_connection(
    client: Wattpilot,
) -> None:
    """An authError after authSuccess set the refusal but left the socket open
    and the client signed in until the next frame: the entities stayed
    available while the reauthentication started (post-check PC2-02)."""
    socket = FakeSocket(
        [
            json.dumps({"type": "authSuccess"}),
            json.dumps({"type": "authError", "message": "wrong password"}),
        ]
    )
    connection = client._connection
    connection.socket = socket  # type: ignore[assignment]
    connection.message_loop_task = asyncio.create_task(connection._message_loop())
    await _settle(lambda: connection.fatal_error is not None)
    await _settle(lambda: socket.closed)

    try:
        assert socket.closed, "the refused socket stayed open"
        assert not client.connected
    finally:
        await connection.close()


async def test_a_refused_connection_stops_counting_as_connected_at_once(
    client: Wattpilot,
) -> None:
    """The refusal closed the socket, but the close waits for the charger's
    reply -- up to ten seconds -- and the client counted as signed in all
    that time (post-check PC3-01)."""
    release = asyncio.Event()
    socket = SlowClosingSocket(release)
    connection = client._connection
    connection.socket = socket  # type: ignore[assignment]
    connection.mark_authenticated()

    closing = asyncio.create_task(connection.reject(WattpilotConnectionError("x")))
    await asyncio.sleep(0)

    try:
        assert not connection.connected, "still signed in while the close waits"
    finally:
        release.set()
        await closing


async def test_a_refusal_during_the_full_status_is_reported_as_itself(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal after authSuccess did not end the wait for the full status:
    setup reported a timeout and retried, and never asked for the password
    (post-check PC3-02)."""
    from custom_components.wattpilot.api.exceptions import (  # noqa: PLC0415
        AuthenticationError,
    )

    async def signs_in_then_refuses(_url: str) -> FakeSocket:
        loop = asyncio.get_running_loop()
        loop.call_soon(client._connection.mark_authenticated)
        refusal = AuthenticationError("wrong password")
        loop.call_later(0.01, client._connection.fail, refusal)
        return FakeSocket([])

    monkeypatch.setattr(CONNECT, signs_in_then_refuses)
    connection = client._connection
    connection.init_timeout = 30

    async with asyncio.timeout(1):
        with pytest.raises(AuthenticationError):
            await connection.open()

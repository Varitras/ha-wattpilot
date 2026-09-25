"""
The socket, the reader task and the state that belongs to one connection.

Split out of client.py, which had grown past three raised size budgets in a
single audit round. The point of the split is state ownership, not line
count: everything that is true only until the next reconnect lives here and
nowhere else -- the socket, the reader task, the two readiness events, and
the error that makes a connection unusable. The client keeps the protocol
and the property cache, which outlive any single connection.

The connection knows nothing about messages. It hands each frame to the
callback it was built with and is told, by the same client, when the
handshake succeeded, when the first full snapshot arrived, and when
something made the connection unusable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

import websockets
import websockets.asyncio.client

from .exceptions import WattpilotConnectionError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from .exceptions import WattpilotError

_LOGGER = logging.getLogger(__name__)


class Connection:
    """One WebSocket connection to a charger, and everything it outlives."""

    def __init__(  # noqa: PLR0913 -- every connection knob the client offers
        self,
        url: str,
        handle_frame: Callable[[str], Awaitable[None]],
        on_reset: Callable[[str], None],
        *,
        connect_timeout: float = 30.0,
        init_timeout: float = 30.0,
        auto_reconnect: bool = True,
        reconnect_delay_min: float = 5.0,
        reconnect_delay_max: float = 300.0,
    ) -> None:
        """Configure a connection; nothing is opened until open() is awaited."""
        self._url = url
        self._handle_frame = handle_frame
        self._on_reset = on_reset
        self.connect_timeout = connect_timeout
        self.init_timeout = init_timeout
        self._auto_reconnect = auto_reconnect
        self._reconnect_delay_min = reconnect_delay_min
        self._reconnect_delay_max = reconnect_delay_max

        self.socket: websockets.asyncio.client.ClientConnection | None = None
        self.message_loop_task: asyncio.Task[None] | None = None
        self._readiness_guard: asyncio.Task[None] | None = None
        # wattpilot: one field for every reason this connection cannot be
        # used -- rejected credentials or the wrong charger answering. A
        # second flag beside it would be a second place to forget.
        self.fatal_error: WattpilotError | None = None

        self._authenticated = False
        self._initialized = False
        self._streaming_full_status = False
        self._authenticated_event = asyncio.Event()
        self._initialized_event = asyncio.Event()
        self._disconnected_event = asyncio.Event()
        self._disconnected_event.set()
        # Two open() calls arriving together each passed the checks below
        # and each opened a socket; the second overwrote the references to
        # the first, and close() never reached it again (audit A13-01).
        self._open_lock = asyncio.Lock()
        # A close() during the connector's await cannot reach the socket yet;
        # this is how open() notices it instead (audit A14-01).
        self._close_count = 0

    # ---- What the outside asks ----

    @property
    def connected(self) -> bool:
        """Return whether the handshake completed and the socket still lives."""
        return self._authenticated

    @property
    def initialized(self) -> bool:
        """Return whether the charger has sent its full state once."""
        return self._initialized

    # ---- What the message handlers report ----

    def mark_authenticated(self) -> None:
        """Record that the charger accepted the credentials."""
        self._set_authenticated(value=True)

    def _set_authenticated(self, *, value: bool) -> None:
        """
        Move the authenticated state, and the two events that mirror it.

        One place, because there are three that used to flip the flag and a
        waiter for the opposite edge now depends on them agreeing.
        """
        self._authenticated = value
        if value:
            self._disconnected_event.clear()
            self._authenticated_event.set()
        else:
            self._authenticated_event.clear()
            self._disconnected_event.set()

    async def wait_disconnected(self) -> None:
        """Wait until the charger is no longer authenticated."""
        await self._disconnected_event.wait()

    def receive_full_status(self, *, partial: bool) -> None:
        """Note one chunk of the full status; the last one completes it."""
        self._streaming_full_status = partial
        if not partial:
            self.mark_initialized()

    def receive_delta_status(self) -> None:
        """
        Note a delta; it completes only firmware that sends no stream.

        A delta can land between the chunks of the first full status (after
        a restart the stream paused ~1 s; deltas come every ~1 s). Counting
        it returned open() mid-stream, and later properties got no entity.
        """
        if not self._streaming_full_status:
            self.mark_initialized()

    def mark_initialized(self) -> None:
        """Record that the complete property snapshot has arrived."""
        self._initialized = True
        self._initialized_event.set()

    def fail(self, error: WattpilotError) -> None:
        """
        Record what makes this connection unusable and unblock open().

        The event is set rather than left alone so a caller waiting for the
        handshake stops waiting; open() checks fatal_error before it returns.
        """
        self.fatal_error = error
        self._authenticated_event.set()

    async def reject(self, error: WattpilotError) -> None:
        """Refuse the connection outright: record the reason and drop the socket."""
        self.fail(error)
        if self.socket is not None:
            await self.socket.close()

    # ---- Lifecycle ----

    async def open(self) -> None:
        """Open the socket, start the reader, and wait for a usable state."""
        async with self._open_lock:
            await self._open()

    async def _open(self) -> None:
        if self.connected:
            if not self._initialized:
                async with self._cleanup_on_failure():
                    await self._wait_for(
                        "property initialization",
                        self._initialized_event,
                        self.init_timeout,
                    )
            return

        # A reader still sitting out its retry backoff owns a socket; replacing
        # it without stopping it left two readers racing (audit A12-01).
        if self.message_loop_task is not None:
            await self.close()

        self.begin()
        self.socket = await self._connect_unless_closed()
        self.message_loop_task = asyncio.create_task(self._message_loop())

        async with self._cleanup_on_failure():
            await self._wait_for(
                "authentication", self._authenticated_event, self.connect_timeout
            )
            if self.fatal_error is not None:
                raise self.fatal_error
            await self._wait_for(
                "property initialization",
                self._initialized_event,
                self.init_timeout,
            )

    async def _connect_unless_closed(
        self,
    ) -> websockets.asyncio.client.ClientConnection:
        # Not the open lock: close() would then wait out the connect timeout.
        closes_before = self._close_count
        socket = await websockets.asyncio.client.connect(self._url)
        if self._close_count != closes_before:
            await socket.close()
            msg = "Connection closed while it was being opened"
            raise WattpilotConnectionError(msg)
        return socket

    def begin(self) -> None:
        """
        Reset everything that belongs to a single connection.

        Every path that opens a socket comes through here. The explicit path
        used to clear only the event and carry the initialized flag over from
        the previous connection, so the first partial replay already
        satisfied readiness and open() returned before the new snapshot had
        arrived (audit A11-07).

        What the charger replays is deliberately not reset -- that is the
        client's property cache, and clearing it would blank every entity for
        the length of the outage instead of holding the last known values.
        """
        self._cancel_readiness_guard()
        self._initialized = False
        self._streaming_full_status = False
        self._initialized_event.clear()
        self._authenticated_event.clear()
        self.fatal_error = None
        # An automatic reconnect never passes through close(), so this is the
        # only place that answers commands sent on the socket that just went
        # away.
        self._on_reset("Connection closed")

    async def close(self) -> None:
        """Stop the reader and close the socket, however either of them ended."""
        self._close_count += 1
        self._cancel_readiness_guard()
        task = self.message_loop_task
        self.message_loop_task = None
        try:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # wattpilot: awaiting a loop that already failed used to
                    # re-raise here, before the socket was closed (A11-06).
                    _LOGGER.debug("Message loop ended with an error", exc_info=True)
        finally:
            # Reset even when close() raises, or `connected` stays True with
            # the reader already cancelled (audit VA-04).
            try:
                if self.socket is not None:
                    await self.socket.close()
            finally:
                self.socket = None
                self._set_authenticated(value=False)
                self._initialized_event.clear()
                self._on_reset("Connection closed")

    async def send(self, payload: str) -> None:
        """Write one frame, or say plainly that there is nowhere to write it."""
        if self.socket is None:
            msg = "Not connected"
            raise WattpilotConnectionError(msg)
        await self.socket.send(payload)

    async def _wait_for(self, what: str, event: asyncio.Event, seconds: float) -> None:
        """Wait for a connection milestone, naming it if it never arrives."""
        try:
            await asyncio.wait_for(event.wait(), seconds)
        except TimeoutError as exc:
            msg = f"Timeout waiting for {what}"
            raise WattpilotConnectionError(msg) from exc

    def _cancel_readiness_guard(self) -> None:
        """Stop watching a connection that is being replaced or torn down."""
        if self._readiness_guard is not None:
            self._readiness_guard.cancel()
            self._readiness_guard = None

    async def _guard_readiness(self) -> None:
        """
        Give up on a reconnected socket that never finishes the handshake.

        open() can wait for readiness inline because a separate task does the
        reading. After an automatic reconnect the reader is this very loop, so
        the same wait would deadlock -- nothing would read the frames it waits
        for. Watching from the side instead: a peer whose socket stays up
        while its application handshake stalls used to hold the integration
        unavailable for good, with no further retry (audit A12-04). Closing
        the socket ends the frame iteration, and the loop tries again.
        """
        try:
            await asyncio.wait_for(
                self._initialized_event.wait(),
                self.connect_timeout + self.init_timeout,
            )
        except TimeoutError:
            _LOGGER.warning("Reconnected socket never became ready; closing it")
            if self.socket is not None:
                await self.socket.close()

    @contextlib.asynccontextmanager
    async def _cleanup_on_failure(self) -> AsyncIterator[None]:
        """
        Hand back socket and message loop however the block ends badly.

        BaseException, not Exception: Home Assistant cancels a setup that
        takes too long, and CancelledError is not an Exception -- the
        narrower catch left both running behind it (audit A11-05).
        """
        try:
            yield
        except BaseException:
            await self.close()
            raise

    # ---- Reader ----

    async def _read_frames(self) -> None:
        """Dispatch frames until the socket ends the iteration."""
        if self.socket is None:
            return
        async for frame in self.socket:
            if self.fatal_error is not None:
                # Frames buffered before a refusal were still applied: the
                # wrong device's amp reached the cache (audit A12-02).
                _LOGGER.debug("Dropping a frame that arrived after the refusal")
                break
            raw = frame.decode("utf-8") if isinstance(frame, bytes) else frame
            try:
                await self._handle_frame(raw)
            except ValueError, TypeError, AttributeError, LookupError:
                # One unreadable frame used to end the loop with the socket
                # open (A11-06). LookupError: an empty `nrg` is indexed into.
                # Errors of the connection itself are deliberately not caught.
                _LOGGER.warning("Ignoring an unreadable frame (%d bytes)", len(raw))

    async def _message_loop(self) -> None:
        if self.socket is None:
            msg = "Message loop started without a socket"
            raise WattpilotConnectionError(msg)
        reconnect_delay = self._reconnect_delay_min
        try:
            while True:
                try:
                    await self._read_frames()
                except websockets.exceptions.ConnectionClosed:
                    _LOGGER.debug("WebSocket connection closed")

                self._set_authenticated(value=False)

                if not self._auto_reconnect:
                    break

                # Retrying cannot help against rejected credentials or a
                # charger that is not ours; both would loop forever.
                if self.fatal_error is not None:
                    _LOGGER.error("Not reconnecting: %s", self.fatal_error)
                    break

                _LOGGER.debug("Reconnecting in %.0fs...", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                try:
                    self.begin()
                    self.socket = await websockets.asyncio.client.connect(self._url)
                    self._readiness_guard = asyncio.create_task(self._guard_readiness())
                    reconnect_delay = self._reconnect_delay_min
                except (OSError, websockets.exceptions.WebSocketException) as exc:
                    reconnect_delay = min(
                        reconnect_delay * 2, self._reconnect_delay_max
                    )
                    # Once per cycle for as long as the charger is away:
                    # at WARNING this fills the log until it comes back.
                    _LOGGER.debug(
                        "Reconnect failed: %s, retrying in %.0fs", exc, reconnect_delay
                    )
        finally:
            self._set_authenticated(value=False)

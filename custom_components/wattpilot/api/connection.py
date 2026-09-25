"""
The socket, the reader task and the state that belongs to one connection.

Everything true only until the next reconnect lives here: socket, reader,
readiness events, and the error that makes a connection unusable. The client
keeps the protocol and the property cache. This module knows nothing about
messages -- it hands each frame to its callback and is told by the client
when the handshake succeeded, the snapshot arrived, or the connection failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import websockets
import websockets.asyncio.client

from .exceptions import WattpilotConnectionError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine

    from .exceptions import WattpilotError

_LOGGER = logging.getLogger(__name__)


def _cancelled_by_caller(attempt: asyncio.Task[None]) -> bool:
    """Whether the caller itself was cancelled, not the attempt by close()."""
    current = asyncio.current_task()
    return not attempt.cancelled() or bool(current and current.cancelling())


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
        # One transition at a time (audits A13-01, A14-01, A15-01): opens
        # share one attempt, close() cancels it rather than waiting it out,
        # and an open() during a close() waits for a fresh connection.
        self._opening: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None

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
        # Every close in progress, not just the first: one can start as another ends.
        while self._closing is not None and not self._closing.done():
            await asyncio.wait({self._closing})
        attempt = self._opening = self._ongoing(self._opening, self._open)
        try:
            # Not shielded: a caller Home Assistant gives up on takes the
            # attempt down with it, socket and reader included (A11-05).
            await attempt
        except asyncio.CancelledError:
            if _cancelled_by_caller(attempt):
                raise
            msg = "Connection closed while it was being opened"
            raise WattpilotConnectionError(msg) from None

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
            await self._close()

        self.begin()
        self.socket = await websockets.asyncio.client.connect(self._url)
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

    def begin(self) -> None:
        """
        Reset everything that belongs to a single connection.

        Every path that opens a socket comes through here; carrying the
        initialized flag over let open() return before the new snapshot
        (audit A11-07). The property cache is deliberately kept: clearing it
        would blank every entity for the length of the outage.
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
        # Visible before anything is awaited: while it cancelled the pending
        # attempt unannounced, a new open() started and was torn down (A16-01).
        self._closing = self._ongoing(self._closing, self._cancel_and_close)
        await asyncio.shield(self._closing)

    async def _cancel_and_close(self) -> None:
        if self._opening is not None:
            self._opening.cancel()
            await asyncio.wait({self._opening})
        await self._close()

    def _ongoing(
        self,
        task: asyncio.Task[None] | None,
        start: Callable[[], Coroutine[Any, Any, None]],
    ) -> asyncio.Task[None]:
        # Done is not ongoing: _forget runs as a callback, a step later, and
        # a caller in that gap would inherit a finished transition's result.
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(start())
        task.add_done_callback(self._forget)
        return task

    def _forget(self, task: asyncio.Task[None]) -> None:
        if self._opening is task:
            self._opening = None
        if self._closing is task:
            self._closing = None

    async def _close(self) -> None:
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
            await self._close()
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

"""The API client's own behaviour.

The client started as wattpilot-api 1.4.0 (MIT) and is ours now: upstream has
been quiet since May, so it is maintained here under this project's rules
rather than kept diffable against a version that no longer moves. None of the
original's tests came along, so its behaviour is pinned here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.wattpilot.api import definition as definition_module
from custom_components.wattpilot.api.client import Wattpilot, _message_type
from custom_components.wattpilot.api.exceptions import CommandError


class FakeSocket:
    """Records what the client sends; never answers on its own."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.close_error: Exception | None = None

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


def make_client(socket: FakeSocket) -> Wattpilot:
    client = Wattpilot("192.0.2.10", "secret")
    client._ws = socket  # type: ignore[assignment]
    client._connected = True
    client._device.secured = 0
    return client


def respond(client: Wattpilot, request_id: int, *, success: bool) -> None:
    """Feed the client the device's answer to one command."""
    from types import SimpleNamespace  # noqa: PLC0415 -- test-local shape

    client._on_response(
        SimpleNamespace(
            requestId=request_id,
            success=success,
            status=SimpleNamespace(),
            message="rejected by charger",
        )
    )


async def test_a_rejected_write_raises_instead_of_reporting_success() -> None:
    """Audit VA-03: set_property returned as soon as the frame was on the
    wire. A later `success: false` was only logged, so a switch the charger
    refused still showed as flipped."""
    socket = FakeSocket()
    client = make_client(socket)

    task = asyncio.ensure_future(client.set_property("amp", 10))
    await asyncio.sleep(0)
    request_id = socket.sent[0]["requestId"]
    respond(client, request_id, success=False)

    with pytest.raises(CommandError, match="rejected by charger"):
        await task


async def test_an_acknowledged_write_returns() -> None:
    socket = FakeSocket()
    client = make_client(socket)

    task = asyncio.ensure_future(client.set_property("amp", 10))
    await asyncio.sleep(0)
    respond(client, socket.sent[0]["requestId"], success=True)
    await task


async def test_a_silent_charger_does_not_hang_the_caller() -> None:
    """No answer must end as an error, not as a coroutine waiting forever."""
    socket = FakeSocket()
    client = make_client(socket)
    client.command_timeout = 0.01

    with pytest.raises(CommandError, match="did not answer"):
        await client.set_property("amp", 10)


async def test_disconnect_releases_a_waiting_command() -> None:
    socket = FakeSocket()
    client = make_client(socket)

    task = asyncio.ensure_future(client.set_property("amp", 10))
    await asyncio.sleep(0)
    await client.disconnect()

    with pytest.raises(CommandError):
        await task


async def test_a_failing_close_still_marks_the_client_disconnected() -> None:
    """Audit VA-04: `connected` stayed True when ws.close() raised, while the
    message loop behind it was already cancelled."""
    socket = FakeSocket()
    socket.close_error = ConnectionResetError("close failed")
    client = make_client(socket)

    with pytest.raises(ConnectionResetError):
        await client.disconnect()
    assert client.connected is False


async def test_no_frame_payload_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audit VA-01, fixed at the source now that the client is ours: frames
    carry Wi-Fi passwords, cloud tokens and OCPP keys, and DEBUG logging is
    one UI toggle away."""
    socket = FakeSocket()
    client = make_client(socket)
    caplog.set_level(logging.DEBUG)

    await client._handle_message(
        json.dumps({"type": "deltaStatus", "status": {"wak": "SECRET_WIFI"}})
    )
    client.command_timeout = 0.01
    with pytest.raises(CommandError):
        await client.set_property("amp", 10)

    assert "SECRET_WIFI" not in caplog.text
    assert "deltaStatus" in caplog.text, "the type still has to be visible"


def test_an_unparsable_frame_is_named_as_such() -> None:
    """The log line says what arrived. A frame that is not JSON has no type,
    and saying so beats logging nothing -- found by a surviving mutant that
    changed the placeholder without any test noticing."""
    assert _message_type('{"type": "deltaStatus"}') == "deltaStatus"
    assert _message_type("not json at all") == "<unparsable>"
    assert _message_type("[1, 2, 3]") == "<not an object>"
    assert _message_type('{"no": "type"}') == "<untyped>"


async def test_disconnect_stops_a_running_message_loop() -> None:
    """The teardown cancels the loop task and swallows the CancelledError
    that cancellation raises. Nothing covered that before: a mutant that
    suppressed the wrong exception class survived."""
    socket = FakeSocket()
    client = make_client(socket)
    started = asyncio.Event()

    async def never_ends() -> None:
        started.set()
        await asyncio.Event().wait()

    client._message_loop_task = asyncio.ensure_future(never_ends())
    await started.wait()

    await client.disconnect()
    assert client._message_loop_task is None
    assert socket.closed


async def test_an_answer_that_arrives_twice_is_ignored() -> None:
    """A charger repeating itself must not raise "already resolved" out of
    the message loop -- the guard against it had no test, so inverting it
    survived."""
    socket = FakeSocket()
    client = make_client(socket)

    task = asyncio.ensure_future(client.set_property("amp", 10))
    await asyncio.sleep(0)
    request_id = socket.sent[0]["requestId"]
    respond(client, request_id, success=True)
    await task

    client._pending_commands[request_id] = asyncio.get_running_loop().create_future()
    client._pending_commands[request_id].set_result(None)
    client._fail_pending_commands("Connection closed")
    assert client._pending_commands == {}


async def test_an_accepted_command_applies_the_properties_it_returns() -> None:
    """The answer carries the charger's new state; dropping it would leave
    the entity showing the old value until the next push."""
    from types import SimpleNamespace  # noqa: PLC0415 -- test-local shape

    socket = FakeSocket()
    client = make_client(socket)
    task = asyncio.ensure_future(client.set_property("amp", 10))
    await asyncio.sleep(0)

    client._on_response(
        SimpleNamespace(
            requestId=socket.sent[0]["requestId"],
            success=True,
            status=SimpleNamespace(amp=10),
        )
    )
    await task
    assert client.all_properties["amp"] == 10


async def test_connect_loads_the_api_definition_off_the_event_loop() -> None:
    """The definition is an 88 kB YAML file, and it used to be read on the
    first write -- blocking the event loop at a moment nobody chose. Loading
    it while connecting, in a thread, removes both the blocking read and the
    warm-up the integration had to do on the client's behalf.
    """
    client = Wattpilot("192.0.2.10", "secret")
    assert client._api_def_cache is None

    # The thread identity, not just "the cache filled": inlining the read
    # would fill it too, and would be exactly the regression. This proof
    # moved here from the hub, whose warm-up this replaces.
    loading_thread: list[int] = []
    real_loader = definition_module.load_api_definition

    def record_thread(**kwargs: object) -> object:
        loading_thread.append(threading.get_ident())
        return real_loader(**kwargs)  # type: ignore[arg-type]

    with patch(
        "custom_components.wattpilot.api.client.load_api_definition", record_thread
    ):
        await client._load_api_definition()

    assert loading_thread
    assert loading_thread[0] != threading.get_ident()
    assert client._api_def_cache is not None
    assert client._api_def_cache.properties

    # And the lazy path must not read the file again once it is there.
    with patch(
        "custom_components.wattpilot.api.client.load_api_definition",
        side_effect=AssertionError("read the file twice"),
    ):
        assert client._get_api_def() is client._api_def_cache

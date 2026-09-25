"""Frame sequences recorded from a real charger, replayed through the client.

The other client tests feed frames written by hand, which encode what we
believe the charger sends. These feed what it did send (firmware 42.5,
recorded 2026-09-25, sanitized), in the order it sent it -- the order is the
evidence, the timestamps only document the gaps. A new firmware gets new
recordings next to these, not edits to them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from custom_components.wattpilot.api.client import Wattpilot

if TYPE_CHECKING:
    import pytest

RECORDINGS = Path(__file__).parent / "fixtures" / "replay"


def load(name: str) -> dict[str, Any]:
    return json.loads((RECORDINGS / name).read_text(encoding="utf-8"))


def frames(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry["frame"] for entry in entries]


def is_last_chunk(frame: dict[str, Any]) -> bool:
    return frame.get("type") == "fullStatus" and frame.get("partial") is False


class RecordingSocket:
    """Takes what the client sends and never answers: the charger's side is
    the recording."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        return


def connected_client() -> tuple[Wattpilot, RecordingSocket]:
    client = Wattpilot("192.0.2.10", "secret")
    socket = RecordingSocket()
    client._connection.socket = socket  # type: ignore[assignment]
    return client, socket


async def replay(client: Wattpilot, recorded: list[dict[str, Any]]) -> None:
    for frame in recorded:
        await client._handle_message(json.dumps(frame))


async def test_the_recorded_connect_is_taken_in_whole_and_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Five chunks, with inverter frames between the first two, then live
    deltas: every reported property arrives, the handshake is answered, and
    nothing in a real stream is worth a warning."""
    recorded = frames(load("connect-fw42.5.json")["frames"])
    client, socket = connected_client()

    with caplog.at_level(logging.WARNING):
        await replay(client, recorded)

    assert client.properties_initialized
    assert client.serial == "123456"
    assert socket.sent[0]["type"] == "auth", "the recorded handshake was answered"
    reported = {
        key
        for frame in recorded
        if frame.get("type") == "fullStatus"
        for key in frame["status"]
    }
    assert reported <= client.all_properties.keys()
    assert not caplog.records, "real frames produced warnings"


async def test_initialisation_waits_for_the_last_chunk_at_every_step() -> None:
    """The same stream, checked frame by frame: before the last chunk the
    client is never initialised, from it on it always is."""
    recorded = frames(load("connect-fw42.5.json")["frames"])
    client, _ = connected_client()
    last = next(i for i, frame in enumerate(recorded) if is_last_chunk(frame))

    for index, frame in enumerate(recorded):
        await client._handle_message(json.dumps(frame))
        assert client.properties_initialized is (index >= last), index


async def test_a_delta_landing_in_the_recorded_pause_does_not_initialise() -> None:
    """After the restart the stream paused for about a second between its
    third and fourth chunk, while deltas arrive about once a second. None
    fell into the pause in this recording; one moved there must still not
    count as initialisation."""
    after = load("restart-fw42.5.json")["after_restart"]
    recorded = frames(after)
    # Inside the stream only: the handshake before it has a longer gap of
    # its own, and a delta there comes before any chunk -- which is the
    # no-stream case and rightly counts as initialisation.
    first = next(i for i, f in enumerate(recorded) if f.get("type") == "fullStatus")
    last = next(i for i, f in enumerate(recorded) if is_last_chunk(f))
    pause_at = max(
        range(first + 1, last + 1), key=lambda i: after[i]["t"] - after[i - 1]["t"]
    )
    assert after[pause_at]["t"] - after[pause_at - 1]["t"] >= 1.0, "no pause recorded"
    delta = next(frame for frame in recorded if frame.get("type") == "deltaStatus")
    stream = [f for f in recorded if f is not delta]
    stream.insert(pause_at, delta)
    client, _ = connected_client()

    for frame in stream:
        await client._handle_message(json.dumps(frame))
        if is_last_chunk(frame):
            break
        assert not client.properties_initialized, frame.get("type")
    assert client.properties_initialized


async def test_a_restart_is_not_waited_for_because_none_is_answered() -> None:
    """Recorded: after rst=1 no response frame arrives at all; the socket
    closes and the charger reconnects. The restart must return at once, and
    the reconnect's stream must initialise the client again."""
    recording = load("restart-fw42.5.json")
    assert not any(
        frame.get("type") == "response"
        for frame in frames(recording["before_restart"] + recording["after_restart"])
    ), "the recording would no longer show the unanswered restart"
    client, socket = connected_client()
    await replay(client, frames(recording["before_restart"]))
    client.command_timeout = 5.0

    async with asyncio.timeout(0.5):
        await client.set_property("rst", 1)
    assert socket.sent[-1]["type"] == "securedMsg", "the charger asks for signing"
    assert client._pending_commands == {}

    client._connection.begin()  # what the reader does on its reconnect
    assert not client.properties_initialized
    await replay(client, frames(recording["after_restart"]))
    assert client.properties_initialized

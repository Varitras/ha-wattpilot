"""
A scripted charger on the far end of the real client.

FakeWattpilot stands in for the client, so it cannot show what the client
does across a reconnect, a reboot or a pause in the status stream. This
replaces only the socket: client, hub and config entry are the real ones.
"""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import patch

import websockets
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.const import (
    CONF_CONNECTION_TYPE,
    CONF_UPDATE_INTERVAL,
    CONNECTION_LOCAL,
    DOMAIN,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from custom_components.wattpilot.hub import WattpilotHub

PROPERTIES = json.loads(
    (Path(__file__).parent / "fixtures" / "device_properties.json").read_text("utf-8")
)
SERIAL = "123456"
HELLO = {"type": "hello", "serial": SERIAL, "hostname": "Wattpilot", "secured": True}
AUTH_REQUIRED = {
    "type": "authRequired",
    "token1": "a" * 32,
    "token2": "b" * 32,
    "hash": "pbkdf2",
}


class ScriptedSocket:
    """The client's end of one connection."""

    def __init__(self, charger: ScriptedCharger) -> None:
        self.charger = charger
        self.closed = False
        self._frames: asyncio.Queue[str | None] = asyncio.Queue()

    def put(self, frame: dict[str, Any]) -> None:
        self._frames.put_nowait(json.dumps(frame))

    def end(self) -> None:
        self.closed = True
        self._frames.put_nowait(None)

    async def send(self, raw: str) -> None:
        if self.closed:
            raise websockets.exceptions.ConnectionClosedOK(None, None)
        self.charger.answer(self, json.loads(raw))

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.end()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> str:
        frame = await self._frames.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


class ScriptedCharger:
    """Answers the handshake, streams its status, reboots on a firmware write."""

    def __init__(self) -> None:
        self.firmware = PROPERTIES["fwv"]
        self.offline = False
        self.sockets: list[ScriptedSocket] = []
        # The pause in the status stream after a reboot, between its chunks.
        self.status_pause = 0.0
        # The handshake of the next connections, one frame pair each.
        self.next_handshakes: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self._tasks: list[asyncio.Task[None]] = []

    async def connect(self, _url: str, **_options: Any) -> ScriptedSocket:
        if self.offline:
            raise OSError(113, "No route to host")
        socket = ScriptedSocket(self)
        self.sockets.append(socket)
        planned = self.next_handshakes.pop(0) if self.next_handshakes else None
        hello, auth_required = planned or (HELLO, AUTH_REQUIRED)
        socket.put(hello)
        socket.put(auth_required)
        return socket

    def answer(self, socket: ScriptedSocket, message: dict[str, Any]) -> None:
        if message["type"] == "auth":
            socket.put({"type": "authSuccess", "token3": "", "hash": ""})
            self._tasks.append(asyncio.create_task(self._stream(socket)))
            return
        data = json.loads(message["data"]) if "data" in message else message
        socket.put(
            {
                "type": "response",
                "requestId": data["requestId"],
                "success": True,
                "status": {data["key"]: data["value"]},
            }
        )
        if data["key"] == "oct":
            self._tasks.append(asyncio.create_task(self._reboot(socket, data["value"])))

    async def _stream(self, socket: ScriptedSocket) -> None:
        status = {**PROPERTIES, "fwv": self.firmware}
        keys = list(status)
        first = {key: status[key] for key in keys[: len(keys) // 2]}
        rest = {key: status[key] for key in keys[len(keys) // 2 :]}
        (first if "fwv" in first else rest)["fwv"] = self.firmware
        socket.put({"type": "fullStatus", "partial": True, "status": first})
        await asyncio.sleep(self.status_pause)
        socket.put({"type": "fullStatus", "partial": False, "status": rest})

    async def _reboot(self, socket: ScriptedSocket, firmware: str) -> None:
        await asyncio.sleep(0.02)
        socket.end()
        self.offline = True
        await asyncio.sleep(0.1)
        self.firmware = firmware
        self.offline = False

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()


class ScriptedEntry:
    """A loaded config entry whose real client talks to the scripted charger."""

    def __init__(
        self, hass: HomeAssistant, charger: ScriptedCharger, interval: int = 0
    ) -> None:
        self.hass = hass
        self.charger = charger
        self.entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
                "host": "192.168.1.50",
                "password": "secret",
            },
            version=2,
            unique_id=SERIAL,
            options={CONF_UPDATE_INTERVAL: interval},
        )
        fast_client = functools.partial(
            Wattpilot,
            reconnect_delay_min=0.02,
            reconnect_delay_max=0.05,
            connect_timeout=1.0,
            init_timeout=1.0,
        )
        self._patches = [
            patch("websockets.asyncio.client.connect", new=charger.connect),
            patch("custom_components.wattpilot.hub.Wattpilot", new=fast_client),
            patch("custom_components.wattpilot.api.client._REBOOT_RETRY_PAUSE", 0.02),
            patch("custom_components.wattpilot._SETTLED_POLL", 0.01),
        ]

    @property
    def hub(self) -> WattpilotHub | None:
        """Return the entry's hub; a reload has none for a moment."""
        return getattr(self.entry, "runtime_data", None)

    async def __aenter__(self) -> Self:
        for active in self._patches:
            active.start()
        self.entry.add_to_hass(self.hass)
        await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.hass.async_block_till_done()
        return self

    async def __aexit__(self, *_exception: object) -> None:
        await self.hass.async_block_till_done()
        if self.entry.state is ConfigEntryState.LOADED:
            await self.hass.config_entries.async_unload(self.entry.entry_id)
            await self.hass.async_block_till_done()
        self.charger.stop()
        for active in self._patches:
            active.stop()


async def until(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    """Wait for something a real connection takes real time to reach."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.005)
    return False

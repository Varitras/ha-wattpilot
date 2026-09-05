"""How the client reacts to each kind of frame the charger sends.

The dispatch is a table, and every arm changes state that something else
later reads: device identity, the initialisation gate, properties. Testing
it through _handle_message rather than the handlers keeps the routing itself
covered -- a frame arriving under the wrong name is the failure this catches.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.api.exceptions import AuthenticationError


@pytest.fixture
def client() -> Wattpilot:
    return Wattpilot("192.0.2.10", "secret")


async def send(client: Wattpilot, frame: dict[str, Any]) -> None:
    await client._handle_message(json.dumps(frame))


async def test_hello_carries_the_device_identity(client: Wattpilot) -> None:
    await send(
        client,
        {
            "type": "hello",
            "serial": "123456",
            "hostname": "Wattpilot_123456",
            "friendly_name": "Garage",
            "version": "1.2.3",
            "manufacturer": "fronius",
            "devicetype": "wattpilot_V2",
            "protocol": 2,
            "secured": 1,
        },
    )
    assert client.serial == "123456"
    assert client.hostname == "Wattpilot_123456"
    assert client.manufacturer == "fronius"
    assert client.device_type == "wattpilot_V2"
    assert client.protocol == 2
    assert client.secured == 1


async def test_hello_without_the_optional_fields(client: Wattpilot) -> None:
    """Older firmware sends the bare minimum; the rest must default rather
    than raise, or the connection never gets past its first frame."""
    await send(client, {"type": "hello", "serial": "123456"})
    assert client.serial == "123456"
    assert client.manufacturer == ""
    assert client.protocol == 0


async def test_a_full_status_completes_the_initialisation(client: Wattpilot) -> None:
    await send(client, {"type": "fullStatus", "partial": False, "status": {"amp": 16}})
    assert client.properties_initialized
    assert client.amp == 16


async def test_a_partial_full_status_does_not_complete_it(client: Wattpilot) -> None:
    """The charger splits its first status across several frames. Declaring
    the client ready after the first one would let a caller read half a
    device."""
    await send(client, {"type": "fullStatus", "partial": True, "status": {"amp": 16}})
    assert not client.properties_initialized
    assert client.amp == 16


async def test_a_delta_status_also_completes_it(client: Wattpilot) -> None:
    """Some firmware sends deltas straight away without a full status."""
    await send(client, {"type": "deltaStatus", "status": {"amp": 6}})
    assert client.properties_initialized
    assert client.amp == 6


async def test_an_auth_error_is_remembered_for_connect(client: Wattpilot) -> None:
    """The message loop cannot raise at the caller, so connect() picks the
    error up afterwards -- losing it here would hang the connect instead."""
    await send(client, {"type": "authError", "message": "wrong password"})
    assert isinstance(client._fatal_error, AuthenticationError)
    assert "wrong password" in str(client._fatal_error)


async def test_auth_success_opens_the_connection_gate(client: Wattpilot) -> None:
    await send(client, {"type": "authSuccess"})
    assert client.connected
    assert client._connected_event.is_set()


async def test_an_unknown_frame_is_logged_and_ignored(
    client: Wattpilot, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await send(client, {"type": "somethingNew", "status": {}})
    assert "somethingNew" in caplog.text


async def test_inverter_frames_are_accepted_without_handling(
    client: Wattpilot, caplog: pytest.LogCaptureFixture
) -> None:
    """clearInverters/updateInverter are known and deliberately ignored --
    they must not show up as unhandled, or every user's log fills with them."""
    caplog.set_level(logging.DEBUG)
    await send(client, {"type": "clearInverters"})
    await send(client, {"type": "updateInverter"})
    assert "Unhandled" not in caplog.text

"""How the client reacts to each kind of frame the charger sends.

The dispatch is a table, and every arm changes state that something else
later reads: device identity, the initialisation gate, properties. Testing
it through _handle_message rather than the handlers keeps the routing itself
covered -- a frame arriving under the wrong name is the failure this catches.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.api.exceptions import (
    AuthenticationError,
    DeviceIdentityError,
)


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
    assert client.name == "Wattpilot_123456"
    assert client.manufacturer == "fronius"
    # Not public, but they decide the hash for a Flex and signed writes.
    assert client._device.device_type == "wattpilot_V2"
    assert client._device.secured == 1


async def test_hello_without_the_optional_fields(client: Wattpilot) -> None:
    """Older firmware sends the bare minimum; the rest must default rather
    than raise, or the connection never gets past its first frame."""
    await send(client, {"type": "hello", "serial": "123456"})
    assert client.serial == "123456"
    assert client.manufacturer == ""
    assert client.name == ""


async def test_a_full_status_completes_the_initialisation(client: Wattpilot) -> None:
    await send(client, {"type": "fullStatus", "partial": False, "status": {"amp": 16}})
    assert client.properties_initialized
    assert client.all_properties.get("amp") == 16


async def test_a_partial_full_status_does_not_complete_it(client: Wattpilot) -> None:
    """The charger splits its first status across several frames. Declaring
    the client ready after the first one would let a caller read half a
    device."""
    await send(client, {"type": "fullStatus", "partial": True, "status": {"amp": 16}})
    assert not client.properties_initialized
    assert client.all_properties.get("amp") == 16


async def test_a_delta_between_partial_frames_does_not_complete_it(
    client: Wattpilot,
) -> None:
    """A live delta can arrive between the chunks of the first full status:
    recorded after a restart, the stream paused for about a second while
    deltas come once a second. A delta used to count as "initialised" on its
    own, so connect() could return mid-stream -- and the platforms, which only
    create entities for properties already reported, left everything that
    came later without an entity until the entry was reloaded."""
    await send(client, {"type": "fullStatus", "partial": True, "status": {"amp": 16}})
    await send(client, {"type": "deltaStatus", "status": {"amp": 6}})
    assert not client.properties_initialized

    await send(
        client, {"type": "fullStatus", "partial": False, "status": {"ebe": True}}
    )
    assert client.properties_initialized


async def test_a_new_connection_starts_without_a_stream_in_progress(
    client: Wattpilot,
) -> None:
    """A connection that dropped mid-stream must not leave the next one
    waiting for a final chunk that will never come."""
    await send(client, {"type": "fullStatus", "partial": True, "status": {"amp": 16}})
    client._connection.begin()
    await send(client, {"type": "deltaStatus", "status": {"amp": 6}})
    assert client.properties_initialized


async def test_a_delta_status_also_completes_it(client: Wattpilot) -> None:
    """Some firmware sends deltas straight away without a full status."""
    await send(client, {"type": "deltaStatus", "status": {"amp": 6}})
    assert client.properties_initialized
    assert client.all_properties.get("amp") == 6


async def test_an_auth_error_is_remembered_for_connect(client: Wattpilot) -> None:
    """The message loop cannot raise at the caller, so connect() picks the
    error up afterwards -- losing it here would hang the connect instead."""
    await send(client, {"type": "authError", "message": "wrong password"})
    assert isinstance(client._connection.fatal_error, AuthenticationError)
    assert "wrong password" in str(client._connection.fatal_error)


async def test_the_refusal_says_which_one_it_was(client: Wattpilot) -> None:
    """The hub answers the two refusals differently -- a new password for
    one, a repair notice for the other -- so it has to be able to tell a
    different charger from a refused password."""
    assert client.refusal is None

    await send(client, {"type": "hello", "serial": "123456"})
    await send(client, {"type": "hello", "serial": "999999"})
    assert isinstance(client.refusal, DeviceIdentityError)

    await send(client, {"type": "authError", "message": "wrong password"})
    assert isinstance(client.refusal, AuthenticationError)


async def test_auth_success_opens_the_connection_gate(client: Wattpilot) -> None:
    await send(client, {"type": "authSuccess"})
    assert client.connected
    assert client._connection._authenticated_event.is_set()


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


async def test_a_malformed_property_costs_neither_its_siblings_nor_the_status(
    client: Wattpilot,
) -> None:
    """One value the client cannot read ended the whole frame: the keys after
    it were lost, and in the last chunk of the first full status the status
    never completed, so connecting ran into its timeout (audit A18-16)."""
    await send(
        client,
        {
            "type": "fullStatus",
            "partial": False,
            "status": {"nrg": [230.0], "amp": 16},
        },
    )
    assert client.all_properties.get("amp") == 16
    assert client.properties_initialized


async def test_an_acceptance_with_a_null_status_answers_the_command(
    client: Wattpilot,
) -> None:
    """`status: null` raised before the waiting command was answered, so a
    write the charger had accepted was reported as unanswered (A18-19)."""
    waiter = asyncio.get_running_loop().create_future()
    client._pending_commands["7"] = waiter
    await send(
        client, {"type": "response", "requestId": 7, "success": True, "status": None}
    )
    assert waiter.done()
    assert waiter.exception() is None


async def test_an_unknown_password_hash_is_a_named_refusal(client: Wattpilot) -> None:
    """An algorithm this client does not know made the frame unreadable: no
    answer was sent and setup timed out without naming the cause (A18-18)."""
    await send(
        client, {"type": "authRequired", "token1": "a", "token2": "b", "hash": "argon2"}
    )
    assert client._connection.fatal_error is not None
    assert "argon2" in str(client._connection.fatal_error)


async def test_an_unknown_password_hash_drops_the_socket(client: Wattpilot) -> None:
    """Left open, the refused socket counted as connected until the readiness
    guard gave up on it, as a wrong charger's does not (reaudit R1-13)."""

    class Socket:
        closed = False

        async def close(self) -> None:
            self.closed = True

    socket = client._connection.socket = Socket()  # type: ignore[assignment]
    await send(
        client, {"type": "authRequired", "token1": "a", "token2": "b", "hash": "argon2"}
    )
    assert socket.closed

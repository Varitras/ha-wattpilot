"""How the client reacts to each kind of frame the charger sends.

The dispatch is a table, and every arm changes state that something else
later reads: device identity, the initialisation gate, properties. Testing
it through _handle_message rather than the handlers keeps the routing itself
covered -- a frame arriving under the wrong name is the failure this catches.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import pytest

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.api.exceptions import (
    AuthenticationError,
    CommandError,
    DeviceIdentityError,
    WattpilotConnectionError,
)

HELLO = {"type": "hello", "serial": "123456"}
AUTH_REQUIRED = {"type": "authRequired", "token1": "a" * 32, "token2": "b" * 32}


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
    never completed, so connecting ran into its timeout."""
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
    write the charger had accepted was reported as unanswered."""
    waiter = asyncio.get_running_loop().create_future()
    client._pending_commands["7"] = waiter
    await send(
        client, {"type": "response", "requestId": 7, "success": True, "status": None}
    )
    assert waiter.done()
    assert waiter.exception() is None


async def test_an_unknown_password_hash_is_a_named_refusal(client: Wattpilot) -> None:
    """An algorithm this client does not know made the frame unreadable: no
    answer was sent and setup timed out without naming the cause."""
    await send(client, HELLO)
    await send(client, {**AUTH_REQUIRED, "hash": "argon2"})
    assert client._connection.fatal_error is not None
    assert "argon2" in str(client._connection.fatal_error)


async def test_an_unknown_password_hash_drops_the_socket(client: Wattpilot) -> None:
    """Left open, the refused socket counted as connected until the readiness
    guard gave up on it, as a wrong charger's does not."""

    class Socket:
        closed = False

        async def close(self) -> None:
            self.closed = True

    socket = client._connection.socket = Socket()  # type: ignore[assignment]
    await send(client, HELLO)
    await send(client, {**AUTH_REQUIRED, "hash": "argon2"})
    assert socket.closed


@pytest.mark.parametrize("status", [None, [], "text", 3])
async def test_an_unreadable_full_status_does_not_complete_the_snapshot(
    client: Wattpilot, status: Any
) -> None:
    """A last chunk whose status is no object completed the snapshot: setup
    then built its entities without the properties, and later deltas never
    added them."""
    await send(client, {"type": "fullStatus", "partial": True, "status": {"fwv": "1"}})
    with contextlib.suppress(TypeError):
        await send(client, {"type": "fullStatus", "partial": False, "status": status})

    assert not client.properties_initialized


@pytest.mark.parametrize("status", [None, [], "text", 3])
async def test_an_unreadable_delta_does_not_complete_the_snapshot(
    client: Wattpilot, status: Any
) -> None:
    """Firmware without a streamed full status counts a delta as complete."""
    with contextlib.suppress(TypeError):
        await send(client, {"type": "deltaStatus", "status": status})

    assert not client.properties_initialized


def test_a_value_nested_too_deep_costs_only_itself() -> None:
    """A value nested past the recursion limit cost the rest of its frame,
    and in the last chunk of the first full status the initialisation."""
    from types import SimpleNamespace  # noqa: PLC0415 -- the wire shape

    client = Wattpilot("192.0.2.10", "secret")
    nested: object = 1
    for _ in range(1200):
        nested = SimpleNamespace(x=nested)
    client._apply_status(SimpleNamespace(amp=nested, lmo=3))

    assert client.all_properties.get("lmo") == 3


@pytest.mark.parametrize("unusable", [None, "", 123, ["x"]], ids=repr)
@pytest.mark.parametrize(
    ("frame", "field"),
    [(HELLO, "serial"), (AUTH_REQUIRED, "token1"), (AUTH_REQUIRED, "token2")],
)
async def test_a_handshake_without_a_usable_field_is_refused_by_name(
    client: Wattpilot,
    monkeypatch: pytest.MonkeyPatch,
    frame: dict[str, Any],
    field: str,
    unusable: object,
) -> None:
    """A field another firmware leaves out or types differently ended in a
    timeout with no cause, or in an answer built from an empty secret."""
    sent: list[Any] = []

    async def record(message: Any) -> None:
        sent.append(message)

    monkeypatch.setattr(client, "_send", record)
    if frame is AUTH_REQUIRED:
        await send(client, HELLO)
    await send(client, {**frame, field: unusable})
    assert isinstance(client.refusal, WattpilotConnectionError)
    assert field in str(client.refusal)
    assert sent == [], "nothing may be answered"


@pytest.mark.parametrize("field", ["serial", "token1"])
async def test_a_missing_handshake_field_is_refused_by_name(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setattr(client, "_send", None)
    frame = HELLO if field == "serial" else AUTH_REQUIRED
    if frame is AUTH_REQUIRED:
        await send(client, HELLO)
    await send(client, {key: value for key, value in frame.items() if key != field})
    assert field in str(client.refusal)


async def test_an_auth_request_before_hello_is_refused(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a serial there is nothing to derive the secret from, and the
    answer went out built from an empty one."""
    monkeypatch.setattr(client, "_send", None)
    await send(client, AUTH_REQUIRED)
    assert isinstance(client.refusal, WattpilotConnectionError)


async def test_a_response_without_success_fails_its_command_at_once(
    client: Wattpilot,
) -> None:
    """It waited for the command timeout instead, as the frame was dropped."""
    future = asyncio.get_running_loop().create_future()
    client._pending_commands["7"] = future
    await send(client, {"type": "response", "requestId": 7})
    assert isinstance(future.exception(), CommandError)

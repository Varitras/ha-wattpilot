"""The authentication handshake's arithmetic.

Every function here is deterministic, and the charger is the other half of
each computation -- if any of it drifts, the device simply refuses the
connection with no explanation. So the values are pinned, not just the
shapes.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from custom_components.wattpilot.api import client as client_module
from custom_components.wattpilot.api.auth import (
    compute_auth_response,
    generate_token,
    hash_password,
    sign_secured_message,
)
from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.api.models import AuthHashType

SERIAL = "123456"
PASSWORD = "hunter2"  # noqa: S105 -- test input, not a credential


@pytest.mark.parametrize("hash_type", [AuthHashType.PBKDF2, AuthHashType.BCRYPT])
def test_hashing_is_deterministic_and_serial_bound(hash_type: AuthHashType) -> None:
    """The serial is the salt: the same password on another charger must not
    produce the same secret, or one device's capture would open the next."""
    first = hash_password(PASSWORD, SERIAL, hash_type)
    assert first == hash_password(PASSWORD, SERIAL, hash_type)
    assert first != hash_password(PASSWORD, "654321", hash_type)
    assert first != hash_password("other", SERIAL, hash_type)
    assert isinstance(first, bytes)
    assert first


def test_an_unknown_hash_type_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown hash type"):
        hash_password(PASSWORD, SERIAL, "argon2")  # type: ignore[arg-type]


def test_the_auth_response_matches_the_documented_construction() -> None:
    """sha256(token3 + token2 + sha256(token1 + secret)). Pinned against a
    hand-computed value: an accidental reordering still produces a plausible
    hex string, and only the charger would notice."""
    import hashlib  # noqa: PLC0415 -- the point is to recompute independently

    secret = b"secret-bytes"
    inner = hashlib.sha256(b"t1" + secret).hexdigest()
    expected = hashlib.sha256(("t3" + "t2" + inner).encode()).hexdigest()

    assert compute_auth_response("t1", "t2", "t3", secret) == expected


def test_tokens_are_random_and_of_the_expected_length() -> None:
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50, "a repeated token would break the handshake"
    assert all(len(token) == 32 for token in tokens)


def test_a_secured_message_keeps_its_payload_and_signs_it() -> None:
    """The charger verifies the HMAC over the exact payload string, so the
    envelope must carry that string rather than re-serialise it later."""
    import hashlib  # noqa: PLC0415 -- independent recomputation
    import hmac  # noqa: PLC0415 -- independent recomputation

    secret = b"secret-bytes"
    message = {"type": "setValue", "requestId": 7, "key": "amp", "value": 10}
    envelope = sign_secured_message(message, secret)

    assert envelope["type"] == "securedMsg"
    assert envelope["requestId"] == "7sm"
    assert json.loads(envelope["data"]) == message
    assert (
        envelope["hmac"]
        == hmac.new(
            bytearray(secret), bytearray(envelope["data"].encode()), hashlib.sha256
        ).hexdigest()
    )


def test_a_different_secret_signs_differently() -> None:
    message = {"requestId": 1, "key": "amp", "value": 6}
    assert (
        sign_secured_message(message, b"a")["hmac"]
        != sign_secured_message(message, b"b")["hmac"]
    )


async def test_authentication_hashes_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PBKDF2 with 100,000 rounds measured 110-116 ms per handshake. Home
    Assistant runs this client in its event loop, so every connection stalled
    it for that long (audit A11-08)."""
    client = Wattpilot("192.0.2.10", "secret")
    client._device.serial = "123456"

    hashed_in: list[int] = []
    real_hash = client_module.hash_password

    def spy(password: str, serial: str, hash_type: object) -> bytes:
        hashed_in.append(threading.get_ident())
        return real_hash(password, serial, hash_type)  # type: ignore[arg-type]

    monkeypatch.setattr(client_module, "hash_password", spy)

    sent: list[dict[str, Any]] = []

    async def fake_send(message: dict[str, Any], **_kwargs: object) -> None:
        sent.append(message)

    monkeypatch.setattr(client, "_send", fake_send)

    await client._handle_message(
        json.dumps(
            {
                "type": "authRequired",
                "hash": "pbkdf2",
                "token1": "aaaa",
                "token2": "bbbb",
            }
        )
    )

    assert hashed_in, "the handshake did not hash at all"
    assert threading.get_ident() not in hashed_in, "hashing ran on the event loop"
    assert sent
    assert sent[0]["type"] == "auth"

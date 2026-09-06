"""What the client puts on the wire for each command.

Every one of these ends in set_property, so the interesting part is the
translation before it: which key, which value, and which conversions the
charger's own quirks force on us.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.api.exceptions import WattpilotConnectionError
from custom_components.wattpilot.api.models import LoadMode

if TYPE_CHECKING:
    from collections.abc import Callable


class RecordingSocket:
    """Answers every command with success, so callers do not wait."""

    def __init__(self, client_getter: Callable[[], Wattpilot]) -> None:
        self.sent: list[dict[str, Any]] = []
        self._client_getter = client_getter

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        from types import SimpleNamespace  # noqa: PLC0415 -- test-local shape

        self._client_getter()._on_response(
            SimpleNamespace(
                requestId=message["requestId"], success=True, status=SimpleNamespace()
            )
        )

    async def close(self) -> None:
        return


@pytest.fixture
def client() -> Wattpilot:
    instance = Wattpilot("192.0.2.10", "secret")
    instance._connection.socket = RecordingSocket(lambda: instance)  # type: ignore[assignment]
    instance._connection.mark_authenticated()
    instance._device.secured = 0
    return instance


def sent_values(client: Wattpilot) -> list[tuple[str, Any]]:
    socket: Any = client._connection.socket
    return [(m["key"], m["value"]) for m in socket.sent]


async def test_set_power_writes_the_current(client: Wattpilot) -> None:
    await client.set_power(12)
    assert sent_values(client) == [("amp", 12)]


async def test_set_mode_writes_the_mode_as_a_number(client: Wattpilot) -> None:
    await client.set_mode(LoadMode.ECO)
    assert sent_values(client) == [("lmo", int(LoadMode.ECO))]


async def test_next_trip_energy_sets_the_unit_first(client: Wattpilot) -> None:
    """The charger interprets the number according to a separate unit flag,
    so writing the value without the flag can mean something else entirely."""
    await client.set_next_trip_energy(20)
    assert sent_values(client) == [("esk", True), ("fte", 20)]


async def test_next_trip_is_sent_as_seconds_since_midnight(
    client: Wattpilot,
) -> None:
    await client.set_next_trip(datetime.time(7, 30))
    assert sent_values(client) == [("ftt", 7 * 3600 + 30 * 60)]


@pytest.mark.parametrize("scheme", [0, 1, 2])
async def test_the_departure_is_written_unshifted_in_every_zone_mode(
    client: Wattpilot, scheme: int
) -> None:
    """`ftt` is seconds since local midnight, full stop.

    The adopted client added an hour when `tds` announced a daylight-saving
    scheme and the process clock was in summer time. Measured on the real
    charger on 2026-09-06, during summer time and with `tds` = 1: a departure
    set to 07:30 in the Wattpilot app reads back as 27000, not 30600. The
    firmware applies no offset, so neither do we (audit A12-06).

    Parametrized over every `tds` value because that announcement is what the
    old branch keyed on. There is no clock to pin any more: the write is
    unconditional, and `import time` left the module with the branch.
    """
    client._all_props["tds"] = scheme

    await client.set_next_trip(datetime.time(7, 30))
    assert sent_values(client) == [("ftt", 7 * 3600 + 30 * 60)]


async def test_disable_cloud_api_writes_the_flag(client: Wattpilot) -> None:
    await client.disable_cloud_api()
    assert sent_values(client) == [("cae", False)]


async def test_enable_cloud_api_returns_the_key_the_charger_reports(
    client: Wattpilot,
) -> None:
    async def answer_with_key() -> None:
        await asyncio.sleep(0)
        client._update_property("cak", "SECRET")
        client._update_property("cae", True)  # noqa: FBT003 -- a device value

    task = asyncio.ensure_future(answer_with_key())
    info = await client.enable_cloud_api(timeout=5)
    await task

    assert info.api_key == "SECRET"
    assert client.serial in info.url


async def test_enable_cloud_api_gives_up_rather_than_waiting_forever(
    client: Wattpilot,
) -> None:
    with pytest.raises(WattpilotConnectionError, match="Timeout"):
        await client.enable_cloud_api(timeout=0.01)

"""What the client puts on the wire for each command.

Every one of these ends in set_property, so the interesting part is the
translation before it: which key, which value, and which conversions the
charger's own quirks force on us.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time
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
    instance._ws = RecordingSocket(lambda: instance)  # type: ignore[assignment]
    instance._connected = True
    instance._device.secured = 0
    return instance


def sent_values(client: Wattpilot) -> list[tuple[str, Any]]:
    socket: Any = client._ws
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


@pytest.mark.parametrize("scheme", [1, 2])
async def test_a_dst_aware_charger_gets_the_hour_added(
    client: Wattpilot, scheme: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `tds` 1 or 2 the charger expects standard time, so a departure
    during daylight saving has to be shifted.

    Asked through time.localtime(), not through an aware datetime: an
    astimezone() result carries a *fixed offset*, and a fixed offset's dst()
    is None -- which is why this adjustment never ran (audit A11-04). The
    direction of the shift is the charger's documented contract and is not
    verified against hardware here.
    """
    client._all_props["tds"] = scheme
    monkeypatch.setattr(
        "custom_components.wattpilot.api.client.time.localtime",
        lambda: time.struct_time((2026, 7, 1, 12, 0, 0, 2, 182, 1)),
    )

    await client.set_next_trip(datetime.time(7, 30))
    assert sent_values(client) == [("ftt", 7 * 3600 + 30 * 60 + 3600)]


async def test_outside_daylight_saving_the_departure_is_unshifted(
    client: Wattpilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    client._all_props["tds"] = 1
    monkeypatch.setattr(
        "custom_components.wattpilot.api.client.time.localtime",
        lambda: time.struct_time((2026, 1, 1, 12, 0, 0, 3, 1, 0)),
    )

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

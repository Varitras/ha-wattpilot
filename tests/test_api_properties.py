"""The client's property table and type coercion.

Both are tables rather than algorithms: one arm per charger key, one branch
per JSON type. Tests for them are therefore table-driven too -- a case per
row, so a missing arm shows up as a missing row rather than as a subtly
wrong number somewhere else.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.api.exceptions import PropertyError

# charger key -> (pushed value, property name on the client, expected value)
SIMPLE_PROPERTIES: list[tuple[str, Any, str, Any]] = [
    ("acs", 1, "access_state", 1),
    ("cbl", 20, "cable_type", 20),
    ("fhz", 50.01, "frequency", 50.01),
    (
        "pha",
        [False, False, False, True, True, True],
        "phases",
        [False] * 3 + [True] * 3,
    ),
    ("wh", 1234.5, "energy_counter_since_start", 1234.5),
    ("err", 0, "error_state", 0),
    ("ust", 2, "cable_lock", 2),
    ("eto", 2494780, "energy_counter_total", 2494780),
    ("cae", True, "cae", True),
    ("cak", "key", "cak", "key"),
    ("lmo", 4, "mode", 4),
    ("car", 2, "car_connected", 2),
    ("alw", True, "allow_charging", True),
    ("amp", 16, "amp", 16),
    ("version", "1.2.3", "version", "1.2.3"),
    ("fwv", "42.5", "firmware", "42.5"),
    ("wss", "TestNet", "wifi_ssid", "TestNet"),
]


@pytest.fixture
def client() -> Wattpilot:
    return Wattpilot("192.0.2.10", "secret")


@pytest.mark.parametrize(("key", "pushed", "attribute", "expected"), SIMPLE_PROPERTIES)
def test_a_pushed_property_reaches_its_getter(
    client: Wattpilot, key: str, pushed: Any, attribute: str, expected: Any
) -> None:
    client._update_property(key, pushed)
    assert getattr(client, attribute) == expected
    # Every property is also kept raw, whatever the table does with it.
    assert client.all_properties[key] == pushed


def test_the_energy_array_is_split_into_phases(client: Wattpilot) -> None:
    """`nrg` is one array carrying twelve readings; the client fans it out.
    The three power values are watts on the wire and kilowatts on the API."""
    client._update_property(
        "nrg", [230.0, 231.0, 232.0, 1.5, 6.0, 6.1, 6.2, 1400, 1410, 1420, 5, 4230]
    )
    assert (client.voltage1, client.voltage2, client.voltage3) == (230.0, 231.0, 232.0)
    assert client.voltage_n == 1.5
    assert (client.amps1, client.amps2, client.amps3) == (6.0, 6.1, 6.2)
    assert client.power1 == pytest.approx(1.4)
    assert client.power_n == pytest.approx(0.005)
    assert client.power == pytest.approx(4.23)


def test_the_connected_access_point_also_yields_the_ssid(client: Wattpilot) -> None:
    """Current firmware reports the AP in `ccw`, not in `wss`."""
    client._update_property("ccw", SimpleNamespace(ssid="TestNet"))
    assert client.wifi_ssid == "TestNet"

    client._update_property("ccw", SimpleNamespace(ssid=""))
    assert client.wifi_ssid == "TestNet", "an empty ssid must not erase the known one"


def test_an_unknown_property_is_kept_without_a_getter(client: Wattpilot) -> None:
    """The table covers what the client exposes; everything else still has to
    survive, because the integration reads properties by key."""
    client._update_property("zzz", 42)
    assert client.all_properties["zzz"] == 42


async def test_property_callbacks_see_every_change(client: Wattpilot) -> None:
    seen: list[tuple[str, Any]] = []

    unsubscribe = client.on_property_change(
        lambda name, value: seen.append((name, value))
    )
    client._update_property("amp", 10)
    unsubscribe()
    client._update_property("amp", 12)

    assert seen == [("amp", 10)]


# json type -> (input, expected) pairs the charger's definition can ask for
COERCIONS: list[tuple[str, Any, Any]] = [
    ("boolean", True, True),
    ("boolean", "true", True),
    ("boolean", "false", False),
    ("boolean", 1, True),
    ("boolean", 0, False),
    ("integer", 5, 5),
    ("integer", "5", 5),
    ("integer", 5.7, 5),
    ("float", 1.5, 1.5),
    ("float", "1.5", 1.5),
    ("float", 2, 2.0),
    ("string", "x", "x"),
    ("string", 5, "5"),
]


@pytest.mark.parametrize(("json_type", "value", "expected"), COERCIONS)
def test_a_value_is_coerced_to_the_type_the_charger_declares(
    client: Wattpilot, json_type: str, value: Any, expected: Any
) -> None:
    assert client._coerce_to_json_type(value, json_type, "amp") == expected


@pytest.mark.parametrize(
    ("json_type", "value"),
    [("boolean", "maybe"), ("integer", "x"), ("float", "x")],
)
def test_a_value_that_cannot_be_coerced_is_refused(
    client: Wattpilot, json_type: str, value: Any
) -> None:
    """Sending a wrong type would be rejected by the charger anyway; failing
    here says which property and which value."""
    with pytest.raises(PropertyError, match="amp"):
        client._coerce_to_json_type(value, json_type, "amp")


def test_an_unknown_json_type_passes_the_value_through(client: Wattpilot) -> None:
    """The definition file may name a type this client does not know; that is
    not a reason to refuse the write."""
    assert client._coerce_to_json_type("x", "something-new", "amp") == "x"

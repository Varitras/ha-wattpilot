"""The client's property table and type coercion.

Both are tables rather than algorithms: one arm per charger key, one branch
per JSON type. Tests for them are therefore table-driven too -- a case per
row, so a missing arm shows up as a missing row rather than as a subtly
wrong number somewhere else.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.api.exceptions import PropertyError

# charger key -> (pushed value, property name on the client, expected value)
SIMPLE_PROPERTIES: list[tuple[str, Any, str, Any]] = [
    ("version", "1.2.3", "version", "1.2.3"),
    ("fwv", "42.5", "firmware", "42.5"),
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


def test_null_is_sent_where_the_charger_declares_the_value_optional(
    client: Wattpilot,
) -> None:
    """dwo (energy limit per charge) is `optional<double>`, and null is how
    the charger is told the limit is off. Coercing it as a float refused
    exactly that value; a key that is not optional still refuses it."""
    assert client._coerce_value("dwo", None) is None
    with pytest.raises(PropertyError, match="amp"):
        client._coerce_value("amp", None)


def test_an_unknown_json_type_passes_the_value_through(client: Wattpilot) -> None:
    """The definition file may name a type this client does not know; that is
    not a reason to refuse the write."""
    assert client._coerce_to_json_type("x", "something-new", "amp") == "x"

"""The API definition: validation, child-property expansion, lookups.

The definition is the charger's own description of its properties. It is
parsed once at connect, and every write is typed against it -- so a silently
mis-parsed entry turns into a rejected command much later, with nothing
pointing back here.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.wattpilot.api.definition import (
    ApiDefinition,
    get_all_properties,
    get_child_property_value,
    load_api_definition,
    validate_api_definition,
)
from custom_components.wattpilot.api.utils import value_to_json

MINIMAL = {"messages": [], "properties": []}


def test_a_well_formed_definition_passes() -> None:
    assert validate_api_definition(MINIMAL) == MINIMAL


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ("not a mapping", TypeError),
        ({"properties": []}, ValueError),
        ({"messages": {}, "properties": []}, ValueError),
        ({"messages": []}, ValueError),
        ({"messages": [], "properties": {}}, ValueError),
        ({"messages": [], "properties": [{"key": "a", "childProps": "no"}]}, TypeError),
    ],
)
def test_a_malformed_definition_is_refused(config: Any, expected: type) -> None:
    """A wrong *type* raises TypeError, a missing or wrong-shaped *entry*
    raises ValueError -- the distinction is what tells a caller whether the
    file is broken or merely incomplete."""
    with pytest.raises(expected):
        validate_api_definition(config)


def test_the_shipped_definition_loads_and_splits_children() -> None:
    """The file that actually ships. Splitting turns array/object properties
    into addressable children, which is what the integration reads."""
    split = load_api_definition(split_properties=True)
    unsplit = load_api_definition(split_properties=False)

    assert len(split.properties) > len(unsplit.properties)
    assert split.split_properties, "no child property was generated"
    assert "nrg" in unsplit.properties


def make_definition() -> ApiDefinition:
    definition = ApiDefinition(config={})
    definition.properties = {
        "arr": {"key": "arr", "jsonType": "array"},
        "obj": {"key": "obj", "jsonType": "object"},
        "flat": {"key": "flat", "jsonType": "integer"},
        "arr_1": {"key": "arr_1", "parentProperty": "arr", "valueRef": "1"},
        "arr_9": {"key": "arr_9", "parentProperty": "arr", "valueRef": "9"},
        "obj_x": {"key": "obj_x", "parentProperty": "obj", "valueRef": "x"},
        "flat_x": {"key": "flat_x", "parentProperty": "flat", "valueRef": "x"},
        "orphan": {"key": "orphan"},
    }
    return definition


def test_a_child_reads_its_slot_of_the_parent_array() -> None:
    assert get_child_property_value(make_definition(), {"arr": [10, 11]}, "arr_1") == 11


def test_a_child_beyond_the_parents_length_is_unknown() -> None:
    """Firmware sends shorter arrays than the definition describes; that is
    a missing value, not an IndexError in the middle of a status update."""
    assert get_child_property_value(make_definition(), {"arr": [10]}, "arr_9") is None


@pytest.mark.parametrize(
    "parent", [SimpleNamespace(x=5), {"x": 5}], ids=["namespace", "dict"]
)
def test_a_child_reads_its_field_of_the_parent_object(parent: Any) -> None:
    """The charger's objects arrive as namespaces from the JSON decoder and
    as dicts from the property cache; both have to work."""
    assert get_child_property_value(make_definition(), {"obj": parent}, "obj_x") == 5


def test_an_unset_parent_yields_no_child_value() -> None:
    definition = make_definition()
    assert get_child_property_value(definition, {}, "arr_1") is None
    assert get_child_property_value(definition, {}, "obj_x") is None


def test_an_unmappable_child_is_logged_rather_than_guessed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    definition = make_definition()

    assert get_child_property_value(definition, {"obj": "a string"}, "obj_x") is None
    assert get_child_property_value(definition, {"flat": 5}, "flat_x") is None
    assert get_child_property_value(definition, {}, "orphan") is None

    assert "Unable to map child property obj_x" in caplog.text
    assert "cannot be split" in caplog.text
    assert "not linked to a parent property" in caplog.text


def test_all_properties_merges_children_into_the_flat_view() -> None:
    definition = make_definition()
    definition.split_properties = ["arr_1"]
    merged = get_all_properties(definition, {"arr": [10, 11], "flat": 3})

    assert merged["flat"] == 3
    assert merged["arr_1"] == 11


def test_namespaces_survive_json_serialisation() -> None:
    """The warning above prints the offending value, and a namespace is not
    JSON-serialisable on its own."""
    assert value_to_json(SimpleNamespace(a=1)) == '{"a": 1}'
    assert value_to_json([1, 2]) == "[1, 2]"

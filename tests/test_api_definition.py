"""The API definition: validation and loading.

The definition is the charger's own description of its properties. It is
parsed once at connect, and every write is typed against it -- so a silently
mis-parsed entry turns into a rejected command much later, with nothing
pointing back here.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.wattpilot.api.definition import (
    load_api_definition,
    validate_api_definition,
)

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
    ],
)
def test_a_malformed_definition_is_refused(config: Any, expected: type) -> None:
    """A wrong *type* raises TypeError, a missing or wrong-shaped *entry*
    raises ValueError -- the distinction is what tells a caller whether the
    file is broken or merely incomplete."""
    with pytest.raises(expected):
        validate_api_definition(config)


def test_the_shipped_definition_loads() -> None:
    """The file that actually ships, with the properties the client types
    its writes against."""
    definition = load_api_definition()
    assert definition.properties["nrg"]["jsonType"] == "array"
    assert definition.properties["amp"]["jsonType"] == "integer"

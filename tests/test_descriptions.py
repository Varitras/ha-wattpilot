"""Constraint parsing and description filtering against real device data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.api.definition import load_api_definition
from custom_components.wattpilot.api.exceptions import PropertyError
from custom_components.wattpilot.descriptions import (
    BUTTON_DESCRIPTIONS,
    NUMBER_DESCRIPTIONS,
    SELECT_DESCRIPTIONS,
    SENSOR_DESCRIPTIONS,
    SWITCH_DESCRIPTIONS,
    WattpilotDescriptionMixin,
    filter_supported,
    firmware_supported,
    variant_supported,
)

DEVICE = json.loads(
    (Path(__file__).parent / "fixtures" / "device_properties.json").read_text(
        encoding="utf-8"
    )
)


@dataclass(frozen=True, kw_only=True)
class Desc(WattpilotDescriptionMixin):
    key: str = "x"


@pytest.mark.parametrize(
    ("current", "constraint", "expected"),
    [
        ("42.5", None, True),
        ("42.5", ">=41.7", True),
        ("40.7", ">=41.7", False),
        ("40.7", "<=40.7", True),
        ("42.5", "<=40.7", False),
        ("38.4", "<38.5", True),
        ("38.5", "<38.5", False),
        ("38.5", ">=38.5", True),
        (None, ">=38.5", False),  # unknown firmware -> constrained entity off
        ("42.5", ">=", False),  # broken constraint -> off, never crash
        ("garbage", ">=38.5", False),
    ],
)
def test_firmware_supported(current, constraint, expected) -> None:
    assert firmware_supported(current, constraint) is expected


def test_variant_supported() -> None:
    assert variant_supported(11, None) is True
    assert variant_supported(11, "11") is True
    assert variant_supported(11, "22") is False
    assert variant_supported(None, "11") is False


def test_uid_suffix_default_and_override() -> None:
    assert Desc(charger_key="amp").uid_suffix == "amp"
    assert Desc(charger_key="amp", uid="amp_22kw").uid_suffix == "amp_22kw"


def test_filter_supported_against_device() -> None:
    # Each line pins one outcome against the real fixture (fwv=42.5, var=11).
    # present/absent below means "is a key in device_properties.json":
    #   amp, variant=11    -> kept-by-variant
    #   amp, variant=22    -> dropped-by-variant
    #   eto, fw<=40.7      -> dropped-by-firmware-despite-being-present (eto present)
    #   ebe, fw>=41.7      -> kept-by-firmware (ebe present)
    #   does_not_exist     -> dropped-by-absence (key absent)
    #   rst, no-property   -> write-only escape (rst absent, kept anyway)
    #   rst, no-property, fw>=99.0 -> escape-does-not-bypass-firmware
    descriptions = [
        Desc(charger_key="amp", variant="11"),
        Desc(charger_key="amp", uid="amp_22kw", variant="22"),
        Desc(charger_key="eto", firmware="<=40.7"),
        Desc(charger_key="ebe", firmware=">=41.7"),
        Desc(charger_key="does_not_exist"),
        Desc(charger_key="rst", requires_property=False),
        Desc(
            charger_key="rst",
            uid="rst_gated",
            requires_property=False,
            firmware=">=99.0",
        ),
    ]
    kept = filter_supported(
        descriptions, firmware="42.5", variant=DEVICE["var"], properties=DEVICE
    )
    suffixes = [d.uid_suffix for d in kept]
    assert suffixes == ["amp", "ebe", "rst"]


def test_a_companion_role_always_comes_with_its_companion() -> None:
    """The sensor classes read the companion a role names without checking
    for it; a role without a key would read nothing and show nothing."""
    for description in SENSOR_DESCRIPTIONS:
        if description.companion_role is not None:
            assert description.companion_key, description.key


def _written_values() -> list[tuple[str, str, object]]:
    """(entity, key, value) for every fixed value an entity writes."""
    written: list[tuple[str, str, object]] = []
    for description in SWITCH_DESCRIPTIONS:
        written += [
            (description.key, description.charger_key, on) for on in (True, False)
        ]
    for description in SELECT_DESCRIPTIONS:
        options = description.select_options or {}
        written += [(description.key, description.charger_key, key) for key in options]
    for description in BUTTON_DESCRIPTIONS:
        written.append(
            (description.key, description.charger_key, description.press_value)
        )
    return written


def test_every_written_value_reaches_the_charger_unchanged() -> None:
    """
    The client converts each write to the type the API definition declares.
    A value that conversion would change goes out as something else: the
    lock option "never locked" (3) was sent as true. A number written to an
    integer key has to be a whole number before it gets there.
    """
    client = Wattpilot("192.0.2.10", "secret")
    definition = load_api_definition()

    def declared(key: str) -> str:
        return str(definition.properties.get(key, {}).get("jsonType", ""))

    changed = []
    for entity, key, value in _written_values():
        try:
            sent = client._coerce_to_json_type(value, declared(key), key)
        except PropertyError:
            sent = "<refused>"
        if sent != value or isinstance(sent, bool) != isinstance(value, bool):
            changed.append(f"{entity}: {key}={value!r} -> {sent!r}")
    changed += [
        f"{description.key}: {description.charger_key} is an integer, "
        "the number is not rounded"
        for description in NUMBER_DESCRIPTIONS
        if declared(description.charger_key) == "integer" and not description.set_as_int
    ]
    assert changed == []


# The device's own values against the type declared for them, for every key
# the integration writes: the declaration decides what is sent.
_DECLARED_AS = {
    "boolean": (bool,),
    "integer": (int,),
    "float": (int, float),
    "string": (str,),
}


def test_every_written_key_is_declared_as_the_charger_reports_it() -> None:
    definition = load_api_definition()
    written = {key for _entity, key, _value in _written_values()} | {
        description.charger_key for description in NUMBER_DESCRIPTIONS
    }
    mismatched = []
    for key in sorted(written):
        json_type = str(definition.properties.get(key, {}).get("jsonType", ""))
        reported = DEVICE.get(key)
        if json_type not in _DECLARED_AS or reported is None:
            continue
        matches = isinstance(reported, _DECLARED_AS[json_type])
        if json_type != "boolean" and isinstance(reported, bool):
            matches = False
        if not matches:
            mismatched.append(f"{key}: declared {json_type}, reported {reported!r}")
    assert mismatched == []

"""Constraint parsing and description filtering against real device data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from custom_components.wattpilot.descriptions import (
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

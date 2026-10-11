"""Readers of the charger's payloads, at their edges."""

from __future__ import annotations

import sys

import pytest

from custom_components.wattpilot.readings import is_number

LARGEST = sys.float_info.max


@pytest.mark.parametrize("value", [0, -1, 1.5, LARGEST, -LARGEST, 10**308])
def test_every_finite_number_is_a_number(value: float) -> None:
    assert is_number(value)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), 10**400, "1", None]
)
def test_what_home_assistant_refuses_is_no_number(value: object) -> None:
    assert not is_number(value)

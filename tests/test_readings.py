"""Readers of the charger's payloads, at their edges."""

from __future__ import annotations

import sys

import pytest

from custom_components.wattpilot.readings import fit_for_state, is_number

LARGEST = sys.float_info.max


@pytest.mark.parametrize("value", [0, -1, 1.5, LARGEST, -LARGEST, 10**308])
def test_every_finite_number_is_a_number(value: float) -> None:
    assert is_number(value)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), 10**400, "1", None]
)
def test_what_home_assistant_refuses_is_no_number(value: object) -> None:
    assert not is_number(value)


def test_a_text_up_to_255_characters_is_a_state_and_one_longer_is_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """255 is Home Assistant's own limit; one more and it logs the text whole."""
    assert fit_for_state("x" * 255, "inverter") == "x" * 255
    assert fit_for_state(12345, "inverter") == 12345
    assert caplog.records == []

    assert fit_for_state("x" * 256, "inverter") is None
    assert [record.getMessage() for record in caplog.records] == [
        "inverter: str of 256 chars is too long for a state"
    ]


def test_a_list_too_long_for_a_state_is_not_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant turns any state into text, a list included."""
    assert fit_for_state(["x" * 300], "networks") is None
    assert [record.getMessage() for record in caplog.records] == [
        "networks: list of 1 is too long for a state"
    ]
    assert fit_for_state(["x" * 10], "networks") == ["x" * 10]

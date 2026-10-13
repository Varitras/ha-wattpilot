"""
Read the charger's payloads: shapes, numbers, prices, durations.

No Home Assistant entity code here; the sensor platform decides what to show.
A charger sends whatever its firmware sends, so every reader expects the
wrong shape as well as the right one.
"""

from __future__ import annotations

import sys
from typing import Any, TypeGuard

from homeassistant.util import dt as dt_util

from .descriptions import MILLISECONDS_PER_MINUTE, NRG_ATTRIBUTES


def describe_shape(value: object) -> str:
    """Name what a value is without repeating what it says."""
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__} of {len(value)}"
    if isinstance(value, dict):
        return f"dict of {len(value)}"
    return type(value).__name__


def describe_with_length(value: object) -> str:
    """describe_shape, with a text's length: for a value that may be free text."""
    if isinstance(value, str):
        return f"str of {len(value)} chars"
    return describe_shape(value)


def namespace_get(value: Any, field: str) -> Any:  # noqa: ANN401 -- dynamically shaped charger payload
    """Read *field* from a dict or a parsed object alike."""
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _price_schedule(value: Any) -> list[dict[str, Any]]:  # noqa: ANN401 -- dynamically shaped charger payload
    """
    Expand a Wattpilot Flex price list into the usual entries.

    The Flex sends one object -- the prices, the first start, the interval --
    whose start and first price match awcp (FW 43.5 diagnostics).
    """
    prices = namespace_get(value, "marketprice")
    start = namespace_get(value, "start")
    interval = namespace_get(value, "interval")
    numbers = all(is_number(x) for x in (start, interval))
    if not (numbers and isinstance(prices, list)):
        return []
    return [
        {
            "start": start + i * interval,
            "end": start + (i + 1) * interval,
            "marketprice": price,
        }
        for i, price in enumerate(prices)
    ]


def price_list(value: Any) -> list[dict[str, Any]]:  # noqa: ANN401 -- dynamically shaped charger payload
    """Return the charger's price list, with its unix times as datetimes."""
    if not isinstance(value, list):
        value = _price_schedule(value)
    return [price for price in map(_price_entry, value) if price is not None]


def _price_entry(entry: Any) -> dict[str, Any] | None:  # noqa: ANN401 -- charger payload
    """One price; None for an entry without usable unix times (RF-D-03)."""
    try:
        return {
            "start": dt_util.utc_from_timestamp(namespace_get(entry, "start")),
            "end": dt_util.utc_from_timestamp(namespace_get(entry, "end")),
            "price": namespace_get(entry, "marketprice"),
        }
    except TypeError, ValueError, OverflowError, OSError:
        return None


def is_number(value: Any) -> TypeGuard[float]:  # noqa: ANN401 -- charger payload
    """Return whether *value* is finite: HA refuses NaN and infinity (PC5-R-01)."""
    # Not math.isfinite: an int past the float range raises there (PC6-01).
    return isinstance(value, (int, float)) and abs(value) <= sys.float_info.max


def number_or_none(value: Any) -> float | None:  # noqa: ANN401 -- charger payload
    """Pass a number or None on; anything else is a malformed push."""
    if value is None or is_number(value):
        return value
    raise TypeError  # the caller logs the shape, never the value


# cdi's "type" as measured on firmware 42.5 (2026-09-25 recordings).
_CHARGING = 0  # value: the boot clock (rbt) when charging began
_STOPPED = 1  # value: the duration in ms


def charging_minutes(cdi: Any, boot_clock: Any) -> int | None:  # noqa: ANN401 -- charger payloads
    """
    Return the whole minutes of the current or last charging session.

    A reboot mid-session makes the start negative, so the subtraction holds
    across it (recorded: -8301127 at rbt 41170, the minutes carried on).
    """
    kind = namespace_get(cdi, "type")
    value = namespace_get(cdi, "value")
    if not is_number(value):
        return None
    if kind == _CHARGING and is_number(boot_clock):
        return _minutes(boot_clock - value)  # each finite, the difference need not be
    if kind == _STOPPED:
        return _minutes(value)
    return None


def _minutes(milliseconds: float) -> int | None:
    return (
        int(milliseconds) // MILLISECONDS_PER_MINUTE
        if is_number(milliseconds)
        else None
    )


# pha: phases L1-L3 switched through to the car, then the supply phases.
_CAR_PHASES = 3


def switched_phase_count(pha: Any) -> int | None:  # noqa: ANN401 -- charger payload
    """Count the phases switched through to the car; None for another shape."""
    if not isinstance(pha, list) or len(pha) < _CAR_PHASES:
        return None
    car_phases = pha[:_CAR_PHASES]
    if not all(isinstance(item, bool) for item in car_phases):
        return None
    return sum(car_phases)


def total_power(nrg: Any) -> float | None:  # noqa: ANN401 -- charger payload
    """Return the total power in an nrg reading, or None without a number there."""
    index = NRG_ATTRIBUTES["TotalPower"]
    if not isinstance(nrg, list) or len(nrg) <= index:
        return None
    reading = nrg[index]
    return reading if is_number(reading) else None

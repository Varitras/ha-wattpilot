"""Shared parity assertion against the frozen fork fixture."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

FIXTURE = Path(__file__).parent / "fixtures" / "fork_parity.json"

# The fork sourced these two sensors from API-client *attributes*
# ("access_state", "car_connected" -- see the fork's SOURCE_ATTRIBUTE
# entries), and its frozen fixture records that attribute name as
# charger_key. Our architecture reads device *properties* only (hub.py is
# the sole owner of the API client -- see the architecture guard), so these
# two read "acs"/"car" instead. The uid (the migration-critical unique_id
# suffix) is unchanged, so existing history still carries over untouched.
_CHARGER_KEY_EXCEPTIONS: dict[str, str] = {
    "access_state": "acs",
    "car_connected": "car",
}

# The fork labels the Awattar max price EUR, but the device stores it in cent
# (vendor schema: "awattarMaxPrice in ct") and we pass it through unscaled, so
# our unit is ct -- otherwise a value of 50 (0.50 EUR) would read as 50 EUR.
# A deliberate, documented deviation from the fork; see the awp description.
_UNIT_EXCEPTIONS: dict[str, str] = {"awp": "ct"}

# Same entity, same reason: the fork declares NumberDeviceClass.MONETARY,
# which Home Assistant documents as requiring an ISO 4217 currency code. "ct"
# is not one, and the currency is not fixed anyway (awc selects the market),
# so the class is dropped rather than declared falsely (audit VA-15).
_DEVICE_CLASS_EXCEPTIONS: dict[str, str | None] = {"awp": None}


def _normalize(value: Any) -> Any:
    return str(value) if value is not None else None


def assert_platform_parity(
    platform: str,
    descriptions: Sequence[Any],
    allowed_extra_uids: set[str] | None = None,
) -> None:
    fork_entries = {
        entry["uid"]: entry
        for entry in json.loads(FIXTURE.read_text(encoding="utf-8"))[platform]
    }
    ours = {description.uid_suffix: description for description in descriptions}
    # Fail immediately if any uid_suffix appears twice in the input list.
    counts = Counter(d.uid_suffix for d in descriptions)
    duplicates = [suffix for suffix, count in counts.items() if count > 1]
    assert not duplicates, f"{platform}: duplicate uid_suffix values: {duplicates}"
    extra = set(ours) - set(fork_entries)
    missing = set(fork_entries) - set(ours)
    assert missing == set(), f"{platform}: fork uids missing (parity broken): {missing}"
    assert extra == (allowed_extra_uids or set()), (
        f"{platform}: unexpected uids: {extra}"
    )
    for uid, fork in fork_entries.items():
        description = ours[uid]
        expected_charger_key = _CHARGER_KEY_EXCEPTIONS.get(uid, fork["charger_key"])
        checks = {
            "charger_key": (description.charger_key, expected_charger_key),
            "unit": (
                getattr(description, "native_unit_of_measurement", None),
                _UNIT_EXCEPTIONS.get(uid, fork["unit"]),
            ),
            "device_class": (
                _normalize(getattr(description, "device_class", None)),
                _DEVICE_CLASS_EXCEPTIONS.get(uid, fork["device_class"])
                if uid in _DEVICE_CLASS_EXCEPTIONS
                else fork["device_class"],
            ),
            "state_class": (
                _normalize(getattr(description, "state_class", None)),
                fork["state_class"],
            ),
            "entity_category": (
                _normalize(getattr(description, "entity_category", None)),
                fork["entity_category"],
            ),
            "enabled_default": (
                description.entity_registry_enabled_default,
                fork["enabled_default"],
            ),
            "firmware": (description.firmware, fork["firmware"]),
            "variant": (description.variant, fork["variant"]),
        }
        for field, (actual, expected) in checks.items():
            assert actual == expected, (
                f"{platform}/{uid}.{field}: ours={actual!r} fork={expected!r}"
            )

"""The frozen parity fixture is the migration promise in data form."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "fork_parity.json"

EXPECTED_COUNTS = {
    # Runtime entity counts: the fork's 10 ID-chip sensors come from one
    # starred generator in source, but expand to 10 fixture entries.
    "sensor": 33,
    "switch": 14,
    "number": 13,
    "select": 8,
    "button": 5,
    "time": 1,
    "update": 1,
}


def load_fork_parity() -> dict[str, list[dict[str, object]]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_counts_match_fork() -> None:
    data = load_fork_parity()
    counts = {platform: len(entries) for platform, entries in data.items()}
    assert counts == EXPECTED_COUNTS


def test_fixture_spot_checks() -> None:
    data = load_fork_parity()
    sensor_uids = {e["uid"] for e in data["sensor"]}
    assert {"wh", "eto", "nrg", "car", "car_connected", "access_state"} <= sensor_uids
    assert {f"cards_{i}" for i in range(10)} <= sensor_uids
    button_uids = [e["uid"] for e in data["button"]]
    assert button_uids == ["frc0", "frc1", "frc2", "rst", "trx"]
    amp = [e for e in data["number"] if e["charger_key"] == "amp"]
    assert {e["uid"] for e in amp} == {"amp", "amp_22kw"}
    assert {e["variant"] for e in amp} == {"11", "22"}

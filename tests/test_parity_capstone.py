"""The uid freeze: every unique_id the deysel fork shipped is still declared.

This compares the description tables against tests/fixtures/fork_parity.json
and nothing else. It is a freeze on what the package *declares*, not a test
of what a running install ends up with -- deleting the registry migration or
breaking unique_id construction leaves it green, and it said otherwise for a
while. What it does cover is drift in the tables themselves, which is where
a lost uid usually starts.

The rest of the promise lives elsewhere, deliberately:
  - tests/test_registry_migration.py -- legacy ids actually being rewritten,
    including the variant-dependent "amp" case the fixture cannot express
    (it only describes the deysel fork).
  - tests/test_e2e_smoke.py -- what a real setup registers on real firmware.
  - tests/test_init.py -- unique_id construction and the entry's identity.

The only additions to the frozen set are the ones this project chose: the
four energy-split sensors (whs/whb/whg/who), the "pnp" phase-count sensor,
the four charging-state sensors (alw/acu/tpa/fhz), and the select platform's
"ct" car profile (mk-maddin parity, absent from the deysel fork). Do not
"fix" a failure by widening ALL's extras below -- find the drift that caused
the mismatch instead.
"""

from __future__ import annotations

from custom_components.wattpilot.descriptions import (
    BUTTON_DESCRIPTIONS,
    NUMBER_DESCRIPTIONS,
    SELECT_DESCRIPTIONS,
    SENSOR_DESCRIPTIONS,
    SWITCH_DESCRIPTIONS,
    TIME_DESCRIPTIONS,
    UPDATE_DESCRIPTIONS,
)

from .parity import assert_platform_parity

ALL = {
    "sensor": (
        SENSOR_DESCRIPTIONS,
        {"whs", "whb", "whg", "who", "pnp", "alw", "acu", "tpa", "fhz"},
    ),
    "switch": (SWITCH_DESCRIPTIONS, set()),
    "number": (NUMBER_DESCRIPTIONS, set()),
    "select": (SELECT_DESCRIPTIONS, {"ct"}),
    "button": (BUTTON_DESCRIPTIONS, set()),
    "time": (TIME_DESCRIPTIONS, set()),
    "update": (UPDATE_DESCRIPTIONS, set()),
}


def test_full_parity() -> None:
    for platform, (descriptions, extras) in ALL.items():
        assert_platform_parity(platform, descriptions, extras)


def test_uid_suffixes_unique_per_platform() -> None:
    for platform, (descriptions, _extras) in ALL.items():
        suffixes = [d.uid_suffix for d in descriptions]
        assert len(suffixes) == len(set(suffixes)), f"duplicate uid in {platform}"

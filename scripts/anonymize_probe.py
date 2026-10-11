"""
Build tests/fixtures/device_properties.json from a local probe dump.

Usage: python scripts/anonymize_probe.py <path-to-wattpilot-probe.json>
The probe dump itself stays outside the repo. Identifying values are
replaced with synthetic ones; log-like and credential keys are dropped.

The scrubbing logic itself lives in custom_components/wattpilot/redaction.py
(diagnostics.py uses the same module) -- this script is a thin CLI wrapper
around it. DROP_KEYS, sanitize_property, sanitize_snapshot and scrub are
re-exported here (see __all__) because the probe script that produces the
dump lives outside this repository and imports them from here by name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from custom_components.wattpilot.redaction import (
    DROP_KEYS,
    sanitize_property,
    sanitize_snapshot,
    scrub,
)

__all__ = [
    "DROP_KEYS",
    "fixture_from",
    "main",
    "sanitize_property",
    "sanitize_snapshot",
    "scrub",
]


def fixture_from(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the snapshot as the committed fixture holds it."""
    cleaned = sanitize_snapshot(snapshot)
    if cleaned.get("ct"):
        # Diagnostics tell a known profile from an unknown one; the suite
        # needs one the car profile select offers (post-check F31-01).
        cleaned["ct"] = "default"
    return cleaned


def main() -> int:
    """Read the probe dump, scrub identifying values, and write the fixture."""
    probe = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    cleaned = fixture_from(probe["snapshot_after_observation"])
    out = Path(__file__).parent.parent / "tests" / "fixtures" / "device_properties.json"
    out.write_bytes(
        (json.dumps(cleaned, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    print(f"wrote {out} ({len(cleaned)} properties)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Print the current complexity table for tests/test_guards.py."""

from __future__ import annotations

from pathlib import Path

from radon.complexity import cc_visit

PACKAGE = Path(__file__).parent.parent / "custom_components" / "wattpilot"
THRESHOLD = 5


def snapshot() -> dict[str, int]:
    """Every function over the threshold, keyed as the ratchet keys it."""
    # rglob, as the ratchet does: glob missed api/, and a table regenerated
    # from it lost all of that package's entries (audit A19-12).
    return {
        f"{path.name}::{block.fullname}": block.complexity
        for path in sorted(PACKAGE.rglob("*.py"))
        for block in cc_visit(path.read_text(encoding="utf-8"))
        if block.complexity > THRESHOLD
    }


if __name__ == "__main__":
    for name, complexity in snapshot().items():
        print(f'        "{name}": {complexity},')

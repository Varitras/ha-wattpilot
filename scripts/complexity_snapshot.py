"""Print the current complexity table for tests/test_guards.py."""

from __future__ import annotations

from pathlib import Path

from radon.complexity import cc_visit

PACKAGE = Path(__file__).parent.parent / "custom_components" / "wattpilot"
THRESHOLD = 5

for path in sorted(PACKAGE.glob("*.py")):
    for block in cc_visit(path.read_text(encoding="utf-8")):
        if block.complexity > THRESHOLD:
            print(f'        "{path.name}::{block.fullname}": {block.complexity},')

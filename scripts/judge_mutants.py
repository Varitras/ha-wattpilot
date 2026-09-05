"""
Judge a `mutmut results` report against the accepted-survivor list.

`mutmut run` exits 0 no matter what it found, so the gate has to read the
report itself. It also reports more outcomes than "killed" and "survived":
"no tests", "not checked", "suspicious" and "timeout" all mean the mutant was
never judged. Filtering for survivors alone let a newly untested mutant
vanish from the comparison entirely while the accepted survivors still
matched, and the gate passed (audit A11-10).

Every outcome therefore gets an explicit verdict here, and anything the
policy does not know is a failure rather than a silence.

Usage: judge_mutants.py <results-file> <accepted-survivors-file>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# `mutmut results` prints one "  <mutant name>: <outcome>" line per mutant.
RESULT_LINE = re.compile(r"^\s*(?P<name>\S.*?): (?P<outcome>[a-z ]+)$")

KILLED = "killed"
SURVIVED = "survived"
# Outcomes that mean the mutant was never actually judged. Each one is a hole
# in the measurement, so the gate refuses rather than reporting a number it
# cannot stand behind.
UNVERIFIED = frozenset({"no tests", "not checked", "suspicious", "timeout"})
# Deliberately excluded from the run, e.g. by a mutmut skip marker.
IGNORED = frozenset({"skipped", "caught by type check"})


def parse(report: str) -> dict[str, list[str]]:
    """Return mutant names grouped by outcome."""
    grouped: dict[str, list[str]] = {}
    for line in report.splitlines():
        match = RESULT_LINE.match(line)
        if match is None:
            continue
        grouped.setdefault(match["outcome"], []).append(match["name"])
    return grouped


def accepted_survivors(text: str) -> set[str]:
    """Read the justification file, ignoring comments and blank lines."""
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def judge(report: str, accepted: str) -> list[str]:
    """Return one complaint per policy violation; empty means the gate passes."""
    grouped = parse(report)
    complaints: list[str] = []

    if not grouped:
        return ["The mutation report named no mutants at all -- nothing was run."]

    unknown = set(grouped) - {KILLED, SURVIVED} - UNVERIFIED - IGNORED
    complaints += [
        f"unknown mutmut outcome {outcome!r} "
        f"({len(grouped[outcome])} mutant(s)) -- decide what it means"
        for outcome in sorted(unknown)
    ]
    complaints += [
        f"{name}: {outcome} -- never judged, so not covered"
        for outcome in sorted(UNVERIFIED & set(grouped))
        for name in sorted(grouped[outcome])
    ]

    survived = set(grouped.get(SURVIVED, []))
    allowed = accepted_survivors(accepted)
    complaints += [
        f"{name}: survived and is not justified -- write a test"
        for name in sorted(survived - allowed)
    ]
    complaints += [
        f"{name}: listed as an accepted survivor but no longer survives "
        "-- remove it from scripts/equivalent-mutants.txt"
        for name in sorted(allowed - survived)
    ]
    return complaints


def main(argv: list[str]) -> int:
    """Print every complaint and return a shell exit code."""
    expected_arguments = 3
    if len(argv) != expected_arguments:
        print(__doc__, file=sys.stderr)
        return 2
    complaints = judge(
        Path(argv[1]).read_text(encoding="utf-8"),
        Path(argv[2]).read_text(encoding="utf-8"),
    )
    for complaint in complaints:
        print(complaint)
    return 1 if complaints else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""
Print the changelog section for one version, for the release workflow to use.

The release body is written by hand in CHANGELOG.md and shipped from there,
rather than generated from commit messages. Commit subjects say what a change
was; a release page has to say what it means for someone installing it, and
the two are not the same text. Keeping one source also means the release page
and the changelog cannot drift apart.

Usage: release_notes.py 0.1.0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).parent.parent / "CHANGELOG.md"
SECTION = "## "


def section_for(changelog: str, version: str) -> str:
    """
    Return the body of the section for *version*, without its heading.

    Raises LookupError if there is no such section, or if it is empty: a
    release with no notes is a mistake worth stopping for, not a blank page.
    """
    heading = re.compile(rf"^## \[{re.escape(version)}\](?: - .*)?$")
    lines = changelog.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        msg = f"CHANGELOG.md has no section for {version}"
        raise LookupError(msg)

    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(SECTION)),
        len(lines),
    )
    body = "\n".join(lines[start + 1 : end]).strip()
    if not body:
        msg = f"the CHANGELOG.md section for {version} is empty"
        raise LookupError(msg)
    return body


def main(argv: list[str]) -> int:
    """Print the section, or explain why there is none."""
    expected_arguments = 2
    if len(argv) != expected_arguments:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(section_for(CHANGELOG.read_text(encoding="utf-8"), argv[1]))
    except LookupError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

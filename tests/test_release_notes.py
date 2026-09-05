"""The release body comes out of CHANGELOG.md, so that extraction is tested.

A release runs once per version and only from a tag push. If the extraction
is wrong, the mistake is visible on a published release page and the tag has
to be deleted to fix it -- which is the kind of path that has to be exercised
before it runs for real, not after.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_notes import CHANGELOG, section_for

MANIFEST = (
    Path(__file__).parent.parent / "custom_components" / "wattpilot" / "manifest.json"
)

SAMPLE = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-10-01

### Added

- Something new.

## [0.1.0] - 2026-09-05

### Fixed

- Something old.

[0.1.0]: https://example.invalid/tag/v0.1.0
"""


def test_the_section_stops_at_the_next_version() -> None:
    notes = section_for(SAMPLE, "0.2.0")
    assert "Something new." in notes
    assert "Something old." not in notes
    assert "0.1.0" not in notes


def test_the_version_heading_is_not_part_of_the_body() -> None:
    """The release page already shows the version above the body. The body
    does start with a heading -- `### Added` -- so this checks for the
    version line itself, not for any heading."""
    assert "[0.2.0]" not in section_for(SAMPLE, "0.2.0")
    assert section_for(SAMPLE, "0.2.0").startswith("### Added")


def test_the_last_section_runs_to_the_end_of_the_file() -> None:
    notes = section_for(SAMPLE, "0.1.0")
    assert "Something old." in notes
    assert "https://example.invalid/tag/v0.1.0" in notes


def test_an_unknown_version_is_an_error_not_an_empty_release() -> None:
    with pytest.raises(LookupError, match=r"no section for 9\.9\.9"):
        section_for(SAMPLE, "9.9.9")


def test_an_empty_section_is_an_error_too() -> None:
    """`## [Unreleased]` carries nothing between releases. Shipping it as a
    release body would publish a blank page instead of failing the job."""
    with pytest.raises(LookupError, match="is empty"):
        section_for(SAMPLE, "Unreleased")


def test_the_real_changelog_has_notes_for_the_declared_version() -> None:
    """The version the manifest declares must be releasable right now."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    notes = section_for(CHANGELOG.read_text(encoding="utf-8"), manifest["version"])
    assert len(notes) > 200, "the release notes are suspiciously short"

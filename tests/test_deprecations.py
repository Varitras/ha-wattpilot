"""Guards: Home Assistant's reports about this integration fail the suite."""

from __future__ import annotations

import logging
import warnings

import pytest
from homeassistant.helpers.deprecation import deprecated_function
from homeassistant.helpers.frame import report_usage

from . import deprecations
from .deprecations import INTEGRATION, REPORTS, import_every_module

# Where Home Assistant looks for the integration behind a call: the frame of
# a file under custom_components/<domain>/.
CALLER_FILE = str(deprecations.PACKAGE / "sensor.py")


def _call_from_the_integration(source: str, **names: object) -> None:
    """Run source as if it were a line of the integration's own code."""
    code = compile(source, CALLER_FILE, "exec")
    exec(code, names)  # noqa: S102 -- the frame Home Assistant attributes reports by


def test_the_deprecation_check_recognises_home_assistants_reports() -> None:
    """
    Both ways Home Assistant reports a deprecated use, produced by Home
    Assistant itself: if it rewords one, this fails rather than the check
    going blind.
    """
    replaced = deprecated_function("its replacement", breaks_in_ha_version="2099.1")(
        lambda: None
    )
    _call_from_the_integration("replaced()", replaced=replaced)
    _call_from_the_integration(
        "report_usage('calls something that goes away', breaks_in_ha_version='2099.1')",
        report_usage=report_usage,
    )

    reported = [record.getMessage() for record in REPORTS.take()]
    assert len(reported) == 2, reported
    assert reported[0].startswith("The deprecated function"), reported
    assert reported[1].startswith("Detected that custom integration"), reported


def test_the_deprecation_check_lets_other_reports_pass() -> None:
    """Counter-check: other integrations' reports and ordinary warnings."""
    for name, message in [
        ("homeassistant.helpers.frame", "Detected that custom integration 'other' x"),
        ("homeassistant.helpers.deprecation", "The deprecated y was used from other."),
        (f"custom_components.{INTEGRATION}.hub", "Charger unavailable"),
    ]:
        logging.getLogger(name).warning(message)
    assert REPORTS.take() == []


def test_importing_the_integration_reports_no_deprecation() -> None:
    """
    A deprecated constant reports when a module imports it, which happens
    while the tests are collected, before any test runs. The check is
    installed before that (tests/__init__.py) and keeps what it saw.
    """
    import_every_module()
    reported = REPORTS.unattributed + REPORTS.take()
    assert reported == [], [record.getMessage() for record in reported]


@pytest.mark.parametrize("category", [DeprecationWarning, PendingDeprecationWarning])
def test_a_deprecation_warning_in_the_integration_fails(
    category: type[Warning],
) -> None:
    """pyproject.toml turns them into errors, for this package only."""
    with pytest.raises(category):
        warnings.warn_explicit(
            "old", category, CALLER_FILE, 1, module=f"custom_components.{INTEGRATION}.x"
        )
    with warnings.catch_warnings(record=True) as elsewhere:
        warnings.warn_explicit("old", category, "other.py", 1, module="other")
    assert len(elsewhere) == 1

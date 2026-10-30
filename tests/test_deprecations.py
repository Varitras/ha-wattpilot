"""Guards: Home Assistant's reports about this integration fail the suite."""

from __future__ import annotations

import ast
import importlib
import logging
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.helpers.deprecation import deprecated_function, get_deprecated
from homeassistant.helpers.frame import ReportBehavior, report_usage

from scripts import import_check

from . import conftest
from .deprecations import INTEGRATION, REPORTS, import_every_module

# Where Home Assistant looks for the integration behind a call: the frame of
# a file under custom_components/<domain>/. The module's own file name, as
# get_deprecated finds the caller's module by it.
CALLER_FILE = str(
    importlib.import_module(f"custom_components.{INTEGRATION}.sensor").__file__
)
# Code outside every integration, as a test helper is: Home Assistant then
# reports without naming anyone.
ELSEWHERE_FILE = str(Path(__file__).parent / "helper.py")


def _call_from(file_name: str, source: str, **names: object) -> None:
    """Run source as if it were a line of that file."""
    code = compile(source, file_name, "exec")
    exec(code, names)  # noqa: S102 -- the frame Home Assistant attributes reports by


def _replaced() -> object:
    return deprecated_function("its replacement", breaks_in_ha_version="2099.1")(
        lambda: None
    )


# Each probe is caught by one rule only: a probe two rules catch leaves one
# of them untested.
REPORT_PROBES = {
    "deprecated, from the integration": (
        CALLER_FILE,
        "replaced()",
        "The deprecated function",
    ),
    "misuse, from the integration": (
        CALLER_FILE,
        "report_usage('calls something that goes away', breaks_in_ha_version='2099.1')",
        "Detected that custom integration",
    ),
    "deprecated, from outside any integration": (
        ELSEWHERE_FILE,
        "replaced()",
        "The deprecated function",
    ),
    "misuse, from outside any integration": (
        ELSEWHERE_FILE,
        "report_usage('calls something that goes away', core_behavior=LOG)",
        "Detected code that",
    ),
    "renamed configuration key, read by the integration": (
        CALLER_FILE,
        "get_deprecated({'old': 1}, 'new', 'old')",
        "'old' is deprecated",
    ),
}


@pytest.mark.parametrize("probe", REPORT_PROBES)
def test_the_deprecation_check_recognises_home_assistants_reports(probe: str) -> None:
    """
    Every wording Home Assistant reports in, produced by Home Assistant
    itself: if it rewords one, this fails rather than the check going blind.
    """
    file_name, source, opening = REPORT_PROBES[probe]
    _call_from(
        file_name,
        source,
        replaced=_replaced(),
        report_usage=report_usage,
        get_deprecated=get_deprecated,
        LOG=ReportBehavior.LOG,
    )

    reported = [record.getMessage() for record in REPORTS.take()]
    assert len(reported) == 1, reported
    assert reported[0].startswith(opening), reported


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


def _a_report() -> None:
    logging.getLogger("homeassistant.helpers.frame").warning(
        "Detected that custom integration '%s' does a thing", INTEGRATION
    )


def test_a_report_during_a_test_fails_it() -> None:
    """What the autouse fixture runs after every test."""
    _a_report()
    with pytest.raises(AssertionError, match="does a thing"):
        REPORTS.check()
    REPORTS.check()


def test_a_report_outside_a_test_fails_the_run() -> None:
    """
    One logged while the tests are collected belongs to no test. It is kept,
    and fails the run whichever tests were selected.
    """
    before = len(REPORTS.unattributed)
    _a_report()
    REPORTS.set_aside()
    REPORTS.check()
    session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)
    try:
        conftest.pytest_sessionfinish(session)  # type: ignore[arg-type]
    finally:
        del REPORTS.unattributed[before:]
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_the_import_check_fails_on_a_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(import_check, "import_every_module", _a_report)
    assert import_check.main() == 1
    monkeypatch.setattr(import_check, "import_every_module", lambda: None)
    assert import_check.main() == 0


def test_no_test_hides_warnings_from_the_check() -> None:
    """
    caplog.set_level without a logger raises the root logger's level, and a
    WARNING from Home Assistant then never reaches the check for that test.
    """
    hiding = {"ERROR", "CRITICAL"}
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in sorted(Path(__file__).parent.glob("test_*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"set_level", "at_level"}
        and not any(keyword.arg == "logger" for keyword in node.keywords)
        and node.args
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr in hiding
    ]
    assert offenders == [], offenders


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

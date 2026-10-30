"""
Home Assistant's reports about this integration, collected to fail the tests.

Home Assistant reports a deprecated call by logging a warning and carrying
on, so a suite stays green on code that the release named in the warning
breaks for every user at once. The static list in test_guards.py only knows
the calls someone already entered; this catches the report wordings
test_deprecations.py produces from Home Assistant itself, on the paths the
tests run.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
import re
from pathlib import Path

# The fixed path, as every other test uses: a dev copy beside it
# (scripts/make_dev_copy.py) must not become the package watched here.
PACKAGE = Path(__file__).parents[1] / "custom_components" / "wattpilot"
# Read from the manifest, not imported: importing the integration is what
# this has to watch.
INTEGRATION = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))[
    "domain"
]
_OWN_LOGGER = f"custom_components.{INTEGRATION}"

# frame.report_usage, for deprecated calls and for misuse such as calling
# into the loop from a thread, when the call comes from the integration.
_FROM_US = re.compile(rf"Detected that custom integration '{re.escape(INTEGRATION)}' ")
# The same two helpers when the caller is in no integration at all. In this
# suite that is the tests or something they drive, never another
# integration, so it counts too.
_FROM_NOBODY = re.compile(r"Detected code that |The deprecated \S+ \S+ was \w+\.")
# helpers.deprecation names the integration ("... was used from <domain>.")
# or its file ("... in '<path>'"), and get_deprecated logs under the caller's
# module, so a report about this integration may come from its own logger.
_NAMED = re.compile(
    rf"\b{re.escape(INTEGRATION)}\b|custom_components[./\\]{re.escape(INTEGRATION)}\b"
)
_DEPRECATED = re.compile("deprecat", re.IGNORECASE)


def is_report(record: logging.LogRecord) -> bool:
    """Whether Home Assistant reports something this integration does."""
    if record.levelno < logging.WARNING:
        return False
    message = record.getMessage()
    if _FROM_US.search(message) or _FROM_NOBODY.match(message):
        return True
    named = _NAMED.search(message) or record.name.startswith(_OWN_LOGGER)
    return bool(named and _DEPRECATED.search(message))


class Reports(logging.Handler):
    """The reports since the last take(); the ones outside any test, kept."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self._pending: list[logging.LogRecord] = []
        self.unattributed: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if is_report(record):
            self._pending.append(record)

    def take(self) -> list[logging.LogRecord]:
        taken, self._pending = self._pending, []
        return taken

    def set_aside(self) -> None:
        """Keep what arrived outside a test for the end of the run."""
        self.unattributed += self.take()

    def check(self) -> None:
        """Fail on what arrived since the last take()."""
        reported = [record.getMessage() for record in self.take()]
        assert not reported, f"Home Assistant reported: {reported[:3]}"


REPORTS = Reports()


def import_every_module() -> None:
    """Import the integration and every module in it, as Home Assistant may."""
    package = importlib.import_module(f"custom_components.{INTEGRATION}")
    for module in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
        importlib.import_module(module.name)

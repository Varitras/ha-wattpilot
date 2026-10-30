"""
Runs before conftest.py imports the integration.

The deprecation check listens from here: a deprecated constant is reported
the moment a module imports it, during collection (tests/deprecations.py).
"""

import logging

from .deprecations import REPORTS

logging.getLogger().addHandler(REPORTS)

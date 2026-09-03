"""
The Wattpilot API client.

Adopted from wattpilot-api 1.4.0 (MIT, see LICENSE) after upstream went
quiet, and maintained here since: the findings that mattered most -- writes
reported as success without an acknowledgement, raw frames logged with
credentials -- could only be fixed inside it. Only the modules this
integration uses were taken over; mqtt, shell and discovery were not, and
with them neither aiomqtt, prompt-toolkit nor pydantic.

Changes against 1.4.0 carry a `wattpilot:` comment naming the finding, so
the adopted parts stay distinguishable from ours.
"""

from .client import Wattpilot
from .exceptions import AuthenticationError, WattpilotError
from .models import CloudInfo

__all__ = ["AuthenticationError", "CloudInfo", "Wattpilot", "WattpilotError"]

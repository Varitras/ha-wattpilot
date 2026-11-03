"""
The Wattpilot API client.

Adopted from wattpilot-api 1.4.0 (MIT, see LICENSE) after upstream went
quiet, and maintained here since: the findings that mattered most -- writes
reported as success without an acknowledgement, raw frames logged with
credentials -- could only be fixed inside it. Only the modules this
integration uses were taken over; mqtt, shell and discovery were not, and
with them neither aiomqtt, prompt-toolkit nor pydantic.

It has been changed throughout since, so the changes are no longer marked
one by one.
"""

from .client import Wattpilot, mask_serial
from .exceptions import (
    AuthenticationError,
    DeviceIdentityError,
    WattpilotError,
    describe_error,
)
from .models import CloudInfo

__all__ = [
    "AuthenticationError",
    "CloudInfo",
    "DeviceIdentityError",
    "Wattpilot",
    "WattpilotError",
    "describe_error",
    "mask_serial",
]

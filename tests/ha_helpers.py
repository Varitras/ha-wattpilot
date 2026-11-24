"""
Home Assistant's test helpers, from whichever test plugin is installed.

Home Assistant ships its own, pytest-homeassistant, from 2026.11 on. The one
it replaced ended with 2026.10 and stays for the declared minimum; drop the
fallback once hacs.json reaches 2026.11.
"""

try:
    from pytest_homeassistant.common import (
        MockConfigEntry,
        async_fire_time_changed,
        mock_restore_cache_with_extra_data,
    )
except ImportError:
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_time_changed,
        mock_restore_cache_with_extra_data,
    )

__all__ = [
    "MockConfigEntry",
    "async_fire_time_changed",
    "mock_restore_cache_with_extra_data",
]

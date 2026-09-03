"""Update entity: versions from onv/fwv, install via hub."""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.wattpilot.descriptions import UPDATE_DESCRIPTIONS
from custom_components.wattpilot.hub import WattpilotHub
from custom_components.wattpilot.update import WattpilotUpdate

from .parity import assert_platform_parity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

ENTRY_ID = "entry1"


async def make_update(hass: HomeAssistant, charger: FakeWattpilot) -> WattpilotUpdate:
    hub = WattpilotHub(hass, ENTRY_ID, charger)  # type: ignore[arg-type]
    await hub.async_connect()
    hub.start_dispatch()  # required: pushes route through the hub's dispatch
    entity = WattpilotUpdate(hub, ENTRY_ID, UPDATE_DESCRIPTIONS[0])
    entity.hass = hass
    entity.entity_id = "update.test_firmware"
    await entity.async_added_to_hass()
    return entity


def test_update_parity_with_fork() -> None:
    assert_platform_parity("update", UPDATE_DESCRIPTIONS)


async def test_versions_from_properties(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["fwv"] = "42.5"
    fake_charger._properties["onv"] = "43.0"
    entity = await make_update(hass, fake_charger)
    assert entity.installed_version == "42.5"
    assert entity.latest_version == "43.0"
    assert isinstance(entity.latest_version, str)


async def test_version_list_uses_highest(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["fwv"] = "42.5"
    fake_charger._properties["onv"] = ["43.0", "42.9"]
    entity = await make_update(hass, fake_charger)
    assert entity.latest_version == "43.0"


async def test_version_list_skips_unparsable_entries(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Unparsable entries are dropped from ranking, not a crash (see
    _newest_version's InvalidVersion handling in update.py)."""
    fake_charger._properties["fwv"] = "42.5"
    fake_charger._properties["onv"] = ["42.9", "not-a-version", "43.0"]
    entity = await make_update(hass, fake_charger)
    assert entity.latest_version == "43.0"


async def test_all_unparsable_versions_use_wire_order_fallback(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """When nothing parses, _newest_version falls back to raw_versions[0]
    instead of going blank (see update.py)."""
    fake_charger._properties["fwv"] = "42.5"
    fake_charger._properties["onv"] = ["alpha", "beta"]
    entity = await make_update(hass, fake_charger)
    assert entity.latest_version == "alpha"


async def test_empty_version_list_falls_back_to_installed(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["fwv"] = "42.5"
    fake_charger._properties["onv"] = []
    entity = await make_update(hass, fake_charger)
    assert entity.latest_version == "42.5"


async def test_unordered_version_list_display_and_install_agree(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """onv order on the wire is not guaranteed (see descriptions.py); the
    displayed latest_version and the version async_install(None) sends must
    still agree with each other regardless of wire order."""
    fake_charger._properties["fwv"] = "42.5"
    fake_charger._properties["onv"] = ["42.9", "43.0", "42.5"]
    entity = await make_update(hass, fake_charger)
    assert entity.latest_version == "43.0"
    await entity.async_install(version=None, backup=False)
    assert fake_charger.install_calls == ["43.0"]


async def test_install_calls_hub(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    fake_charger._properties["fwv"] = "42.5"
    fake_charger._properties["onv"] = "43.0"
    entity = await make_update(hass, fake_charger)
    await entity.async_install(version=None, backup=False)
    assert fake_charger.install_calls == ["43.0"]


async def test_push_updates_latest_version(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Cover the live-push path: start_dispatch() must actually be exercised
    by a push, not just called (see test_number.py's test_push_updates_native_value)."""
    entity = await make_update(hass, fake_charger)
    fake_charger.push("onv", "99.0")
    assert entity.latest_version == "99.0"


async def test_installed_version_reacts_to_its_own_push(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """
    installed_version comes from fwv, which moves on its own after an install
    completes -- with no onv push to carry it. The base entity subscribes only
    to onv, so without a dedicated fwv subscription the installed version would
    stay frozen at whatever it was when onv last changed.
    """
    fake_charger._properties["fwv"] = "42.5"
    fake_charger._properties["onv"] = "43.0"
    entity = await make_update(hass, fake_charger)
    assert entity.installed_version == "42.5"

    fake_charger.push("fwv", "43.0")
    assert entity.installed_version == "43.0"

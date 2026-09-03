"""mk-maddin installs keep history: legacy unique_ids are rewritten once."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattpilot import registry_migration
from custom_components.wattpilot.const import DOMAIN

from .conftest import FakeWattpilot
from .test_init import V2_LOCAL_DATA, setup_entry

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant


def seed(
    hass: HomeAssistant, entry: MockConfigEntry, platform: str, unique_id: str
) -> er.RegistryEntry:
    registry = er.async_get(hass)
    return registry.async_get_or_create(platform, DOMAIN, unique_id, config_entry=entry)


async def test_legacy_prefix_and_suffix_rewritten(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=2)
    entry.add_to_hass(hass)
    # mk-maddin style: friendly-name prefix (with a dash!) + CamelCase suffix.
    seeded_amp = seed(hass, entry, "number", "Garage-Box-amp")
    seeded_access = seed(hass, entry, "sensor", "Garage-Box-AccessState")
    assert await setup_entry(hass, entry, fake_charger)
    registry = er.async_get(hass)
    assert registry.async_get(seeded_amp.entity_id).unique_id == "123456-amp"
    assert (
        registry.async_get(seeded_access.entity_id).unique_id == "123456-access_state"
    )


async def test_deysel_style_ids_untouched_and_collision_skipped(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=2)
    entry.add_to_hass(hass)
    correct = seed(hass, entry, "number", "123456-amp")
    legacy_colliding = seed(hass, entry, "number", "Wattpilot-amp")
    assert await setup_entry(hass, entry, fake_charger)
    registry = er.async_get(hass)
    assert registry.async_get(correct.entity_id).unique_id == "123456-amp"
    # Target already exists -> legacy entry stays (skipped, not crashed).
    assert registry.async_get(legacy_colliding.entity_id).unique_id == "Wattpilot-amp"


async def test_legacy_amp_follows_the_charger_to_the_22kw_uid(
    hass: HomeAssistant, device_properties: dict[str, Any]
) -> None:
    """
    mk-maddin keyed max charging current "amp" on both hardware variants.

    We split it: the 11 kW charger gets uid "amp", the 22 kW one "amp_22kw"
    (the deysel fork's spelling, frozen in the parity fixture). Rewriting a
    22 kW install's legacy entity to "<serial>-amp" therefore aims it at a
    uid no entity on that device ever claims -- Home Assistant then creates
    "<serial>-amp_22kw" from scratch, and the migrated entity is left behind
    with all its history and dashboard references. Every mk-maddin user with
    a 22 kW charger loses exactly one entity's past, silently.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=2)
    entry.add_to_hass(hass)
    seeded = seed(hass, entry, "number", "Garage-Box-amp")
    charger = FakeWattpilot(device_properties)
    charger.variant = 22  # int, like the real device reports it
    assert await setup_entry(hass, entry, charger)
    registry = er.async_get(hass)
    assert registry.async_get(seeded.entity_id).unique_id == "123456-amp_22kw"
    # And the entity that claims it is the one the platform built, not a
    # second one created alongside the orphan.
    assert (
        len(
            [
                item
                for item in er.async_entries_for_config_entry(registry, entry.entry_id)
                if item.unique_id.endswith("-amp_22kw")
            ]
        )
        == 1
    )


async def test_cross_domain_same_suffix_both_migrate(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The uid_suffix "trx" is legitimate on both sensor (id_chip_current)
    and button (authenticate) -- same suffix, different domains, both real.
    The collision set is scoped per domain, so both migrate independently
    instead of the second being skipped as a false collision.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=2)
    entry.add_to_hass(hass)
    seeded_sensor = seed(hass, entry, "sensor", "Garage-trx")
    seeded_button = seed(hass, entry, "button", "Garage-trx")
    assert await setup_entry(hass, entry, fake_charger)
    registry = er.async_get(hass)
    assert registry.async_get(seeded_sensor.entity_id).unique_id == "123456-trx"
    assert registry.async_get(seeded_button.entity_id).unique_id == "123456-trx"


async def test_unrelated_unique_ids_untouched(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=2)
    entry.add_to_hass(hass)
    odd = seed(hass, entry, "sensor", "something-entirely-different")
    assert await setup_entry(hass, entry, fake_charger)
    registry = er.async_get(hass)
    assert registry.async_get(odd.entity_id).unique_id == "something-entirely-different"


async def test_longest_suffix_wins_over_shorter_shadow(
    hass: HomeAssistant,
    fake_charger: FakeWattpilot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short suffix must never shadow a longer one that ends with it.

    No pair in today's real suffix table collides like this: matching
    always requires a literal "-" right before the suffix, and none of our
    real uid suffixes contain one (verified separately, not asserted here).
    That makes the ordering guarantee unfalsifiable against production data
    today -- so it is proven directly against the matching table instead,
    with a synthetic pair standing in for a future suffix that could.
    """
    monkeypatch.setattr(
        registry_migration,
        "_known_suffixes",
        lambda _variant: {"amp": "SHORT_WRONG", "box-amp": "LONG_RIGHT"},
    )
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=2)
    entry.add_to_hass(hass)
    seeded = seed(hass, entry, "number", "Garage-box-amp")
    assert await setup_entry(hass, entry, fake_charger)
    registry = er.async_get(hass)
    assert registry.async_get(seeded.entity_id).unique_id == "123456-LONG_RIGHT"


async def test_a_skipped_collision_raises_a_repair_issue(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """Audit VA-06: skipping the legacy entity leaves exactly the one that
    carries the history, entity id and customisations orphaned, while the
    integration binds the entity that was already there. Nothing destroys
    data here, and nothing should -- which of the two is worth keeping is
    the owner's call, not ours. But it must not stay a log line either: a
    repair issue names the affected entities and asks.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=2)
    entry.add_to_hass(hass)
    seed(hass, entry, "number", "123456-amp")
    legacy = seed(hass, entry, "number", "Wattpilot-amp")
    assert await setup_entry(hass, entry, fake_charger)

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"migration_collision_{entry.entry_id}"
    )
    assert issue is not None, "a silently orphaned entity must surface as a repair"
    assert legacy.entity_id in issue.translation_placeholders["entities"]


async def test_no_repair_issue_without_a_collision(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """The counter-check: an ordinary install must not be nagged."""
    entry = MockConfigEntry(domain=DOMAIN, data=V2_LOCAL_DATA, version=2)
    entry.add_to_hass(hass)
    seed(hass, entry, "number", "Wattpilot-amp")
    assert await setup_entry(hass, entry, fake_charger)

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"migration_collision_{entry.entry_id}"
        )
        is None
    )

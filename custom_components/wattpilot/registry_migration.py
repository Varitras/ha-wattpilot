"""
One-time entity-registry unique_id migration for legacy installs.

mk-maddin/Wattpilot-HA built unique_ids as `<friendly_name|ip>-<uid>` and
used two CamelCase suffixes. We use `<serial>-<uid>`. Rewriting the registry
entry preserves entity_id, customizations and recorder history.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .descriptions import (
    BUTTON_DESCRIPTIONS,
    NUMBER_DESCRIPTIONS,
    SELECT_DESCRIPTIONS,
    SENSOR_DESCRIPTIONS,
    SWITCH_DESCRIPTIONS,
    TIME_DESCRIPTIONS,
    UPDATE_DESCRIPTIONS,
    VARIANT_22,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Legacy (mk-maddin) suffix -> our suffix. Everything else maps 1:1.
_SUFFIX_ALIASES = {
    "AccessState": "access_state",
    "carConnected": "car_connected",
}

# mk-maddin exposed max charging current as one entity keyed "amp" on both
# hardware variants. We split it, and the 22 kW half carries the deysel
# fork's spelling "amp_22kw" (frozen in the parity fixture, so it cannot be
# renamed). On a 22 kW charger the legacy "amp" therefore has to land on
# "amp_22kw": the plain "amp" uid belongs to a description that device never
# creates, and aiming at it strands the entity behind a freshly built one.
_VARIANT_22_SUFFIXES = {"amp": "amp_22kw"}


def _known_suffixes(variant: object) -> dict[str, str]:
    """Return every accepted legacy suffix mapped to our uid suffix."""
    ours = {
        description.uid_suffix: description.uid_suffix
        for table in (
            SENSOR_DESCRIPTIONS,
            SWITCH_DESCRIPTIONS,
            NUMBER_DESCRIPTIONS,
            SELECT_DESCRIPTIONS,
            BUTTON_DESCRIPTIONS,
            TIME_DESCRIPTIONS,
            UPDATE_DESCRIPTIONS,
        )
        for description in table
    }
    variant_specific = _VARIANT_22_SUFFIXES if str(variant) == VARIANT_22 else {}
    return ours | _SUFFIX_ALIASES | variant_specific


def _match_legacy_suffix(unique_id: str, suffix_map: dict[str, str]) -> str | None:
    """
    Return the longest known suffix unique_id ends with, or None.

    Longest-first matching means a short suffix (e.g. "amp") can never
    shadow a longer one that ends with it (e.g. a hypothetical "box-amp"):
    see test_longest_suffix_wins_over_shorter_shadow.
    """
    for legacy_suffix in sorted(suffix_map, key=len, reverse=True):
        if unique_id.endswith(f"-{legacy_suffix}"):
            return legacy_suffix
    return None


async def async_migrate_legacy_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry, serial: str, variant: object
) -> None:
    """
    Rewrite `<anything>-<known suffix>` to `<serial>-<our suffix>`.

    Entity registry uniqueness is per (domain, platform, unique_id), not
    unique_id alone -- e.g. "trx" is a legitimate uid_suffix on both the
    sensor and button platforms. The collision set must stay scoped by
    domain, or a legitimate second entity is skipped as a false collision
    and orphaned behind a freshly created registry entry.
    """
    suffix_map = _known_suffixes(variant)
    collisions: list[str] = []
    registry = er.async_get(hass)
    existing = {
        (item.domain, item.unique_id)
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }

    @callback
    def _migrate(entity_entry: er.RegistryEntry) -> dict[str, str] | None:
        unique_id = entity_entry.unique_id
        legacy_suffix = _match_legacy_suffix(unique_id, suffix_map)
        if legacy_suffix is None:
            return None
        target = f"{serial}-{suffix_map[legacy_suffix]}"
        if unique_id == target:
            return None  # already correct
        collision_key = (entity_entry.domain, target)
        if collision_key in existing:
            # Deliberately not resolved here: the skipped entity is the one
            # holding the history, entity id and customisations, and the one
            # kept is the entity already bound. Which of the two is worth
            # keeping is the owner's call, so nothing is deleted or renamed
            # -- but it is reported instead of only logged (audit VA-06).
            collisions.append(entity_entry.entity_id)
            _LOGGER.warning(
                "Not migrating %s (%s): target unique_id %s already exists",
                entity_entry.entity_id,
                unique_id,
                target,
            )
            return None
        existing.add(collision_key)
        _LOGGER.info(
            "Migrating unique_id %s -> %s for %s",
            unique_id,
            target,
            entity_entry.entity_id,
        )
        return {"new_unique_id": target}

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)
    _report_collisions(hass, entry, collisions)


def _report_collisions(
    hass: HomeAssistant, entry: ConfigEntry, collisions: list[str]
) -> None:
    """Raise (or clear) the repair issue for entities left unmigrated."""
    issue_id = f"migration_collision_{entry.entry_id}"
    if not collisions:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="migration_collision",
        translation_placeholders={"entities": ", ".join(sorted(collisions))},
    )

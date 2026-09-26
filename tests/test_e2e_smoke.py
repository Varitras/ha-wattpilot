"""Full-setup smoke test against the real-device fixture (slow).

Every other test builds its entities by hand, so nothing else checks the
inventory a real install actually ends up with. This one runs the whole
setup against the anonymized firmware-42.5 property dump and pins the
entity count and the firmware/variant gating.

(The push path -- charger -> hub -> entity state -- is covered by
test_init.test_setup_wires_credentials_and_pushes_end_to_end, in the fast
lane where it belongs.)
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattpilot.const import DOMAIN
from custom_components.wattpilot.descriptions import SENSOR_DESCRIPTIONS

from .test_init import V2_LOCAL_DATA, setup_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]

SERIAL = "123456"


async def test_full_setup_registers_expected_entities(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id=SERIAL
    )
    assert await setup_entry(hass, entry, fake_charger)

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    unique_ids = {entity.unique_id for entity in entities}

    # 92 descriptions ship in total (75 fork uids + the 4 energy-split
    # sensors + pnp, alw, acu, tpa, fhz, awcp + the ct car profile + the frc
    # and preset selects + cco, dwo + the two car binary sensors). This
    # fixture reports firmware 42.5 on an 11 kW charger, which gates away
    # exactly the five asserted below.
    assert len(entities) == 87
    assert {
        f"{SERIAL}-wh",
        f"{SERIAL}-whs",
        f"{SERIAL}-ct",
        f"{SERIAL}-pnp",
    } <= unique_ids
    # <=40.7 only: the webserver diagnostics trio.
    # variant 22 only: the 22 kW charger's wider current range.
    for gated in ("qsw", "wcch", "wccw", "amp_22kw"):
        assert f"{SERIAL}-{gated}" not in unique_ids
    assert f"{SERIAL}-amp" in unique_ids
    # The fifth: "bac" is a switch below firmware 38.5 and a select from 38.5
    # on. Both descriptions carry the same uid_suffix, so only the domain
    # tells them apart -- 42.5 must land on the select.
    by_unique_id = {entity.unique_id: entity for entity in entities}
    assert by_unique_id[f"{SERIAL}-bac"].domain == "select"


async def test_the_uptime_reads_in_hours(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    """rbt is milliseconds since boot: 2887124198 said nothing at a glance.
    The charger's unit stays; Home Assistant converts a duration sensor to
    the suggested unit when it registers the entity."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id=SERIAL
    )
    fake_charger._properties["rbt"] = 2887124198
    # Disabled by default; enabled here so it gets a state at all.
    enabled = tuple(
        replace(d, entity_registry_enabled_default=True)
        if d.charger_key == "rbt"
        else d
        for d in SENSOR_DESCRIPTIONS
    )
    with patch("custom_components.wattpilot.sensor.SENSOR_DESCRIPTIONS", enabled):
        assert await setup_entry(hass, entry, fake_charger)

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}-rbt"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["unit_of_measurement"] == "h"
    assert float(state.state) == pytest.approx(2887124198 / 3_600_000, abs=0.01)

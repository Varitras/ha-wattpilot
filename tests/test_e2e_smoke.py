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

from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattpilot.const import DOMAIN

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

    # 85 descriptions ship in total (75 fork uids + the 4 energy-split
    # sensors + pnp, alw, acu, tpa, fhz + the ct car profile). This fixture
    # reports firmware 42.5 on an 11 kW charger, which gates away exactly the
    # five asserted below.
    assert len(entities) == 80
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

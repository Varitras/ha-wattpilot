"""Diagnostics are tested against the shape the real client produces.

The redaction fixtures are plain JSON, but the client rebuilds every nested
object as a SimpleNamespace. `scrub` walks dictionaries, lists and strings,
so the namespaces went past it untouched and Home Assistant's exporter wrote
their repr() into the download -- IPs, SSIDs and MAC addresses from `dns`,
`wifis` and `scan` (audit A11-03). Dictionary-only fixtures cannot see that
boundary, so this module drives frames through the client instead.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.helpers.json import ExtendedJSONEncoder

from custom_components.wattpilot.api.client import Wattpilot
from custom_components.wattpilot.redaction import (
    UNEXPECTED_SHAPE,
    sanitize_snapshot,
)

# A status frame in the shape the charger really sends: the identifying data
# sits inside nested objects and lists, not at the top level.
STATUS_FRAME = {
    "type": "fullStatus",
    "partial": False,
    "status": {
        "dns": {"dns0": "192.168.7.1", "dns1": "8.8.8.8"},
        "ccw": {"ssid": "Homenet", "bssid": "AA:BB:CC:DD:EE:FF"},
        "cci": {
            "id": "12345678",
            "label": "Garage Inverter",
            "commonName": "inverter-1.2e-3_4",
            "model": "Symo",
        },
        "cards": [{"name": "Alex Example", "cardId": "04A1B2C3D4E5F6", "energy": 12}],
        "wifis": [{"ssid": "Homenet", "staticIp": "192.168.7.55"}],
        "scan": [{"ssid": "Neighbour", "bssid": "11:22:33:44:55:66"}],
        "hsta": "Wattpilot_987654",
        "host": "Wattpilot_987654",
        "fna": "Example Wallbox",
    },
}


@pytest.fixture
async def loaded_client() -> Wattpilot:
    """A client that has processed one real status frame."""
    client = Wattpilot("192.0.2.10", "secret")
    await client._handle_message(json.dumps(STATUS_FRAME))
    return client


def _exported(snapshot: dict[str, object]) -> str:
    """What actually lands in the user's diagnostics download."""
    return json.dumps(sanitize_snapshot(snapshot), cls=ExtendedJSONEncoder)


async def test_nothing_identifying_survives_the_real_export(
    loaded_client: Wattpilot,
) -> None:
    exported = _exported(loaded_client.all_properties)

    for leaked in (
        "192.168.7.1",
        "192.168.7.55",
        "Homenet",
        "Neighbour",
        "AA:BB:CC:DD:EE:FF",
        "11:22:33:44:55:66",
        "Garage Inverter",
        "04A1B2C3D4E5F6",
        "Alex Example",
        "Example Wallbox",
        "987654",
    ):
        assert leaked not in exported, f"{leaked} reached the diagnostics export"


async def test_the_normal_client_shapes_are_not_treated_as_unexpected(
    loaded_client: Wattpilot,
) -> None:
    """`cards` and `cci` carry their own handling. Reporting the client's
    ordinary values as an unknown shape drops the very data a bug report
    needs (audit A11-03)."""
    sanitized = sanitize_snapshot(loaded_client.all_properties)

    assert sanitized["cci"]["model"] == "Symo"
    assert sanitized["cards"] == [{"name": "Card 0", "cardId": True, "energy": 12}]
    assert UNEXPECTED_SHAPE not in json.dumps(sanitized, cls=ExtendedJSONEncoder)


async def test_the_client_hands_out_plain_json(loaded_client: Wattpilot) -> None:
    """The guarantee the redaction relies on: whatever the wire looked like,
    a property is a dict, a list or a scalar by the time anyone reads it."""
    assert isinstance(loaded_client.all_properties["dns"], dict)
    assert isinstance(loaded_client.all_properties["wifis"][0], dict)
    assert loaded_client.wifi_ssid == "Homenet"

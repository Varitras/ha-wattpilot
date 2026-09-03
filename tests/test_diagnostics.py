"""Diagnostics must never leak credentials or identifiers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattpilot.const import DOMAIN
from custom_components.wattpilot.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from custom_components.wattpilot.redaction import (
    DROP_KEYS,
    MAC_RE,
    MAC_SENTINEL,
    sanitize_snapshot,
)

from .test_init import V2_LOCAL_DATA, setup_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import FakeWattpilot


def test_redact_set_covers_entry_data() -> None:
    """TO_REDACT only needs the entry-level connection settings; device
    properties are covered by redaction.sanitize_snapshot instead (see
    diagnostics.py's module docstring for why the two are split)."""
    assert {"password", "host"} == TO_REDACT


# Written out here on purpose, NOT derived from redaction.DROP_KEYS. Building
# the input from the very set under test made this check unfalsifiable:
# deleting an entry from DROP_KEYS deleted its own test case too, so the
# suite stayed green while that credential started flowing into diagnostics.
# Measured, not assumed -- dropping "ocppck" from DROP_KEYS left all three
# diagnostics tests passing.
CREDENTIAL_KEYS = frozenset(
    {
        "cak",  # cloud API key, live
        "faccak",  # ...and its factory-reset counterpart
        "wak",  # WiFi AP password, live
        "facwak",  # ...and its factory-reset counterpart
        "ocppck",  # OCPP client key
        "ocppcc",  # OCPP client certificate
        "ocppfu",  # OCPP backend URL (may embed basic-auth credentials)
        "ocppu",  # ...second OCPP URL, same reason
        "data",  # cloud URL carrying a per-device access token
        "dll",  # ...second cloud URL, same reason
        "wsl",  # WiFi roaming log: nearby SSIDs and MACs
        "ipw",  # 32-char hex value shaped like a hashed credential
    }
)


def test_diagnostics_drops_every_credential_key() -> None:
    """Not one of the keys above may survive into diagnostics' output."""
    snapshot = dict.fromkeys(CREDENTIAL_KEYS, "sentinel-value")
    assert sanitize_snapshot(snapshot) == {}


def test_credential_key_list_matches_drop_keys() -> None:
    """
    The two lists must agree, and disagreeing is a decision, not a detail.

    A key added to DROP_KEYS without a line above goes unverified; a key
    that leaves DROP_KEYS while still listed above is a leak. Either way a
    human has to look, which is exactly what deriving one from the other
    prevented.
    """
    assert CREDENTIAL_KEYS == DROP_KEYS


# Realistic raw values the real firmware reports before anonymization -- what
# scripts/anonymize_probe.py scrubs before tests/fixtures/device_properties.json
# is ever committed. The committed fixture is therefore already clean and
# cannot exercise "does redaction actually scrub raw data"; these are layered
# onto the real fixture's property shape (same keys, same structure) instead.
RAW_SSID = "Mueller_Family_5G"
RAW_NEIGHBOR_SSID = "Neighbor_Wifi"
RAW_BSSID = "AA:BB:CC:DD:EE:01"
RAW_NEIGHBOR_BSSID = "AA:BB:CC:DD:EE:02"
RAW_MAC = "12:34:56:78:9A:BC"
# Hyphen-separated MAC: some fields carry this form, and it must be scrubbed
# just like the colon form -- caught only by MAC_RE, no key-name list covers it.
RAW_DASH_MAC = "DE-AD-BE-EF-00-99"
RAW_IP = "203.0.113.42"
RAW_NETWORK_OVERRIDES: dict[str, Any] = {
    # MAC-shaped strings not covered by any key-name list, only by MAC_RE.
    "maca": RAW_MAC,
    "macs": RAW_MAC,
    "abm": RAW_MAC,
    "dbm": RAW_MAC,
    "wcb": RAW_MAC,
    "macd": RAW_DASH_MAC,
    "wpb": [RAW_MAC],
    "dns": {"dns0": RAW_IP, "dns1": "8.8.8.8", "dns2": ""},
    "wifis": [
        {
            "ssid": RAW_SSID,
            "key": True,
            "useStaticIp": True,
            "staticIp": RAW_IP,
            "staticGateway": "203.0.113.1",
            "staticSubnet": "255.255.255.0",
            "staticDns0": RAW_IP,
        }
    ],
    "scan": [
        {"ssid": RAW_SSID, "bssid": RAW_BSSID, "rssi": -55, "channel": 6},
        {
            "ssid": RAW_NEIGHBOR_SSID,
            "bssid": RAW_NEIGHBOR_BSSID,
            "rssi": -80,
            "channel": 11,
        },
    ],
}


async def test_diagnostics_redact_sensitive_data(
    hass: HomeAssistant, fake_charger: FakeWattpilot
) -> None:
    # all_properties is a copy-returning property (see conftest.py); seed the
    # backing dict directly so the value actually reaches hub.properties.
    fake_charger._properties["cak"] = "SECRET_CLOUD_KEY_123"
    fake_charger._properties.update(RAW_NETWORK_OVERRIDES)
    entry = MockConfigEntry(
        domain=DOMAIN, data=V2_LOCAL_DATA, version=2, unique_id="123456"
    )
    assert await setup_entry(hass, entry, fake_charger)
    result = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(result)

    assert "SECRET_CLOUD_KEY_123" not in dump
    assert "secret" not in dump  # the password
    assert result["properties"]["amp"] == fake_charger.all_properties["amp"]
    assert result["device"]["firmware"] == "42.5"

    # No MAC-shaped string survives anywhere in the serialized output --
    # generic, so it does not depend on which key happened to hold it.
    assert MAC_RE.search(dump) is None
    for raw_value in (
        RAW_SSID,
        RAW_NEIGHBOR_SSID,
        RAW_BSSID,
        RAW_NEIGHBOR_BSSID,
        RAW_MAC,
        RAW_DASH_MAC,
        RAW_IP,
        "8.8.8.8",
    ):
        assert raw_value not in dump, f"{raw_value!r} leaked into diagnostics"

    # dns/wifis/scan structures carry no original addresses -- checked
    # structurally too, not just "the raw value is gone from the dump".
    properties = result["properties"]
    assert properties["dns"] == {"dns0": "192.0.2.1", "dns1": "192.0.2.1", "dns2": ""}
    assert all(wifi["ssid"] == "TestNet" for wifi in properties["wifis"])
    assert all(wifi["staticIp"] == "192.0.2.1" for wifi in properties["wifis"])
    assert all(scan["ssid"] == "TestNet" for scan in properties["scan"])
    assert all(scan["bssid"] == MAC_SENTINEL for scan in properties["scan"])


def test_time_server_host_is_not_exposed() -> None:
    """Audit VA-07: `ts` is the configured NTP server. A public pool name is
    harmless, an internal hostname or private IP is not, and the redaction
    cannot tell them apart -- so it replaces the string.

    The second half pins why this is not a NET_FIELDS entry: `smd` carries a
    numeric `ts` of its own (a timestamp), and a name-based rule one level
    down would rewrite that too.
    """
    cleaned = sanitize_snapshot(
        {"ts": "ntp.internal.example", "smd": {"I1": 1.5, "ts": 2887121445}}
    )
    assert "internal" not in str(cleaned["ts"])
    assert cleaned["smd"]["ts"] == 2887121445


def test_unexpected_card_and_companion_shapes_are_safe() -> None:
    """Audit VA-12: the redaction is written for the shapes firmware 42.5
    sends. A different one must fail safe, not either way it did:

    - a card that is not a dict passed through untouched, so a bare string
      there stayed a card identifier;
    - `cci` that is not a dict hit an unconditional .update() and raised,
      which does not leak but breaks the whole diagnostics download -- the
      one thing a user needs when reporting a problem.
    """
    cleaned = sanitize_snapshot({"cards": ["OWNER-CARD-1234", {"name": "n"}]})
    assert "OWNER-CARD-1234" not in str(cleaned["cards"])

    for shape in (None, "unexpected", 42, []):
        assert sanitize_snapshot({"cci": shape}) is not None

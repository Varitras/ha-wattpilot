"""
Single source of truth for scrubbing identifying data out of device properties.

Two consumers share this module: diagnostics.py (the live redaction users
paste into public issue trackers) and scripts/anonymize_probe.py (which
builds the committed tests/fixtures/device_properties.json from a local probe
dump). They used to carry independent copies of this logic -- diagnostics.py
only ever redacted by key name, so fields it never learned about (wifis,
scan, dns, and any MAC-shaped string not named "maca"/"macs") passed through
untouched. Keeping one module means the generic protections here (the MAC_RE
regex substitution, the NET_FIELDS name matching) automatically cover both
consumers, and there is nothing left to keep in sync by hand.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# wak/facwak: WiFi AP password, live + factory-reset value.
# cak/faccak: another live + factory-reset credential pair (cak is already
#   masked at probe time; faccak is its untouched factory-reset counterpart).
# ocppck/ocppcc/ocppfu/ocppu: OCPP client key/cert and backend URLs -- OCPP
#   URLs conventionally carry inline basic-auth credentials (ws://user:pass@host).
# data/dll: cloud API URLs, each embedding a long per-device access token
#   (data.wattpilot.io/{data,export}?e=<token>).
# wsl/ipw: WiFi roaming log (nearby SSIDs/MACs) / a 32-char hex value shaped
#   like a hashed credential.
DROP_KEYS = {
    "wsl",
    "ipw",
    "cak",
    "wak",
    "facwak",
    "ocppck",
    "ocppcc",
    "faccak",
    "data",
    "dll",
    "ocppfu",
    "ocppu",
}
REPLACE: dict[str, Any] = {
    "sse": "123456",
    "fna": "Wattpilot",
    "ffna": "Wattpilot_123456",
    "wan": "Wattpilot_123456",
    "wae": True,
    "ct": "default",  # carType free text identifies the owner's specific vehicle model
}
# Both separators, colon and hyphen, each consistent across the address.
# The sentinel below is intentionally non-hex so it is never re-matched here.
MAC_RE = re.compile(
    r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b"
    r"|\b[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}\b"
)
MAC_SENTINEL = "xx:xx:xx:xx:xx:xx"
# RFC 2606 reserves .invalid, so this can never resolve anywhere.
TIME_SERVER_REPLACEMENT = "time.example.invalid"

# Network-identifying field names, matched after lowercasing and stripping a
# leading "static" -- wifis[i].staticIp/staticGateway/staticSubnet/staticDns*
# carry the same address data as the top-level ip/gw/netmask/dns* fields,
# just per-WiFi-slot and prefixed.
NET_FIELDS = frozenset(
    {
        "ip",
        "gw",
        "gateway",
        "netmask",
        "subnet",
        "dns",
        "dns0",
        "dns1",
        "dns2",
        "ssid",
        "bssid",
        "ipv6",
    }
)
NET_REPLACEMENTS: dict[str, Any] = {
    "ssid": "TestNet",
    "bssid": MAC_SENTINEL,
    "ipv6": ["2001:db8::1"],  # RFC 3849 documentation prefix
}
# Already-innocuous defaults: skipping them keeps the fixture faithful to what
# the real firmware reports for unconfigured fields, while still closing the
# hole for configured (i.e. actually identifying) ones.
SAFE_ADDRESS_VALUES = frozenset(
    {
        "",
        "0.0.0.0",  # noqa: S104 -- data literal, not a bind address
        "255.255.255.0",
        "192.0.2.1",
        "::",
        "0:0:0:0:0:0:0:0",
    }
)


def _is_safe_address(value: Any) -> bool:  # noqa: ANN401 -- untyped JSON
    """Return whether value is already a safe, non-identifying address."""
    if value is None:
        return True
    if isinstance(value, list):
        return not value
    return isinstance(value, str) and value in SAFE_ADDRESS_VALUES


def scrub(value: Any) -> Any:  # noqa: ANN401 -- recurses over untyped JSON
    """Replace MAC addresses and network-identifying fields in JSON values."""
    if isinstance(value, str):
        # Non-hex sentinel (see MAC_SENTINEL): a hex, MAC-shaped replacement
        # would be re-matched by MAC_RE and trip the "no MAC leaked" guard.
        return MAC_RE.sub(MAC_SENTINEL, value)
    if isinstance(value, dict):
        cleaned = {k: scrub(v) for k, v in value.items()}
        for key, current in cleaned.items():
            net_key = key.lower().removeprefix("static")
            if net_key not in NET_FIELDS or _is_safe_address(current):
                continue
            cleaned[key] = NET_REPLACEMENTS.get(net_key, "192.0.2.1")
        return cleaned
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


UNEXPECTED_SHAPE = "<redacted: unexpected shape>"


def _sanitized_card(index: int, card: Any) -> Any:  # noqa: ANN401 -- untyped JSON
    """Return one ID chip with its identifiers removed."""
    if not isinstance(card, dict):
        return UNEXPECTED_SHAPE
    return {
        "name": f"Card {index}",
        "cardId": bool(card.get("cardId")),
        "energy": card.get("energy", 0),
    }


def sanitize_property(key: str, value: Any) -> Any:  # noqa: ANN401 -- untyped JSON
    """Sanitize one charger property. Callers must skip DROP_KEYS first."""
    if key == "cards":
        # A card that is not a dict used to pass through as-is, which for a
        # bare string means the identifier itself survived. An unknown shape
        # is exactly the case we cannot judge, so it is dropped (VA-12).
        return [_sanitized_card(i, card) for i, card in enumerate(value)]
    if key == "cci":
        # Paired companion device (e.g. a solar inverter's DataManager).
        # id/label/commonName are per-installation identifiers (label is
        # a user-editable name, commonName embeds a unique device ID);
        # model/deviceFamily/message are fixed product constants and
        # pass through scrub() unchanged.
        if not isinstance(value, dict):
            # The unconditional update() below raised on anything else, which
            # took the whole diagnostics download with it -- the one thing a
            # user needs when reporting a problem (VA-12).
            return UNEXPECTED_SHAPE
        companion = scrub(value)
        companion.update(
            id="00000000", label="Companion Device", commonName="companion-0.0e-0_0"
        )
        return companion
    if key == "ts" and isinstance(value, str):
        # timeServer: a public pool name is harmless, an internal hostname or
        # a private IP is not, and nothing in the value tells them apart, so
        # it always goes. Not a NET_FIELDS entry and not in REPLACE, because
        # both match by name at any depth and `smd` carries a numeric `ts` of
        # its own -- a timestamp, which must survive (audit VA-07).
        return TIME_SERVER_REPLACEMENT
    if key in REPLACE:
        return REPLACE[key]
    return scrub(value)


def sanitize_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a property snapshot with identifying values dropped or replaced."""
    return {
        key: sanitize_property(key, value)
        for key, value in snapshot.items()
        if key not in DROP_KEYS
    }

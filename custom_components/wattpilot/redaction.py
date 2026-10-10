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

import ipaddress
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
# host/hsta/hsts/fwan: hostnames the bundled definition documents as
# "Wattpilot_<serial>". None of them is populated on firmware 42.5, which is
# exactly why they had no decision -- the register only knew the fields this
# one device happened to report (audit A11-03).
REPLACE: dict[str, Any] = {
    "sse": "123456",
    "fna": "Wattpilot",
    "ffna": "Wattpilot_123456",
    "wan": "Wattpilot_123456",
    "fwan": "Wattpilot_123456",
    "host": "Wattpilot_123456",
    "hsta": "Wattpilot_123456",
    "hsts": "Wattpilot_123456",
    # The Ethernet hostname of a Wattpilot Flex, spelled with a hyphen.
    "hste": "Wattpilot-123456",
    "wae": True,
    "ct": "default",  # carType free text identifies the owner's specific vehicle model
    # Free text nobody bounds: a load group named after a place, a WiFi error
    # quoting a network name. Empty on the reference device, which proves
    # nothing about the next (audit A17-02).
    "log": "<redacted>",
    "wsm": "<redacted>",
    # Readable on the reference device, a TLS error; on a Flex on Ethernet
    # it quotes the charger's address.
    "cle": "<redacted>",
}
# Flex chip names (c0n..c9n), the flat counterpart of cards[].name.
CHIP_NAME_KEY = re.compile(r"c(\d)n")
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
        "resolv",  # resolv.conf: name servers and the local search domain
    }
)
NET_REPLACEMENTS: dict[str, Any] = {
    "ssid": "TestNet",
    "bssid": MAC_SENTINEL,
    "ipv6": ["2001:db8::1"],  # RFC 3849 documentation prefix
    "resolv": "<redacted>",
}
# Value-based nets for addresses inside text, whatever the key: name-based
# rules only know the keys someone has seen, and a Flex on Ethernet reported
# a dozen nobody had. ipaddress decides, so times and versions survive.
# A full stop may end the IPv4 address, a label may precede the IPv6 one:
# "Connect to <address>." and "inet6:<address>" got through (R1-07).
IPV4_CANDIDATE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\d)(?!\.\d)")
IPV6_CANDIDATE = re.compile(
    r"(?<!\w)[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:%\w+)?(?![\w:])"
)
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


def _documentation_address(match: re.Match[str]) -> str:
    """Swap an address for its documentation-range stand-in."""
    text = match[0]
    try:
        address = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return text
    if text in SAFE_ADDRESS_VALUES:
        return text
    if address.version == 6:  # noqa: PLR2004 -- the IP version, not a count
        return "2001:db8::1"
    return "192.0.2.1"


def _scrub_text(text: str, serial: str | None) -> str:
    # Non-hex sentinel (see MAC_SENTINEL): a hex, MAC-shaped replacement
    # would be re-matched by MAC_RE and trip the "no MAC leaked" guard.
    text = MAC_RE.sub(MAC_SENTINEL, text)
    if serial:
        text = text.replace(serial, REPLACE["sse"])
    text = IPV4_CANDIDATE.sub(_documentation_address, text)
    return IPV6_CANDIDATE.sub(_documentation_address, text)


def scrub(value: Any, serial: str | None = None) -> Any:  # noqa: ANN401 -- recurses over untyped JSON
    """Replace MACs, addresses, the serial and network fields in JSON values."""
    if isinstance(value, str):
        return _scrub_text(value, serial)
    if isinstance(value, dict):
        cleaned = {k: scrub(v, serial) for k, v in value.items()}
        for key, current in cleaned.items():
            net_key = key.lower().removeprefix("static")
            if net_key not in NET_FIELDS or _is_safe_address(current):
                continue
            cleaned[key] = NET_REPLACEMENTS.get(net_key, "192.0.2.1")
        return cleaned
    if isinstance(value, list):
        return [scrub(item, serial) for item in value]
    return value


UNEXPECTED_SHAPE = "<redacted: unexpected shape>"
# id/label/commonName are per-installation identifiers (label is a
# user-editable name, commonName embeds a unique device ID).
_COMPANION_STAND_INS = {
    "id": "00000000",
    "label": "Companion Device",
    "commonName": "companion-0.0e-0_0",
}


def _stand_in(value: Any, replacement: Any) -> Any:  # noqa: ANN401 -- untyped JSON
    """Return *replacement*, unless the value is empty."""
    if value is None or value == "":
        # Names nobody, and a stand-in would read as a real value (A18-28).
        return value
    return replacement


def _sanitized_card(index: int, card: Any) -> Any:  # noqa: ANN401 -- untyped JSON
    """Return one ID chip with its identifiers removed."""
    if not isinstance(card, dict):
        return UNEXPECTED_SHAPE
    return {
        "name": _stand_in(card.get("name"), f"Card {index}"),
        "cardId": bool(card.get("cardId")),
        "energy": card.get("energy", 0),
    }


def _sanitized_cards(value: Any) -> Any:  # noqa: ANN401 -- untyped JSON
    # A card that is not a dict used to pass through as-is, which for a bare
    # string means the identifier itself survived. An unknown shape is
    # exactly the case we cannot judge, so it is dropped (VA-12) -- and so
    # is an outer value that is not a list, which used to abort the whole
    # diagnostics download (A13-05).
    if not isinstance(value, list):
        return UNEXPECTED_SHAPE
    return [_sanitized_card(i, card) for i, card in enumerate(value)]


_KEEP = object()


def _replacement_for(key: str, value: Any) -> Any:  # noqa: ANN401 -- untyped JSON
    chip = CHIP_NAME_KEY.fullmatch(key)
    if chip:
        return _stand_in(value, f"Card {chip[1]}")
    return _stand_in(value, REPLACE.get(key, _KEEP))


def _sanitized_companion(value: Any, serial: str | None) -> Any:  # noqa: ANN401 -- untyped JSON
    """Paired companion device, e.g. a solar inverter's DataManager."""
    # model/deviceFamily/message are fixed product constants and pass
    # through scrub() unchanged.
    if not isinstance(value, dict):
        # An unconditional update() raised on anything else, which took the
        # whole diagnostics download with it -- the one thing a user needs
        # when reporting a problem (VA-12).
        return UNEXPECTED_SHAPE
    companion = scrub(value, serial)
    for field, replacement in _COMPANION_STAND_INS.items():
        companion[field] = _stand_in(companion.get(field), replacement)
    return companion


def sanitize_property(key: str, value: Any, serial: str | None = None) -> Any:  # noqa: ANN401 -- untyped JSON
    """
    Sanitize one charger property. Callers must skip DROP_KEYS first.

    *serial* is the charger's own, removed from any text that quotes it.
    """
    if key == "cards":
        return _sanitized_cards(value)
    if key == "cci":
        return _sanitized_companion(value, serial)
    if key == "ts" and isinstance(value, str):
        # timeServer: a public pool name is harmless, an internal hostname or
        # a private IP is not, and nothing in the value tells them apart, so
        # it always goes. Not a NET_FIELDS entry, which matches by name at any
        # depth while `smd` carries a numeric `ts` of its own -- a timestamp,
        # which must survive -- and only for a string (audit VA-07).
        return _stand_in(value, TIME_SERVER_REPLACEMENT)
    replacement = _replacement_for(key, value)
    if replacement is not _KEEP:
        return replacement
    return scrub(value, serial)


def sanitize_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a property snapshot with identifying values dropped or replaced."""
    serial = snapshot.get("sse")
    if not isinstance(serial, str):
        serial = None
    return {
        key: sanitize_property(key, value, serial)
        for key, value in snapshot.items()
        if key not in DROP_KEYS
    }

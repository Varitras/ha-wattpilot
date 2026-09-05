"""Every field the device reports as a string carries a redaction decision.

Adapted from the erosionsschutz kit's completeness register. The class it
exists for is documented in this project: the diagnostics blocklist matched
by field name, so every new data shape leaked exactly once -- wifis, scan and
dns each had to be discovered separately before redaction.py unified them.
A register turns "nobody thought of that field" into a red test at the moment
the surface grows, instead of a finding two audits later.

The surface is every top-level string field the charger *can* report: the
string properties of the bundled API definition, plus anything the reference
device reports as a string that the definition does not list. Reading it off
the snapshot alone was the same gap one level up -- `host`, `hsta` and `hsts`
are documented as "Wattpilot_<serial>" but are unpopulated on firmware 42.5,
so they never reached a decision until an audit read the definition (A11-03).

Deliberate boundary: nested containers (cards, cci, wifis, scan, dns) carry
their own assertions in test_guards.py::test_device_fixture_is_anonymized and
in test_diagnostics_boundary.py, which drives real frames through the client.
Widening this register to nested paths is worthwhile the day a leak is found
there.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.wattpilot.api.definition import load_api_definition
from custom_components.wattpilot.redaction import (
    DROP_KEYS,
    MAC_RE,
    REPLACE,
    TIME_SERVER_REPLACEMENT,
    sanitize_snapshot,
)

FIXTURE = Path(__file__).parent / "fixtures" / "device_properties.json"
# `ts` is replaced by sanitize_property rather than through the REPLACE
# table -- see the comment there for why it cannot live in a name-matched
# rule. The register still has to know what to expect.
REPLACEMENTS = {**REPLACE, "ts": TIME_SERVER_REPLACEMENT}

REPLACED = "replaced"
SCRUBBED = "scrubbed"
DROPPED = "dropped"
READABLE_PREFIX = "readable"
LEAKS_PREFIX = "leaks"

# One entry per top-level string field, with what redaction does to it.
# Verified by measurement, not by reading the code: each decision below was
# produced by injecting a hostname, a MAC and an IP into that field and
# observing sanitize_snapshot().
REGISTER: dict[str, str] = {
    # Replaced by a fixed synthetic value.
    "ct": REPLACED,
    "ffna": REPLACED,
    "fna": REPLACED,
    "sse": REPLACED,
    "wan": REPLACED,
    # Hostnames the definition documents as "Wattpilot_<serial>". Declared
    # but never populated on firmware 42.5 -- see the module docstring.
    "fwan": REPLACED,
    "host": REPLACED,
    "hsta": REPLACED,
    "hsts": REPLACED,
    # Never shown at all: credentials, tokens and the URLs carrying them.
    "cak": DROPPED,
    "data": DROPPED,
    "dll": DROPPED,
    "facwak": DROPPED,
    "ocppu": DROPPED,
    "wak": DROPPED,
    # MAC-shaped, caught by MAC_RE wherever it appears.
    "abm": SCRUBBED,
    "dbm": SCRUBBED,
    "maca": SCRUBBED,
    "macs": SCRUBBED,
    "wcb": SCRUBBED,
    # Deliberately readable: nothing here identifies the owner or the site.
    "arv": "readable (recommended app version, same for every device)",
    "authhash": "readable (names the hash algorithm, not a hash)",
    "art": "readable (article number of the hardware model)",
    "cch": "readable (LED colour, user preference without identity)",
    "cfi": "readable (LED colour)",
    "cid": "readable (LED colour)",
    "cwc": "readable (LED colour)",
    "ccrv": "readable (recommended charge-controller version)",
    "cle": "readable (last cloud error text, needed for support)",
    "cy": "readable (country for the tariff, coarse by design)",
    "dfam": "readable (device family, same for every Wattpilot)",
    "ens": "readable (empty on this firmware; no known identifier)",
    "fml": "readable (energy source label such as 'grid')",
    "fwc": "readable (charge-controller firmware version)",
    "fwv": "readable (firmware version -- the whole point of a diagnostic)",
    "gmk": "readable (empty on this firmware; no known identifier)",
    "grp": "readable (hardware model name, not the owner's name)",
    "imi": "readable (numeric counter carried as a string)",
    "loc": "readable (local time; the offset reveals only the time zone)",
    "log": "readable (load group id, free text -- empty here; see note below)",
    "nif": "readable (default route name such as 'st', not an address)",
    "ocm": "readable (OTA status message)",
    "ocppd": "readable (dummy card id, a device-side constant)",
    "oem": "readable (OEM manufacturer, same for every Wattpilot)",
    "onv": "readable (offered firmware version)",
    "typ": "readable (device type, same for every Wattpilot)",
    "tzt": "readable (time zone label chosen in the app)",
    "utc": "readable (UTC timestamp)",
    "imp": "readable (mDNS service protocol, a protocol constant)",
    "ims": "readable (mDNS service type of the paired inverter)",
    "los": "readable (load balancing status word, no address in it)",
    "wsm": "readable (WiFi error text; the SSID has its own fields, see note)",
    # Was the one known gap (audit VA-07); replaced since e6aec24's follow-up.
    "ts": REPLACED,
}

# `log` (load group id) and `wsm` (WiFi error text) are the two free-text
# fields whose contents nobody controls. Both are empty or absent on the
# reference device, so there is nothing to observe and no decision to derive
# from measurement; if either is ever seen populated with something
# identifying, it belongs with `ts`.


def _actual_fields() -> set[str]:
    """
    Every top-level string field the charger can report.

    Two sources, because neither alone is the surface: the bundled definition
    knows fields this device leaves unpopulated, and the device reports
    fields the definition does not list.
    """
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    observed = {key for key, value in snapshot.items() if isinstance(value, str)}
    declared = {
        key
        for key, prop in load_api_definition(split_properties=False).properties.items()
        if prop.get("jsonType") == "string"
    }
    return observed | declared


def test_every_string_field_has_an_explicit_decision() -> None:
    undecided = _actual_fields() - set(REGISTER)
    assert not undecided, (
        f"string field(s) without a redaction decision: {sorted(undecided)}. "
        "Add each to REGISTER -- replaced/scrubbed, or readable WITH a "
        "reason. Silence is how this project leaked wifis, scan and dns."
    )


def test_no_register_entry_outlives_its_field() -> None:
    stale = set(REGISTER) - _actual_fields()
    assert not stale, (
        f"register entries for fields the device no longer reports: {sorted(stale)}"
    )


def test_every_decision_is_from_the_vocabulary() -> None:
    invalid = {
        field: decision
        for field, decision in REGISTER.items()
        if decision not in {REPLACED, SCRUBBED, DROPPED}
        and not decision.startswith((READABLE_PREFIX + " (", LEAKS_PREFIX + " ("))
    }
    assert not invalid, (
        f"unknown decision(s): {invalid}. A readable field without a reason "
        "is not a decision, it is a shrug."
    )


def _sanitized_with(field: str, value: str) -> object:
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot[field] = value
    return sanitize_snapshot(snapshot).get(field)


def test_replaced_and_scrubbed_fields_really_are() -> None:
    """The register must not be able to claim a protection that is absent.

    The SCRUBBED half needs the fixture value, not an injected MAC: MAC_RE
    runs over every string, so injecting one proves nothing about *this*
    field. What distinguishes a scrubbed field is that its real value is
    MAC-shaped and therefore depends on that protection -- checking the
    injected value alone let a deliberately wrong entry pass.
    """
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for field, decision in REGISTER.items():
        if decision == REPLACED:
            assert _sanitized_with(field, "owner-identifying") == REPLACEMENTS[field], (
                field
            )
        if decision == DROPPED:
            assert field in DROP_KEYS, field
            assert _sanitized_with(field, "secret") is None, field
        if decision == SCRUBBED:
            # Only for a field this device actually reports a string for:
            # the injected MAC below is scrubbed for every string, so it says
            # nothing about this one. What makes a field MAC-scrubbed is that
            # its real value depends on that protection.
            if isinstance(snapshot.get(field), str):
                assert MAC_RE.search(snapshot[field]), (
                    f"{field} is registered as MAC-scrubbed but its real value "
                    f"{snapshot[field]!r} is not MAC-shaped"
                )
            mac = "AA:BB:CC:DD:EE:FF"
            assert _sanitized_with(field, mac) != mac, field


def test_no_field_is_registered_as_leaking() -> None:
    """This started as a ratchet on `ts`, the one field the audit found
    passing identifying data through. It went red the moment that was fixed,
    which is what a ratchet is for; the vocabulary keeps the entry so a
    future gap can be recorded rather than argued about."""
    declared = {
        field
        for field, decision in REGISTER.items()
        if decision.startswith(LEAKS_PREFIX)
    }
    assert declared == set(), f"field(s) knowingly leaking: {sorted(declared)}"

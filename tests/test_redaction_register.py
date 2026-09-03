"""Every field the device reports as a string carries a redaction decision.

Adapted from the erosionsschutz kit's completeness register. The class it
exists for is documented in this project: the diagnostics blocklist matched
by field name, so every new data shape leaked exactly once -- wifis, scan and
dns each had to be discovered separately before redaction.py unified them.
A register turns "nobody thought of that field" into a red test at the moment
the surface grows, instead of a finding two audits later.

Deliberate boundary: the surface is the *top-level string* fields of the real
device snapshot. Nested containers (cards, cci, wifis, scan, dns) carry their
own assertions in test_guards.py::test_device_fixture_is_anonymized, and the
generic MAC_RE/NET_FIELDS protections cover them by shape rather than by
name. Widening this register to nested paths is worthwhile the day a leak is
found there.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.wattpilot.redaction import (
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
    # MAC-shaped, caught by MAC_RE wherever it appears.
    "abm": SCRUBBED,
    "dbm": SCRUBBED,
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
    # Was the one known gap (audit VA-07); replaced since e6aec24's follow-up.
    "ts": REPLACED,
}

# `log` is free text a user could fill with anything identifying. It is empty
# on the reference device, so there is nothing to observe and no decision to
# derive; if a populated one is ever seen, it belongs with `ts`.


def _actual_fields() -> set[str]:
    """The real surface, read from the device snapshot -- never hand-copied."""
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {key for key, value in snapshot.items() if isinstance(value, str)}


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
        if decision not in {REPLACED, SCRUBBED}
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
        if decision == SCRUBBED:
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

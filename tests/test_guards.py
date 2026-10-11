"""Guards: machine-enforced project rules. See tests/README.md for the index."""

from __future__ import annotations

import ast
import json
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
import yaml

from custom_components.wattpilot.redaction import DROP_KEYS, REPLACE
from scripts import complexity_snapshot

from .conftest import IDENTIFIERS

PACKAGE = Path(__file__).parent.parent / "custom_components" / "wattpilot"


# The client package. It arrived as a third-party copy and was exempt from
# the house rules while it was one; it is maintained here now, so it is not.
API_DIR = "api"


def iter_python_files() -> list[Path]:
    files = sorted(PACKAGE.rglob("*.py"))
    assert files, "package scan must never be empty (guard would be blind)"
    return files


# Names whose value identifies a device or a network. Logs are what users
# attach to public issues, and nothing redacts them.
# err/exc: a socket error's text quotes the charger's address and port.
# entity_id: installations set up with 0.2.0 or earlier name the device after
# its host name, Wattpilot_<serial>, so their entity IDs carry the serial.
IDENTIFYING_NAMES = frozenset(
    {
        "serial",
        "_serial",
        "unique_id",
        "host",
        "_host",
        "hostname",
        "friendly_name",
        "title",
        "entity_id",
        "token",
        "err",
        "exc",
    }
)
# As an attribute only: hub.name, charger.name, the host name again. A plain
# `name` is a property's name.
IDENTIFYING_ATTRIBUTES = IDENTIFYING_NAMES | {"name"}
# Calls whose result carries no identifier, whatever goes in.
SAFE_WRAPPERS = frozenset({"mask_serial", "describe_error", "type"})
# An error's reason is a fixed phrase, a decode error's "invalid start byte".
SAFE_ATTRIBUTES = frozenset({"reason"})
LOGGER_LEVELS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "fatal",
        "log",
    }
)
# Home Assistant logs these with their placeholders filled in. A repair
# issue is not among them: it is shown, not logged.
LOGGED_EXCEPTIONS = frozenset(
    {
        "ConfigEntryError",
        "ConfigEntryAuthFailed",
        "ConfigEntryNotReady",
        "HomeAssistantError",
        "ServiceValidationError",
    }
)
# This package's own errors: describe_error() logs their text as it is.
OWN_ERRORS = frozenset(
    node.name
    for node in ast.walk(
        ast.parse((PACKAGE / API_DIR / "exceptions.py").read_text(encoding="utf-8"))
    )
    if isinstance(node, ast.ClassDef)
)
# Catches that can hold an error quoting the charger's address, as a refused
# connect's does: socket errors by any of their names, and the broad catches.
TRANSPORT_ERRORS = frozenset(
    {
        "OSError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "WebSocketException",
        "InvalidURI",
        "InvalidHandshake",
        "_WRITE_ERRORS",
        "Exception",
        "BaseException",
    }
)


def _is_logger_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_LOGGER"
        and node.func.attr in LOGGER_LEVELS
    )


def _logged_values(node: ast.AST) -> list[ast.expr]:
    """What a log call or a logged exception writes, or nothing."""
    if _is_logger_call(node):
        return list(node.args)  # type: ignore[attr-defined]
    # ruff (EM101/EM102) puts every exception message into `msg` first.
    is_message = (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and getattr(node.targets[0], "id", None) == "msg"
    )
    if is_message:
        return [node.value]  # type: ignore[attr-defined]
    is_logged_exception = isinstance(node, ast.Call) and (
        getattr(node.func, "id", None) in LOGGED_EXCEPTIONS | OWN_ERRORS
    )
    if not is_logged_exception:
        return []
    return [
        *node.args,  # type: ignore[attr-defined]
        *(
            keyword.value
            for keyword in node.keywords  # type: ignore[attr-defined]
            if keyword.arg == "translation_placeholders"
        ),
    ]


def _unmasked_identifiers(node: ast.AST) -> list[str]:
    """Identifying names in *node* that do not pass through a safe wrapper."""
    is_safe_call = isinstance(node, ast.Call) and (
        getattr(node.func, "id", None) in SAFE_WRAPPERS
    )
    is_safe_attribute = getattr(node, "attr", None) in SAFE_ATTRIBUTES
    if is_safe_call or is_safe_attribute:
        return []
    found = []
    if isinstance(node, ast.Name) and node.id in IDENTIFYING_NAMES:
        found.append(node.id)
    if isinstance(node, ast.Attribute) and node.attr in IDENTIFYING_ATTRIBUTES:
        found.append(node.attr)
    for child in ast.iter_child_nodes(node):
        found.extend(_unmasked_identifiers(child))
    return found


def _identifier_leaks(source: str) -> list[str]:
    return [
        f"{node.lineno} {names}"
        for node in ast.walk(ast.parse(source))
        for names in [
            [
                name
                for value in _logged_values(node)
                for name in _unmasked_identifiers(value)
            ]
        ]
        if names
    ]


def _catches_transport_errors(handler: ast.ExceptHandler) -> bool:
    caught = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    return any(
        getattr(name, "id", getattr(name, "attr", None)) in TRANSPORT_ERRORS
        for name in caught
    )


def _shows_traceback(node: ast.AST, error: str | None) -> bool:
    """A raise that chains *error*, or a log call that prints the traceback."""
    if isinstance(node, ast.Raise):
        # A bare raise passes the error itself on; whoever catches it is
        # checked there. Without `from`, the caught error is chained too.
        reraised = node.exc is None
        chained = node.cause is None or getattr(node.cause, "id", None) == error
        return not reraised and chained
    if not _is_logger_call(node):
        return False
    with_traceback = any(
        keyword.arg == "exc_info"
        and not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)
        for keyword in node.keywords  # type: ignore[attr-defined]
    )
    return with_traceback or node.func.attr == "exception"  # type: ignore[attr-defined]


def _own_nodes(handler: ast.ExceptHandler) -> list[ast.AST]:
    """The handler's nodes, without nested handlers: those log their own error."""
    nodes, pending = [], list(handler.body)
    while pending:
        node = pending.pop()
        nodes.append(node)
        pending.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, ast.ExceptHandler)
        )
    return nodes


def _traceback_leaks(source: str) -> list[str]:
    """
    Tracebacks of an error that can quote the address.

    Home Assistant logs a failed setup's traceback, causes included, and a
    message mask never reaches it (R1-01).
    """
    return [
        f"{inner.lineno} traceback of {handler.name or 'the error'}"
        for handler in ast.walk(ast.parse(source))
        if isinstance(handler, ast.ExceptHandler) and _catches_transport_errors(handler)
        for inner in _own_nodes(handler)
        if _shows_traceback(inner, handler.name)
    ]


def _log_leaks(source: str) -> list[str]:
    return _identifier_leaks(source) + _traceback_leaks(source)


@pytest.mark.parametrize(
    "snippet",
    [
        '_LOGGER.info("x %s", self.serial)',
        '_LOGGER.critical("x %s", host)',
        '_LOGGER.log(20, "x %s", self.hub.title)',
        '_LOGGER.debug("failed: %s", exc)',
        'raise ConfigEntryNotReady(translation_placeholders={"error": str(err)})',
        'raise HomeAssistantError(translation_placeholders={"serial": serial})',
        '_LOGGER.warning("%s: %s", self.entity_id, type(err).__name__)',
        '_LOGGER.info("%s connected", self.hub.name)',
        'raise DeviceIdentityError(f"another charger at {self._host}")',
        'msg = f"no answer from {self._host}"',
        'raise ConfigEntryNotReady(f"cannot reach {host}")',
        'raise HomeAssistantError(f"charger {serial} refused")',
        "try: pass\nexcept OSError as err: raise ConfigEntryNotReady() from err",
        "try: pass\nexcept (ValueError, OSError): _LOGGER.exception('failed')",
        "try: pass\nexcept _WRITE_ERRORS as err: _LOGGER.debug('x', exc_info=err)",
        "try: pass\nexcept ws.WebSocketException: _LOGGER.info('x', exc_info=True)",
        "try: pass\nexcept _WRITE_ERRORS: raise ConfigEntryNotReady()",
        "try: pass\nexcept Exception: _LOGGER.debug('x', exc_info=True)",
        "try: pass\nexcept ConnectionRefusedError as err: raise X() from err",
    ],
)
def test_the_identifier_guard_can_actually_fail(snippet: str) -> None:
    assert _log_leaks(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        '_LOGGER.info("x %s", mask_serial(self.serial))',
        '_LOGGER.debug("failed: %s", describe_error(exc))',
        '_LOGGER.warning("%s: %s", description.key, type(err).__name__)',
        'msg = f"Cannot convert {value!r} for property {name!r}"',
        'msg = f"Failed to decode: {exc.reason}"',
        "try: pass\nexcept OSError as err: raise ConfigEntryNotReady() from None",
        "try: pass\nexcept TimeoutError as err: raise CommandError(msg) from err",
        "try: pass\nexcept OSError: _LOGGER.debug('x', exc_info=False)",
        "try: pass\nexcept BaseException: raise",
    ],
)
def test_the_identifier_guard_lets_masked_values_through(snippet: str) -> None:
    assert not _log_leaks(snippet), snippet


def test_no_device_identifier_reaches_the_log() -> None:
    """
    A serial, host or token that reaches the log goes through mask_serial.

    The connect and availability messages logged the full serial at INFO,
    and the wrong-charger setup error carried both serials into the log
    Home Assistant writes for it -- the log users paste into issues. A
    setup retry's traceback quoted the address through the chained cause.
    Fix: wrap the value in api.mask_serial, or leave it out; raise a
    transport error's replacement `from None`.
    """
    offenders = [
        f"{path.relative_to(PACKAGE)}:{leak}"
        for path in iter_python_files()
        for leak in _log_leaks(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"device identifiers logged unmasked: {offenders}"


def _strip_vendor_spellings(text: str) -> str:
    """Remove the vendor package spellings before the domain-literal test.

    Naming the library we wrap in prose (e.g. a module docstring) has
    nothing to do with make_dev_copy's replace and shouldn't force code to
    dodge the guard with a capitalization trick.
    """
    return text.replace("wattpilot_api", "").replace("wattpilot-api", "")


def test_domain_literal_only_in_const() -> None:
    """Among Python files, the domain string lives in const.py only. Fix:
    use signal helpers or import DOMAIN from const instead of embedding the
    literal (includes f-string chunks).

    AST-based on purpose: only actual string constants count, so prose in
    comments/docstrings that happens to mention the library name (see
    _strip_vendor_spellings) can't false-positive here. That precision is
    also why this guard is Python-only -- test_domain_literal_confined_to_
    known_carriers below covers the non-Python files as plain text.

    api/ is exempt, and the reason is not "third-party code": it names the
    product and its protocol ("wattpilot.yaml", "Wattpilot: <name>"), never
    the Home Assistant domain -- it knows nothing about Home Assistant at
    all, which test_the_client_stays_independent_of_home_assistant pins.
    Renaming those strings in the dev copy would be wrong, not right.
    """
    offenders: list[str] = []
    for path in iter_python_files():
        if path.name == "const.py" or API_DIR in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            is_wattpilot_literal = (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "wattpilot" in _strip_vendor_spellings(node.value)
            )
            if is_wattpilot_literal:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"'wattpilot' literal outside const.py: {offenders}"


# The only files make_dev_copy.py rewrites -- see its docstring.
KNOWN_DOMAIN_CARRIERS = {"const.py", "manifest.json", "services.yaml"}


def test_domain_literal_confined_to_known_carriers() -> None:
    """The domain literal must not silently gain a fourth carrier.

    services.yaml carried "integration: wattpilot" four times, unnoticed by
    test_domain_literal_only_in_const because that guard only looks at
    Python. make_dev_copy.py copied it byte-for-byte, so the dev copy's
    device picker filtered for the wrong integration -- exactly the
    regression this guard exists to catch. Python files are already covered
    precisely (by AST, so prose mentions of the library name don't
    false-positive) by test_domain_literal_only_in_const; this scans
    everything else in the package as plain text.
    """
    offenders: list[str] = []
    for path in PACKAGE.rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        # api/ for the same reason as in the guard above: the client's
        # resource file describes the charger protocol, not the integration.
        if path.suffix == ".py" or path.name in KNOWN_DOMAIN_CARRIERS:
            continue
        if API_DIR in path.parts:
            continue
        if "wattpilot" in _strip_vendor_spellings(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(PACKAGE)))
    assert not offenders, f"'wattpilot' literal outside known carriers: {offenders}"


# Third-party modules the integration may import without declaring them,
# because Home Assistant itself depends on them and ships them to every
# installation. Anything else has to appear in manifest.json: an undeclared
# import is a module that simply is not there on a user's system.
PROVIDED_BY_HOME_ASSISTANT = {
    # Not voluptuous: HA replaced it with probatio in 2026.9 and only aliases
    # the old name at runtime, which type checking does not see.
    "probatio": "config-flow and action schemas; a core HA dependency since 2026.9",
    "yaml": "PyYAML, used to read the bundled API definition; core HA dependency",
    "bcrypt": "the charger's password hash in api/auth.py; core HA dependency",
    "packaging": "firmware version comparisons; core HA dependency",
}


def _third_party_imports() -> dict[str, set[str]]:
    """Every non-stdlib, non-local module the integration imports, by module."""
    local = {"custom_components", "homeassistant"}
    found: dict[str, set[str]] = {}
    for path in iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top and top not in sys.stdlib_module_names and top not in local:
                    found.setdefault(top, set()).add(path.name)
    return found


def _declared_distributions() -> set[str]:
    """The distribution names manifest.json declares, lowercased."""
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    return {
        re.split(r"[<>=!~\[]", spec, maxsplit=1)[0].strip().lower()
        for spec in manifest["requirements"]
    }


def test_every_third_party_import_is_declared() -> None:
    """
    An import the manifest does not declare is missing on a user's system.

    Home Assistant installs exactly what manifest.json asks for. The test
    environment has far more than that, so the suite stays green while a real
    installation raises ImportError on the first use -- which is why this is
    checked by name against the manifest rather than by running the code.

    Fix: add the distribution to manifest.json's requirements, or, if Home
    Assistant itself ships it, record it in PROVIDED_BY_HOME_ASSISTANT with
    the reason.
    """
    declared = _declared_distributions()
    undeclared = {
        module: sorted(files)
        for module, files in _third_party_imports().items()
        if module.lower() not in declared and module not in PROVIDED_BY_HOME_ASSISTANT
    }
    assert not undeclared, (
        f"third-party import(s) without a declaration: {undeclared}. "
        "Home Assistant installs only what the manifest asks for."
    )


def test_no_manifest_requirement_is_unused() -> None:
    """
    A requirement nothing imports is a dependency users install for nothing.

    Fix: drop it from manifest.json, or, if it is needed indirectly, say so
    here -- silence turns into a stale dependency nobody dares remove.
    """
    imported = {module.lower() for module in _third_party_imports()}
    unused = sorted(_declared_distributions() - imported)
    assert not unused, (
        f"manifest.json declares requirement(s) nothing imports: {unused}"
    )


def test_manifest_does_not_declare_what_home_assistant_ships() -> None:
    """
    hassfest rejects a requirement that is a dependency of HA itself.

    The manifest listed bcrypt and packaging, both pinned by HA; hassfest
    began failing on that in October 2026 while every other gate stayed
    green. Fix: drop the requirement and record the module in
    PROVIDED_BY_HOME_ASSISTANT.
    """
    duplicated = sorted(_declared_distributions() & set(PROVIDED_BY_HOME_ASSISTANT))
    assert not duplicated, (
        f"manifest.json declares {duplicated}, which Home Assistant ships itself"
    )


def test_the_test_environment_installs_what_the_manifest_declares() -> None:
    """
    Every declared requirement has to be present where the tests run.

    Home Assistant installs them from the manifest; a test environment does
    not, unless the dev group asks. They used to arrive by accident, through
    the library the client was adopted from, and removing that package left
    a fresh environment without websockets -- which showed up as twenty-eight
    collection errors in CI rather than as one sentence.

    Fix: add the distribution to the dev group in pyproject.toml.
    """
    missing = []
    for name in sorted(_declared_distributions()):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
    assert not missing, (
        f"manifest.json declares {missing}, which the test environment does "
        "not install -- add them to the dev dependency group"
    )


def test_manifest_is_consistent() -> None:
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "wattpilot"
    assert manifest["iot_class"] == "local_push"
    # No wattpilot-api: the client lives in api/ now; mqtt/shell/discovery
    # were never adopted, and with them neither aiomqtt, prompt-toolkit nor
    # pydantic. bcrypt and packaging come with Home Assistant.
    assert manifest["requirements"] == ["websockets>=14.0"]
    assert manifest["config_flow"] is True
    assert manifest["version"] == "0.3.0b1"
    assert manifest["codeowners"] == ["@Varitras"]
    assert manifest["integration_type"] == "device"
    assert manifest["loggers"] == ["custom_components.wattpilot"]
    assert manifest["documentation"] == "https://github.com/Varitras/ha-wattpilot"
    assert (
        manifest["issue_tracker"] == "https://github.com/Varitras/ha-wattpilot/issues"
    )


def test_the_two_declared_versions_agree() -> None:
    """One release, one number. The release gate compares the tag to the
    manifest and never looked at pyproject.toml, so the two drifted to 0.1.0
    and 0.0.0 with nothing to notice (audit VA-17)."""
    import tomllib  # noqa: PLC0415 -- scoped on purpose, only this test needs it

    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert project["project"]["version"] == manifest["version"]


DEVICE_FIXTURE = Path(__file__).parent / "fixtures" / "device_properties.json"


REPLAY_FIXTURES = Path(__file__).parent / "fixtures" / "replay"
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}\b")
_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def _statuses(recording: dict[str, object]) -> list[dict[str, object]]:
    frames = [
        entry["frame"]
        for key in ("frames", "before_restart", "after_restart")
        for entry in recording.get(key, [])  # type: ignore[attr-defined]
    ]
    return frames + [f["status"] for f in frames if isinstance(f.get("status"), dict)]


def test_replay_fixtures_are_anonymized() -> None:
    """
    Recorded frame sequences carry the whole device state, several times.

    They are checked like the device snapshot above, per frame: a recording
    is easier to add than to review, and a future one from other firmware
    may carry a field the sanitizer does not know yet.
    Fix: re-record, or fix the sanitizing tables -- never hand-edit.
    """
    files = sorted(REPLAY_FIXTURES.glob("*.json"))
    assert files, "no replay fixtures found -- the guard would check nothing"
    for path in files:
        raw = path.read_text(encoding="utf-8")
        assert _MAC.search(raw) is None, f"{path.name}: MAC address"
        for address in set(_IPV4.findall(raw)):
            assert address in {"0.0.0.0", "127.0.0.1"} or address.startswith(  # noqa: S104
                ("192.0.2.", "255.255.255.")
            ), f"{path.name}: non-documentation IPv4 {address}"
        for item in _statuses(json.loads(raw)):
            dropped = DROP_KEYS & item.keys()
            assert not dropped, f"{path.name}: identifying keys {sorted(dropped)}"
            for key, synthetic in (
                ("serial", "123456"),
                ("hostname", "Wattpilot_123456"),  # carries the real serial
                ("friendly_name", "Wattpilot"),  # the name the owner chose
                ("sse", "123456"),
                ("ct", "default"),
                ("ffna", REPLACE["ffna"]),
            ):
                if key in item:
                    assert item[key] == synthetic, f"{path.name}: real {key}"
            companion = item.get("cci")
            if isinstance(companion, dict):
                assert companion.get("label") == "Companion Device", path.name


def test_device_fixture_is_anonymized() -> None:
    """The committed device snapshot must contain no owner identifiers.
    Fix: re-run scripts/anonymize_probe.py, never hand-edit."""
    import re  # noqa: PLC0415 -- scoped on purpose, only this test needs it

    raw = DEVICE_FIXTURE.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert len(data) > 300, "fixture unexpectedly small — wrong input file?"
    assert data["sse"] == "123456", "serial must be the synthetic one"
    assert re.search(r"\b[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}\b", raw) is None, (
        "MAC found"
    )
    for forbidden in DROP_KEYS:
        assert forbidden not in data, f"identifying key '{forbidden}' must be dropped"
    assert data["ct"] == "default", "car profile must not reveal the owner's vehicle"
    assert data["ffna"] == REPLACE["ffna"], "friendly network name must be synthetic"

    assert "cci" in data, "cci companion block must be present"
    companion = data["cci"]
    assert companion["id"] == "00000000", "companion id must be neutralized"
    assert companion["label"] == "Companion Device", (
        "companion label must be neutralized"
    )
    assert companion["commonName"] == "companion-0.0e-0_0", (
        "companion commonName must be neutralized"
    )

    # Generic net: catches identifying addresses the field-name matching in
    # scrub() misses (e.g. a static-IP variant nobody added to the alias set).
    allowed_ipv4_exact = {
        "0.0.0.0",  # noqa: S104 -- data, not a bind address
        "127.0.0.1",
    }
    allowed_ipv4_prefixes = ("192.0.2.", "255.255.255.")
    for candidate in set(re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", raw)):
        is_allowed = candidate in allowed_ipv4_exact or any(
            candidate.startswith(prefix) for prefix in allowed_ipv4_prefixes
        )
        assert is_allowed, f"non-allow-listed IPv4 found in fixture: {candidate}"


PLATFORM_MODULES = {
    "binary_sensor.py",
    "sensor.py",
    "switch.py",
    "number.py",
    "select.py",
    "button.py",
    "time.py",
    "update.py",
}
SIZE_LIMIT_DEFAULT = 400
SIZE_BUDGET = {  # frozen ceilings; only shrink. Files under the default
    # The entity table is data, not logic. Raised from 1230 on 2026-09-26
    # for seven new entities: two binary sensors, two selects, two numbers
    # and the price sensor (measured: 1322); again the same day for the
    # charging duration and four timings in seconds (measured: 1378). Raised
    # on 2026-10-11 for the fifteen readings per phase, approved by the owner
    # (measured: 1415); again the same day for the stop hysteresis, the
    # minimum current, the current limits and the power per source,
    # owner-approved (measured: 1501).
    "descriptions.py": 1501,
    # The charger protocol in one class: connection lifecycle, message
    # dispatch, property table and the typed write paths. Adopted at 1220
    # lines; splitting it is a separate decision, not a side effect of taking
    # it over. Three raises in the 2026-09-05 audit round triggered the
    # ratchet's own clustering criterion, and the owner decided to split
    # (ledger L-016): the connection lifecycle moved to connection.py and
    # this budget came back down with it (measured: 1192). Nudged to 1230 on
    # 2026-09-06 for the audit round's connect cleanup and firmware deadline
    # (measured: 1221). Down on 2026-10-11 with the 45 accessors nothing read
    # (audit A18-37), keeping room for the refusal in the firmware wait. Down
    # again the same day with the seven getters only tests read, then with
    # `version` (measured: 793). Up to 803 the same day, owner-approved, for
    # status frames without a status object and the close count of the
    # firmware wait (findings 6 and RF-L-01).
    "client.py": 803,
    # Past the default on 2026-09-25 by the open/close coordination (audits
    # A15-01, A16-01): shared attempts, a close that cancels instead of
    # waiting, and one visible transition at a time (measured: 406). Raised
    # on 2026-10-10, owner-approved: to 424 for recovery after a failed
    # reconnect action (A18-05/06), to 440 for a deliberate close winning
    # over recovery (R1-11/12) (measured on 2026-10-11: 437).
    "connection.py": 440,
    # Past the default on 2026-09-26 by the repair notice for a different
    # charger answering at runtime (audit A15-04): the hub is where the
    # connection's state turns into what the user is told (measured: 424).
    # Owner-approved raises: 444 on 2026-10-10 for announcing a recovery with
    # the current values (A18-20), 446 on 2026-10-11 for refusal notices
    # that go once the charger is back (reaudit RF-L-03).
    "hub.py": 446,
}
SHRINK_SLACK = 0.85  # an entry >=15% below budget must be ratcheted down

# Per-file ceilings alone are escapable: split the file and both halves are
# under budget while the thing itself kept growing. Memo did exactly that with
# partials and reached ~15,500 lines past a per-file budget. So a unit that was
# split stays measured as one cluster.
CLUSTER_BUDGET = {
    # The adopted charger client. Split on 2026-09-05 (ledger L-016) into the
    # protocol and the connection lifecycle; the pair is the unit that must
    # not grow. Raised once for the whole 2026-09-06 audit round: connection
    # ownership and readiness deadlines (A12-01/04), fail-stop on refusal
    # (A12-02), the cleanup that spans the definition load (A12-03) and the
    # firmware deadline with the disconnect event it waits on (A12-08), at a
    # measured 1567 lines. Raised a second time on 2026-09-25, after about
    # forty lines of comment had already been condensed to make room: the
    # open/close coordination (A13-01, A14-01, A15-01) and the refused-
    # password flag (A14-02), at a measured 1607 lines. Down with client.py
    # on 2026-10-11 (A18-37), and again with the last unread getters (1233); up
    # to 1240 with client.py (owner-approved).
    "api client": (("client.py", "connection.py"), 1240),
}


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            if node.module:
                modules.add(prefix + node.module)
            else:
                # Bare `from . import sibling`: the alias itself names the
                # submodule (module is None, only the level is set).
                modules |= {prefix + alias.name for alias in node.names}
    return modules


# Password hashing is deliberately expensive; the number below is the
# measured cost of one PBKDF2 handshake on the development machine.
EXPENSIVE_HASHES = frozenset({"hash_password", "_hash_pbkdf2", "_hash_bcrypt"})


def test_expensive_hashing_never_runs_on_the_event_loop() -> None:
    """
    A key-derivation call must be handed to a thread, never called directly.

    It was called directly once, and the handshake stalled Home Assistant's
    event loop for 110-116 ms on every connection (audit A11-08). Moving the
    one call site fixes the day; this stops the next one being written.

    Fix: `await asyncio.to_thread(hash_password, ...)` -- pass the function,
    do not call it.
    """
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in iter_python_files()
        if API_DIR in path.parts and path.name != "auth.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in EXPENSIVE_HASHES
    ]
    assert not offenders, f"key derivation called on the event loop: {offenders}"


# Home Assistant calls that still exist but report as deprecated at runtime,
# each with the replacement the message names.
DEPRECATED_HA_CALLS = {
    # 2026.9: identifiers are no longer unique across config entries.
    "async_get_device": "async_get_device_by_identifier / _by_connection",
}


def test_no_deprecated_home_assistant_call() -> None:
    """
    A deprecated call works until the release that removes it, then setup
    breaks for everyone at once. Home Assistant reports it at runtime, but
    only on the path that runs -- this finds it in code nobody exercised.

    Tests are scanned too: they are where such a call last lived here.
    """
    tests_directory = Path(__file__).parent
    offenders = [
        f"{path.name}:{node.lineno} {node.func.attr}"
        for path in [*iter_python_files(), *sorted(tests_directory.rglob("*.py"))]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in DEPRECATED_HA_CALLS
    ]
    assert not offenders, f"deprecated Home Assistant calls: {offenders}"


def test_the_client_never_names_its_own_install_path() -> None:
    """
    The client must not spell out where it is mounted.

    It broke exactly that way once: the resource lookup carried the literal
    "custom_components.wattpilot.api.resources", the dev copy renames the
    folder to wattpilot_dev, and setup died with ModuleNotFoundError on the
    real installation. The domain-literal guards do not catch it, because
    api/ is exempt from those -- so this is the narrow rule they cannot be:
    product and protocol names are fine in there, an install path is not.

    Fix: derive the location, e.g. from __package__, instead of writing it.
    """
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in iter_python_files()
        if API_DIR in path.parts
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "custom_components" in node.value
    ]
    assert not offenders, f"the client names its install path: {offenders}"


def test_the_client_stays_independent_of_home_assistant() -> None:
    """
    The client talks to a charger, not to Home Assistant.

    This is what earns api/ its exemption from the domain-literal guards:
    the "wattpilot" strings in there name the product and its protocol file,
    and the integration domain cannot appear because the package has no idea
    the domain exists. Keep it that way and the client stays testable on its
    own -- and reusable outside this integration, which is how it arrived.
    """
    offenders = [
        f"{path.name} -> {module}"
        for path in iter_python_files()
        if API_DIR in path.parts
        for module in _imports_of(path)
        if module.split(".")[0] == "homeassistant" or module.lstrip(".") == "const"
    ]
    assert not offenders, f"the client reached into Home Assistant: {offenders}"


# Every exception class Home Assistant turns into text a user reads.
_USER_FACING_ERRORS = frozenset(
    {
        "HomeAssistantError",
        "ServiceValidationError",
        "ConfigEntryError",
        "ConfigEntryNotReady",
        "ConfigEntryAuthFailed",
    }
)
_PLACEHOLDER = re.compile(r"{(\w+)}")


def _raised_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def test_every_user_facing_error_is_translated() -> None:
    """
    An error Home Assistant shows a user carries a translation key, and the
    placeholders it passes are the ones its message uses.

    quality_scale.yaml declared exception-translations done while every
    error the hub raised was an English f-string, and nothing checked the
    claim; the existing key test only resolves keys that are present.

    Fix: raise with translation_domain=DOMAIN, translation_key=... and
    translation_placeholders matching the message in strings.json.

    Only a direct `raise X(...)` is seen. An error built in a helper and
    raised by name slips past, which is why each one is spelled out.
    """
    messages = {
        key: entry["message"]
        for key, entry in json.loads(
            (PACKAGE / "strings.json").read_text(encoding="utf-8")
        )["exceptions"].items()
    }
    problems: list[str] = []
    for path in iter_python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            call = node.exc
            if _raised_name(call) not in _USER_FACING_ERRORS:
                continue
            where = f"{path.name}:{node.lineno}"
            keywords = {k.arg: k.value for k in call.keywords}
            key = keywords.get("translation_key")
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                problems.append(f"{where}: no literal translation_key")
                continue
            given = keywords.get("translation_placeholders")
            passed = (
                {k.value for k in given.keys if isinstance(k, ast.Constant)}
                if isinstance(given, ast.Dict)
                else set()
            )
            wanted = set(_PLACEHOLDER.findall(messages.get(key.value, "")))
            if passed != wanted:
                problems.append(
                    f"{where}: {key.value} passes {sorted(passed)}, "
                    f"its message uses {sorted(wanted)}"
                )
    assert not problems, "untranslated or mismatched errors:\n" + "\n".join(problems)


def test_the_client_is_only_imported_by_hub() -> None:
    """One import site, still. The client living in this repository rather
    than in site-packages changed where it is, not why it stays behind one
    module: everything else talks to the charger through the hub.
    Fix: route through hub.py."""
    offenders = [
        path.name
        for path in iter_python_files()
        if path.name != "hub.py"
        and any(m.lstrip(".").split(".")[0] == API_DIR for m in _imports_of(path))
    ]
    assert not offenders, f"client imported outside hub.py: {offenders}"


def test_platform_modules_do_not_import_each_other() -> None:
    """Platforms stay independent. Fix: shared code goes to entity/descriptions."""
    offenders = [
        f"{path.name} -> {module}"
        for path in iter_python_files()
        if path.name in PLATFORM_MODULES
        for module in _imports_of(path)
        if module.lstrip(".") + ".py" in PLATFORM_MODULES - {path.name}
    ]
    assert not offenders, f"platform cross-imports: {offenders}"


def test_file_size_budget() -> None:
    """God modules never shrink on their own. Fix: split the file, or if you
    consciously extend the table, discuss raising its frozen entry."""
    for path in iter_python_files():
        lines = len(path.read_text(encoding="utf-8").splitlines())
        budget = SIZE_BUDGET.get(path.name, SIZE_LIMIT_DEFAULT)
        assert lines <= budget, f"{path.name}: {lines} lines > budget {budget}"
        if path.name in SIZE_BUDGET and lines < budget * SHRINK_SLACK:
            raise AssertionError(
                f"{path.name} shrank to {lines} lines — lower its budget "
                f"entry ({budget}) so the slack cannot be spent again"
            )


def test_cluster_size_budget() -> None:
    """A unit that was split into several files is still one unit.

    Fix: shrink it, or discuss raising the entry -- but not by splitting
    again, which is the move this guard exists to catch.
    """
    by_name = {path.name: path for path in iter_python_files()}
    for cluster, (names, budget) in CLUSTER_BUDGET.items():
        missing = [name for name in names if name not in by_name]
        assert not missing, (
            f"cluster {cluster!r} names file(s) that no longer exist: {missing}. "
            "A renamed or deleted member makes this guard measure less than it "
            "claims -- update the entry."
        )
        total = sum(
            len(by_name[name].read_text(encoding="utf-8").splitlines())
            for name in names
        )
        assert total <= budget, (
            f"cluster {cluster!r}: {total} lines across {len(names)} files "
            f"> budget {budget}"
        )
        if total < budget * SHRINK_SLACK:
            raise AssertionError(
                f"cluster {cluster!r} shrank to {total} lines — lower its "
                f"budget entry ({budget}) so the slack cannot be spent again"
            )


def _complexities_over(threshold: int) -> dict[str, int]:
    from radon.complexity import cc_visit  # noqa: PLC0415 -- scoped on purpose

    return {
        f"{path.name}::{block.fullname}": block.complexity
        for path in iter_python_files()
        for block in cc_visit(path.read_text(encoding="utf-8"))
        if block.complexity > threshold
    }


def test_the_snapshot_helper_sees_what_the_ratchet_sees() -> None:
    """The helper read only the top-level package; regenerating the table
    with it dropped every api/ entry (audit A19-12)."""
    assert complexity_snapshot.snapshot() == _complexities_over(
        complexity_snapshot.THRESHOLD
    )


def test_complexity_ratchet() -> None:
    """Cyclomatic complexity is an exact ratchet: going up fails, going down
    demands the table be updated. Fix: simplify, or update COMPLEXITY_TABLE
    to the LOWER value. Regenerate: python scripts/complexity_snapshot.py"""
    threshold = 5
    # function qualified name -> frozen cyclomatic complexity (> threshold)
    # populated by scripts/complexity_snapshot.py
    complexity_table: dict[str, int] = {
        "descriptions.py::firmware_supported": 7,
        "redaction.py::scrub": 9,
        # 7 since the `ts` rule (audit VA-07): one more branch in what is
        # a dispatch table written as code, not added logic.
        "redaction.py::sanitize_property": 6,
        "sensor.py::WattpilotSensor._apply_value": 8,
        "services.py::_hub_for_device": 6,
        # api/: frozen at what the client had when it was adopted. Every one
        # of these is a dispatch table (message types, property keys, JSON
        # types) rather than a branchy algorithm -- see the noqa comments at
        # each. The point of freezing them is that they cannot grow further
        # without someone saying so in a diff.
        "auth.py::_bcryptjs_base64_encode": 6,
        "client.py::Wattpilot._coerce_to_json_type": 17,
        "connection.py::Connection._read_frames": 6,
        "connection.py::Connection._message_loop": 6,
        "client.py::Wattpilot._handle_message": 11,
        "client.py::Wattpilot._on_response": 6,
        "client.py::Wattpilot._update_property": 6,
        "definition.py::validate_api_definition": 12,
        "definition.py::load_api_definition": 8,
    }
    actual = _complexities_over(threshold)
    for name, complexity in actual.items():
        frozen = complexity_table.get(name)
        assert frozen is not None, (
            f"{name} exceeds cc {threshold} (cc={complexity}) and has no "
            "frozen entry — simplify it or freeze it consciously"
        )
        assert complexity <= frozen, f"{name}: cc {complexity} > frozen {frozen}"
        assert complexity >= frozen, (
            f"{name}: cc {complexity} < frozen {frozen} — ratchet the table down"
        )
    stale = set(complexity_table) - set(actual)
    assert not stale, f"stale complexity entries (function simplified/moved): {stale}"


CI_WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
HACS_MANIFEST = Path(__file__).parent.parent / "hacs.json"
RELEASE_WORKFLOW = CI_WORKFLOW.parent / "release.yml"


# Word for word: a substring check stayed green with the arguments reversed,
# with `|| true` and with `echo` in front (reaudit RF-D-05).
ANCESTRY_CHECK = 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main'


def _release_problems(workflow: str) -> list[str]:
    release = yaml.safe_load(workflow)["jobs"]["release"]
    steps = release["steps"]
    checkout = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    problems = []
    if checkout.get("with", {}).get("fetch-depth") != 0:
        problems.append("no history to check against")
    ancestry = [
        index
        for index, step in enumerate(steps)
        if str(step.get("run", "")).strip() == ANCESTRY_CHECK
    ]
    if not ancestry:
        return [*problems, "the release job does not check the tag is on main"]
    publish = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("softprops/action-gh-release")
    )
    # After the publish step, or allowed to fail, it would check nothing.
    if ancestry[0] > publish:
        problems.append("the tag is checked after it is published")
    if steps[ancestry[0]].get("continue-on-error") or release.get("continue-on-error"):
        problems.append("the check may fail")
    return problems


def test_a_release_is_only_cut_from_main() -> None:
    """
    Tags come from main only -- a project rule the workflow did not check.

    A tag pushed from a branch before its merge published code main does
    not contain, and HACS offers that to everyone (audit A18-31). Fix: keep
    the ancestry check in the release job, with the full history checked out.
    """
    assert _release_problems(RELEASE_WORKFLOW.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "broken",
    [
        'git merge-base --is-ancestor origin/main "$GITHUB_SHA"',
        ANCESTRY_CHECK + " || true",
        "echo " + ANCESTRY_CHECK,
    ],
    ids=["reversed", "or_true", "echo"],
)
def test_the_release_guard_can_actually_fail(broken: str) -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count(ANCESTRY_CHECK) == 1
    assert _release_problems(workflow.replace(ANCESTRY_CHECK, broken)) != []


def test_ci_runs_the_same_gates_as_check_sh() -> None:
    """CI and the local script must not drift. Fix: CI calls check.sh."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/check.sh" in workflow
    assert "hassfest" in workflow
    assert "hacs/action" in workflow


def _runs_pytest(command: str) -> bool:
    """A line that runs the suite -- not one that only installs the plugin."""
    return any(
        words[:1] == ["pytest"] or words[1:3] == ["-m", "pytest"]
        for words in (line.split() for line in command.splitlines())
    )


def _jobs_pinning_and_testing(jobs: dict[str, Any], pin: str) -> list[str]:
    # Structural, not a substring scan of the file: the pin and the test run
    # have to sit in the SAME job's run steps. A commented-out pin cannot
    # satisfy this -- the YAML parser drops comments before we look. And
    # "pytest" alone was found in pytest-homeassistant-custom-component on
    # the install line, so a job without its test step passed (A18-30).
    return [
        name
        for name, job in jobs.items()
        for commands in [[step.get("run", "") for step in job.get("steps", [])]]
        if any(pin in command for command in commands)
        and any(_runs_pytest(command) for command in commands)
    ]


def test_the_minimum_ha_guard_needs_a_test_run_not_an_install() -> None:
    install = (
        'pip install --group dev "homeassistant==2026.9.0" '
        '"pytest-homeassistant-custom-component==0.13.363"'
    )
    only_install = {"minimum": {"steps": [{"run": install}]}}
    with_tests = {
        "minimum": {"steps": [{"run": install}, {"run": "python3 -m pytest"}]}
    }
    assert _jobs_pinning_and_testing(only_install, "homeassistant==2026.9.0") == []
    assert _jobs_pinning_and_testing(with_tests, "homeassistant==2026.9.0") == [
        "minimum"
    ]


def test_ci_actually_runs_the_declared_minimum_home_assistant() -> None:
    """
    The minimum HA version in hacs.json must be a version CI really runs.

    Declaring one and testing another is not a promise, it is a guess --
    and it cost us once already: hacs.json said 2026.7.0 while every run
    used the newest release, so nothing noticed that DeviceEntry gained
    `config_entry_id` only in 2026.8.0. All four actions raised
    AttributeError on the version we told HACS to allow, with a green
    suite. Fix: pin `homeassistant==<the hacs.json version>` in a CI job
    (pytest-homeassistant-custom-component pins one HA release each, so
    its version must match or pip fails the job loudly).
    """
    declared = json.loads(HACS_MANIFEST.read_text(encoding="utf-8"))["homeassistant"]
    pin = f"homeassistant=={declared}"
    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    pinned_and_tested = _jobs_pinning_and_testing(jobs, pin)
    assert pinned_and_tested, (
        f"hacs.json requires HA {declared}, but no CI job both pins "
        f"'{pin}' and runs pytest with it"
    )


def _enum_signature(enum: object) -> object:
    """Compare enums by what they map, not by which object they are.

    Audit VA-11: comparing `id(enum)` let a behaviourally identical copy of
    CAR_STATE_ENUM pass as a different sensor -- the guard would have missed
    the very duplicate it exists to catch.
    """
    if not isinstance(enum, dict):
        return None
    return tuple(sorted((repr(key), value) for key, value in enum.items()))


def _rendering_signature(description: object) -> tuple[object, ...]:
    """Everything that decides what a sensor ends up displaying."""
    return (
        *(
            getattr(description, field, None)
            for field in (
                "charger_key",
                "value_index",
                "cards_index",
                "namespace_value",
            )
        ),
        _enum_signature(getattr(description, "enum", None)),
    )


def test_no_two_sensors_render_the_same_thing() -> None:
    """
    Two descriptions that convert identically are one entity too many.

    car_connected and car_state both read "car" through the same enum and
    were therefore literal duplicates, shipped that way because the fork had
    them and parity was read as "reproduce the uid", not "reproduce the
    value". Splitting one of them is a fix for that pair; this is the class.
    Adding a second view on a property is fine -- it just has to differ in
    how it renders, which is exactly what this compares.
    """
    from custom_components.wattpilot.descriptions import (  # noqa: PLC0415
        SENSOR_DESCRIPTIONS,
    )

    seen: dict[tuple[object, ...], str] = {}
    duplicates: list[str] = []
    for description in SENSOR_DESCRIPTIONS:
        signature = _rendering_signature(description)
        previous = seen.get(signature)
        if previous is not None:
            duplicates.append(f"{previous} == {description.uid_suffix}")
        seen[signature] = description.uid_suffix
    assert not duplicates, f"sensors that render identically: {duplicates}"


def test_the_duplicate_sensor_guard_can_actually_fail() -> None:
    """Counter-check: the signature must collapse for a real duplicate and
    stay distinct for the two sensors that legitimately share a property."""
    from dataclasses import replace  # noqa: PLC0415

    from custom_components.wattpilot.descriptions import (  # noqa: PLC0415
        SENSOR_DESCRIPTIONS,
    )

    by_uid = {d.uid_suffix: d for d in SENSOR_DESCRIPTIONS}
    car_state, car_connected = by_uid["car"], by_uid["car_connected"]
    assert _rendering_signature(car_state) != _rendering_signature(car_connected)

    # The pre-fix shape: same property, same enum, different uid.
    regressed = replace(car_connected, enum=car_state.enum)
    assert _rendering_signature(regressed) == _rendering_signature(car_state)

    # A copy of that enum, which audit VA-11 slipped past the first version:
    # same mapping, different object.
    copied = replace(car_connected, enum=dict(car_state.enum))
    assert _rendering_signature(copied) == _rendering_signature(car_state)

    # And the ten ID-chip sensors must stay legitimate: same property and
    # enum, told apart by their index alone.
    assert _rendering_signature(by_uid["cards_0"]) != _rendering_signature(
        by_uid["cards_1"]
    )


# The guards, and what each one holds. Adapted from the erosionsschutz kit's
# meta-guard: deleting a guard is otherwise a green diff, and nothing answers
# "what stops the old problems coming back" six months later. Indexed per
# function rather than per file, because this project keeps its guards in two
# files rather than one per rule.
GUARD_INDEX: dict[str, dict[str, str]] = {
    "test_guards.py": {
        "test_domain_literal_only_in_const": "domain string lives in const.py only",
        "test_domain_literal_confined_to_known_carriers": "no fourth domain carrier",
        "test_manifest_is_consistent": "manifest metadata stays as declared",
        "test_no_deprecated_home_assistant_call": (
            "no Home Assistant call that reports as deprecated"
        ),
        "test_every_user_facing_error_is_translated": (
            "every error a user reads is translated, with matching placeholders"
        ),
        "test_replay_fixtures_are_anonymized": (
            "recorded frames carry no owner identifiers"
        ),
        "test_the_test_environment_installs_what_the_manifest_declares": (
            "the tests run with the dependencies the manifest promises"
        ),
        "test_every_third_party_import_is_declared": (
            "nothing is imported that a user's install would not have"
        ),
        "test_no_manifest_requirement_is_unused": (
            "no dependency is installed for nothing"
        ),
        "test_no_device_identifier_reaches_the_log": (
            "no serial, host, title, entity ID or address-quoting error or "
            "traceback in the log"
        ),
        "test_the_minimum_ha_guard_needs_a_test_run_not_an_install": (
            "the minimum-HA guard is not satisfied by an install line"
        ),
        "test_the_snapshot_helper_sees_what_the_ratchet_sees": (
            "regenerating the complexity table keeps every entry"
        ),
        "test_the_release_guard_can_actually_fail": "counter-check for it",
        "test_the_log_check_knows_every_identifier_shape": (
            "the log check sees serials, IPv4 and IPv6"
        ),
        "test_the_log_check_lets_ordinary_output_pass": "counter-check for it",
        "test_the_mutation_run_keeps_every_logic_module": (
            "a module cannot drop out of the mutation run"
        ),
        "test_the_log_check_outlasts_home_assistant": (
            "the log check also reads what HA logs while it stops"
        ),
        "test_a_release_is_only_cut_from_main": (
            "a tag that is not on main publishes nothing"
        ),
        "test_the_identifier_guard_can_actually_fail": (
            "the identifier guard flags each kind of leak it claims to"
        ),
        "test_the_identifier_guard_lets_masked_values_through": (
            "masked values pass, so the guard is not fixed by deletion"
        ),
        "test_manifest_does_not_declare_what_home_assistant_ships": (
            "hassfest accepts the requirements"
        ),
        "test_the_two_declared_versions_agree": "one release, one number",
        "test_device_fixture_is_anonymized": "snapshot carries no owner identifiers",
        "test_the_client_is_only_imported_by_hub": ("the client keeps one import site"),
        "test_the_client_stays_independent_of_home_assistant": (
            "the client knows nothing about Home Assistant"
        ),
        "test_expensive_hashing_never_runs_on_the_event_loop": (
            "key derivation is handed to a thread, not called"
        ),
        "test_the_client_never_names_its_own_install_path": (
            "the client does not spell out where it is mounted"
        ),
        "test_platform_modules_do_not_import_each_other": "platforms stay independent",
        "test_file_size_budget": "no module grows past its frozen ceiling",
        "test_cluster_size_budget": (
            "a split unit stays measured as one, so splitting cannot buy room"
        ),
        "test_complexity_ratchet": "cyclomatic complexity only ever goes down",
        "test_ci_runs_the_same_gates_as_check_sh": "CI and check.sh do not drift",
        "test_ci_actually_runs_the_declared_minimum_home_assistant": (
            "the declared minimum HA version is really tested"
        ),
        "test_no_two_sensors_render_the_same_thing": "no two entities convert alike",
        "test_the_duplicate_sensor_guard_can_actually_fail": "counter-check for it",
        "test_every_guard_is_listed_in_the_index": "this index stays complete",
    },
    "test_comment_narration.py": {
        "test_no_comment_merely_narrates_the_adjacent_code": (
            "no comment only restates the line below it"
        ),
        "test_the_heuristic_flags_narration_and_passes_decisions": (
            "counter-check: the heuristic fires and does not over-fire"
        ),
    },
    "test_redaction_register.py": {
        "test_every_string_field_has_an_explicit_decision": (
            "a new device field forces a redaction decision"
        ),
        "test_no_register_entry_outlives_its_field": "register cannot drift",
        "test_every_decision_is_from_the_vocabulary": "readable carries its reason",
        "test_replaced_and_scrubbed_fields_really_are": (
            "the register cannot claim an absent protection"
        ),
        "test_no_field_is_registered_as_leaking": "no field knowingly leaks",
    },
}


def _test_functions(path: Path) -> set[str]:
    return {
        node.name
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def test_every_guard_is_listed_in_the_index() -> None:
    """Both directions on purpose: a deleted guard leaves an entry behind,
    an undocumented new one leaves a gap. Either is a silent change to what
    this suite protects."""
    tests_directory = Path(__file__).parent
    for file_name, expected in GUARD_INDEX.items():
        actual = _test_functions(tests_directory / file_name)
        assert set(expected) - actual == set(), (
            f"{file_name}: indexed guard(s) gone: {sorted(set(expected) - actual)}. "
            "If the protection is genuinely obsolete, drop the entry in the "
            "same commit and say in the message what replaced it."
        )
        assert actual - set(expected) == set(), (
            f"{file_name}: guard(s) missing from GUARD_INDEX: "
            f"{sorted(actual - set(expected))} -- add one line saying what each holds."
        )


# The log check in conftest.py is itself a guard; these keep it honest
# (post-check PC6-04).
@pytest.mark.parametrize(
    "text",
    [
        "123456",
        "333333",
        "192.168.1.50",
        "192.0.2.77",
        "10.0.0.7",
        "169.254.3.4",
        "2001:db8::5",
        "fe80::1",
    ],
)
def test_the_log_check_knows_every_identifier_shape(text: str) -> None:
    assert IDENTIFIERS.search(f"refused {text}: timeout")


@pytest.mark.parametrize(
    "text",
    ["192.0.2.1", "2026.10.0", "42.5", "12:34:56", "2026-10-11T08:54:56", "1234567"],
)
def test_the_log_check_lets_ordinary_output_pass(text: str) -> None:
    assert not IDENTIFIERS.search(f"at {text} done")


def test_the_log_check_outlasts_home_assistant() -> None:
    """hass comes in through auto_enable_custom_integrations, and autouse
    fixtures start in name order: only requesting the log fixtures first sets
    them up before hass and tears them down after its stop (PC6-04)."""
    tree = ast.parse(
        (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    )
    hook = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "auto_enable_custom_integrations"
    )
    assert [arg.arg for arg in hook.args.args] == [
        "no_identifier_reaches_the_log",
        "drop_serial_masks",
        "enable_custom_integrations",
    ]


# The modules whose logic the mutation run measures. Code moved out of one
# of them left the run unless the config followed by hand (post-check PC7-05).
MUTATED_MODULES = {"__init__.py", "hub.py", "sensor.py", "readings.py"}


def test_the_mutation_run_keeps_every_logic_module() -> None:
    import tomllib  # noqa: PLC0415 -- scoped on purpose, only this test needs it

    pyproject = tomllib.loads(
        (PACKAGE.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    mutated = {Path(path).name for path in pyproject["tool"]["mutmut"]["source_paths"]}
    assert mutated >= MUTATED_MODULES, f"missing: {MUTATED_MODULES - mutated}"

"""Guards: machine-enforced project rules. See tests/README.md for the index."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import yaml

from custom_components.wattpilot.redaction import DROP_KEYS, REPLACE

PACKAGE = Path(__file__).parent.parent / "custom_components" / "wattpilot"


# The client package. It arrived as a third-party copy and was exempt from
# the house rules while it was one; it is maintained here now, so it is not.
API_DIR = "api"


def iter_python_files() -> list[Path]:
    files = sorted(PACKAGE.rglob("*.py"))
    assert files, "package scan must never be empty (guard would be blind)"
    return files


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


def test_manifest_is_consistent() -> None:
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "wattpilot"
    assert manifest["iot_class"] == "local_push"
    # No wattpilot-api: the client lives in api/ now. bcrypt is what auth.py
    # needs; mqtt/shell/discovery were never adopted, and with them neither
    # aiomqtt, prompt-toolkit nor pydantic.
    assert manifest["requirements"] == [
        "packaging>=23.0",
        "websockets>=14.0",
        "bcrypt>=4.0",
    ]
    assert manifest["config_flow"] is True
    assert manifest["version"] == "0.1.0"
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
    "descriptions.py": 1230,  # the entity table is data, not logic (measured: 1206)
    # The charger protocol in one class: connection lifecycle, message
    # dispatch, property table and the typed write paths. Adopted at 1220
    # lines; splitting it is a separate decision, not a side effect of taking
    # it over. Raised to 1340 for the connection-lifecycle guards the
    # 2026-09-05 audit asked for (measured: 1337). Second raise in a row:
    # the next one splits the connection lifecycle out instead.
    "client.py": 1340,
}
SHRINK_SLACK = 0.85  # an entry >=15% below budget must be ratcheted down


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


def test_complexity_ratchet() -> None:
    """Cyclomatic complexity is an exact ratchet: going up fails, going down
    demands the table be updated. Fix: simplify, or update COMPLEXITY_TABLE
    to the LOWER value. Regenerate: python scripts/complexity_snapshot.py"""
    from radon.complexity import cc_visit  # noqa: PLC0415 -- scoped on purpose

    threshold = 5
    # function qualified name -> frozen cyclomatic complexity (> threshold)
    # populated by scripts/complexity_snapshot.py
    complexity_table: dict[str, int] = {
        "descriptions.py::firmware_supported": 7,
        "descriptions.py::filter_supported": 6,
        "redaction.py::scrub": 9,
        # 7 since the `ts` rule (audit VA-07): one more branch in what is
        # a dispatch table written as code, not added logic.
        "redaction.py::sanitize_property": 8,
        "sensor.py::WattpilotSensor": 8,
        "sensor.py::WattpilotSensor._apply_value": 17,
        "services.py::_hub_for_device": 6,
        # api/: frozen at what the client had when it was adopted. Every one
        # of these is a dispatch table (message types, property keys, JSON
        # types) rather than a branchy algorithm -- see the noqa comments at
        # each. The point of freezing them is that they cannot grow further
        # without someone saying so in a diff.
        "auth.py::_bcryptjs_base64_encode": 6,
        "client.py::Wattpilot._on_hello": 6,
        "client.py::Wattpilot.install_firmware_update": 7,
        "client.py::Wattpilot._coerce_to_json_type": 17,
        "client.py::Wattpilot._message_loop": 7,
        "client.py::Wattpilot._handle_message": 11,
        "client.py::Wattpilot._on_response": 7,
        "client.py::Wattpilot._update_property": 24,
        "definition.py::validate_api_definition": 13,
        "definition.py::load_api_definition": 11,
        "definition.py::get_child_property_value": 11,
    }
    actual: dict[str, int] = {}
    for path in iter_python_files():
        for block in cc_visit(path.read_text(encoding="utf-8")):
            if block.complexity > threshold:
                actual[f"{path.name}::{block.fullname}"] = block.complexity
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


def test_ci_runs_the_same_gates_as_check_sh() -> None:
    """CI and the local script must not drift. Fix: CI calls check.sh."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/check.sh" in workflow
    assert "hassfest" in workflow
    assert "hacs/action" in workflow


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
    # Structural, not a substring scan of the file: the pin and the test run
    # have to sit in the SAME job's run steps. A commented-out pin cannot
    # satisfy this -- the YAML parser drops comments before we look.
    pinned_and_tested = [
        name
        for name, job in jobs.items()
        for commands in [[step.get("run", "") for step in job.get("steps", [])]]
        if any(pin in command for command in commands)
        and any("pytest" in command for command in commands)
    ]
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
        "test_the_two_declared_versions_agree": "one release, one number",
        "test_device_fixture_is_anonymized": "snapshot carries no owner identifiers",
        "test_the_client_is_only_imported_by_hub": ("the client keeps one import site"),
        "test_the_client_stays_independent_of_home_assistant": (
            "the client knows nothing about Home Assistant"
        ),
        "test_the_client_never_names_its_own_install_path": (
            "the client does not spell out where it is mounted"
        ),
        "test_platform_modules_do_not_import_each_other": "platforms stay independent",
        "test_file_size_budget": "no module grows past its frozen ceiling",
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

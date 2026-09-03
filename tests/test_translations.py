"""Every translation_key resolves; en mirrors strings; de mirrors keys."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from custom_components.wattpilot.descriptions import (
    BUTTON_DESCRIPTIONS,
    NUMBER_DESCRIPTIONS,
    SELECT_DESCRIPTIONS,
    SENSOR_DESCRIPTIONS,
    SWITCH_DESCRIPTIONS,
    TIME_DESCRIPTIONS,
    UPDATE_DESCRIPTIONS,
)

PACKAGE = Path(__file__).parent.parent / "custom_components" / "wattpilot"
PLATFORM_TABLES = {
    "sensor": SENSOR_DESCRIPTIONS,
    "switch": SWITCH_DESCRIPTIONS,
    "number": NUMBER_DESCRIPTIONS,
    "select": SELECT_DESCRIPTIONS,
    "button": BUTTON_DESCRIPTIONS,
    "time": TIME_DESCRIPTIONS,
    "update": UPDATE_DESCRIPTIONS,
}


def load(name: str) -> dict:
    return json.loads((PACKAGE / name).read_text(encoding="utf-8"))


def test_every_translation_key_has_a_name() -> None:
    strings = load("strings.json")
    missing: list[str] = []
    for platform, table in PLATFORM_TABLES.items():
        for description in table:
            key = description.translation_key
            if key is None:
                continue
            entry = strings.get("entity", {}).get(platform, {}).get(key)
            if not entry or "name" not in entry:
                missing.append(f"{platform}.{key}")
    assert not missing, f"translation keys without names: {sorted(set(missing))}"


def test_en_is_identical_to_strings() -> None:
    assert load("translations/en.json") == load("strings.json")


def _keys(tree: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        out.add(path)
        if isinstance(value, dict):
            out |= _keys(value, path)
    return out


def test_de_mirrors_key_structure() -> None:
    assert _keys(load("translations/de.json")) == _keys(load("strings.json"))


def test_exception_translation_keys_resolve() -> None:
    """Discover and verify exception translation_key values resolve."""
    # Entity translation_keys from descriptions.py — these are not exceptions
    entity_keys: set[str] = {
        description.translation_key
        for desc_table in PLATFORM_TABLES.values()
        for description in desc_table
        if description.translation_key is not None
    }

    # Discover all translation_key values from Python files (excluding
    # descriptions.py, whose keys are entity keys already covered above).
    # Limitation: only string *literals* are matched. A key passed as a name
    # (translation_key=SOME_CONSTANT) is invisible here, which is why the
    # non-empty assertion below exists -- without it, a package that indirected
    # every key would make this test pass while checking nothing.
    found_keys: set[str] = set()
    for py_file in PACKAGE.rglob("*.py"):
        if py_file.name == "descriptions.py":
            continue

        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "translation_key"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found_keys.add(node.value.value)

    assert found_keys, (
        "no translation_key literals discovered at all -- the scan is broken or "
        "every key is now indirected; either way this test would silently check "
        "nothing, so it fails loudly instead"
    )

    # Whatever is not an entity key must resolve as an exception or as a
    # repair issue -- the third kind, added with the migration collision
    # notice. Each carries a different required field, so they are checked
    # separately rather than by mere presence of the key.
    strings = load("strings.json")
    exceptions = strings.get("exceptions", {})
    issues = strings.get("issues", {})

    def resolves(key: str) -> bool:
        if key in exceptions:
            return "message" in exceptions[key]
        if key in issues:
            return {"title", "description"} <= set(issues[key])
        return False

    missing = [key for key in sorted(found_keys - entity_keys) if not resolves(key)]

    assert not missing, (
        f"translation_key entries missing from strings.json: {missing} -- "
        "an exception needs a message, an issue a title and a description"
    )

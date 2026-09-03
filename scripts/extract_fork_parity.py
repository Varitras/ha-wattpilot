"""
Extract entity parity data from the fork's descriptions.py via AST.

Usage: python scripts/extract_fork_parity.py <path-to-fork-checkout>
Writes tests/fixtures/fork_parity.json. Run once; the JSON is committed so
tests stay offline. Fork pin: ruaan-deysel/ha-wattpilot @ 1decee7.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

LISTS = {
    "SENSOR_DESCRIPTIONS": "sensor",
    "SWITCH_DESCRIPTIONS": "switch",
    "NUMBER_DESCRIPTIONS": "number",
    "SELECT_DESCRIPTIONS": "select",
    "BUTTON_DESCRIPTIONS": "button",
    "TIME_DESCRIPTIONS": "time",
    "UPDATE_DESCRIPTIONS": "update",
}
FIELDS = (
    "native_unit_of_measurement",
    "device_class",
    "state_class",
    "entity_category",
    "entity_registry_enabled_default",
    "firmware",
    "variant",
)
OUT_KEYS = (
    "unit",
    "device_class",
    "state_class",
    "entity_category",
    "enabled_default",
    "firmware",
    "variant",
)


def literal(node: ast.expr) -> Any:  # noqa: ANN401 -- dynamic literal value
    """Resolve a keyword argument's AST node to its Python literal value."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):  # SensorDeviceClass.ENERGY -> "energy"
        return node.attr.lower()
    return None


def entry_from_call(call: ast.Call) -> dict[str, Any]:
    """Build one fixture entry from a WattpilotXEntityDescription(...) call."""
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    charger_key = literal(kw["charger_key"])
    uid = literal(kw["uid"]) if "uid" in kw else None
    entry: dict[str, Any] = {"uid": uid or charger_key, "charger_key": charger_key}
    for field, out in zip(FIELDS, OUT_KEYS, strict=True):
        entry[out] = literal(kw[field]) if field in kw else None
    if entry["enabled_default"] is None:
        entry["enabled_default"] = True
    return entry


def expand_starred_cards(comp: ast.ListComp) -> list[dict[str, Any]]:
    """Expand the `*[... for i in range(10)]` ID-chip generator to 10 entries."""
    template = entry_from_call(comp.elt)  # type: ignore[arg-type]
    # Sanity check, not runtime validation (one-off dev script).
    assert template["charger_key"] == "cards", "unexpected starred block"  # noqa: S101
    return [{**template, "uid": f"cards_{i}"} for i in range(10)]


def main() -> int:
    """Parse the fork's descriptions.py and write the frozen parity fixture."""
    fork = Path(sys.argv[1]) / "custom_components" / "wattpilot" / "descriptions.py"
    tree = ast.parse(fork.read_text(encoding="utf-8"))
    result: dict[str, list[dict[str, Any]]] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or node.value is None:
            continue
        name = getattr(node.target, "id", "")
        if name not in LISTS:
            continue
        entries: list[dict[str, Any]] = []
        for element in node.value.elts:  # type: ignore[attr-defined]
            if isinstance(element, ast.Starred):
                entries.extend(expand_starred_cards(element.value))  # type: ignore[arg-type]
            elif isinstance(element, ast.Call):
                entries.append(entry_from_call(element))
        result[LISTS[name]] = entries
    out = Path(__file__).parent.parent / "tests" / "fixtures" / "fork_parity.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes((json.dumps(result, indent=2) + "\n").encode("utf-8"))
    print(f"wrote {out} ({sum(len(v) for v in result.values())} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

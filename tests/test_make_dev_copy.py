"""The dev copy is a byte-level domain rename, nothing else."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from make_dev_copy import make_dev_copy

REPO = Path(__file__).parent.parent


RENAMED_FILES = {"manifest.json", "const.py", "services.yaml"}


def test_dev_copy_renames_domain_only(tmp_path: Path) -> None:
    target = make_dev_copy(REPO / "custom_components" / "wattpilot", tmp_path)
    assert target.name == "wattpilot_dev"
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "wattpilot_dev"
    assert manifest["name"] == "Fronius Wattpilot (dev)"
    # The manifest names the logger namespace for the UI's "enable debug
    # logging". It is the package path, so it moves with the folder -- the
    # dev copy pointed at the real integration's namespace until this.
    assert manifest["loggers"] == ["custom_components.wattpilot_dev"]
    const = (target / "const.py").read_text(encoding="utf-8")
    assert 'DOMAIN = "wattpilot_dev"' in const

    services = (target / "services.yaml").read_text(encoding="utf-8")
    assert "integration: wattpilot_dev" in services
    assert "integration: wattpilot\n" not in services

    # Everything else -- every file, not just *.py -- is byte-identical.
    for path in target.rglob("*"):
        if path.is_dir() or path.name in RENAMED_FILES:
            continue
        original = REPO / "custom_components" / "wattpilot" / path.relative_to(target)
        assert path.read_bytes() == original.read_bytes(), path.name


def test_the_renamed_copy_can_load_its_own_resources(tmp_path: Path) -> None:
    """
    The copy must find its API definition under its new package name.

    This is the failure the dev copy hit on a real install: the resource
    lookup carried the literal package path, so after the rename it raised
    ModuleNotFoundError and the entry never set up. Checking that the file
    was copied is not enough -- it always was; what broke was finding it.

    Run in a subprocess on purpose: importing a second copy of the package
    into this interpreter would leave it in sys.modules and in the
    custom_components namespace path for every later test.
    """
    import subprocess  # noqa: PLC0415 -- test-local
    import sys  # noqa: PLC0415 -- test-local

    parent = tmp_path / "custom_components"
    parent.mkdir()
    make_dev_copy(REPO / "custom_components" / "wattpilot", parent)

    program = (
        "from custom_components.wattpilot_dev.api.definition import "
        "load_api_definition;"
        "definition = load_api_definition();"
        "print(len(definition.properties))"
    )
    result = subprocess.run(  # noqa: S603 -- fixed program, no shell
        [sys.executable, "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0, "the copy loaded no properties"

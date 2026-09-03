"""
Create custom_components/wattpilot_dev for side-by-side testing.

Usage: python scripts/make_dev_copy.py [target_custom_components_dir]
Default target: the repo's own custom_components/ (gitignored name).
The domain literal lives in const.py, manifest.json, and services.yaml
(guard-enforced: test_guards.py fails if a fourth file starts carrying it),
so this is a three-file replace plus folder rename.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

DEV_DOMAIN = "wattpilot_dev"


def make_dev_copy(source: Path, target_parent: Path) -> Path:
    """Copy the integration to target_parent/wattpilot_dev with the domain renamed."""
    target = target_parent / DEV_DOMAIN
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))

    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["domain"] = DEV_DOMAIN
    manifest["name"] = "Fronius Wattpilot (dev)"
    # The logger entry is the package path, so it has to move with the folder.
    # Left alone, the dev copy offered "enable debug logging" for the real
    # integration's namespace instead of its own.
    manifest["loggers"] = [f"custom_components.{DEV_DOMAIN}"]
    manifest_path.write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))

    const_path = target / "const.py"
    const_path.write_bytes(
        const_path.read_text(encoding="utf-8")
        .replace('DOMAIN = "wattpilot"', f'DOMAIN = "{DEV_DOMAIN}"')
        .encode("utf-8")
    )

    services_path = target / "services.yaml"
    services_path.write_bytes(
        services_path.read_text(encoding="utf-8")
        .replace("integration: wattpilot", f"integration: {DEV_DOMAIN}")
        .encode("utf-8")
    )
    return target


if __name__ == "__main__":
    repo = Path(__file__).parent.parent
    source = repo / "custom_components" / "wattpilot"
    parent = Path(sys.argv[1]) if len(sys.argv) > 1 else repo / "custom_components"
    print(f"created {make_dev_copy(source, parent)}")

"""Loader and accessor for the Wattpilot YAML API definition."""

from __future__ import annotations

import importlib.resources as import_resources
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any

import yaml

_LOGGER = logging.getLogger(__name__)


@dataclass
class ApiDefinition:
    """Parsed representation of ``wattpilot.yaml``."""

    config: dict[str, Any] = field(default_factory=dict)
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)


def validate_api_definition(config: Any) -> dict[str, Any]:  # noqa: ANN401 -- charger values are dynamically typed
    """Validate top-level structure of the raw YAML config."""
    if not isinstance(config, dict):
        msg = "wattpilot.yaml must define a mapping at top level"
        raise TypeError(msg)
    if "messages" not in config or not isinstance(config["messages"], list):
        msg = "wattpilot.yaml must contain a list 'messages'"
        raise ValueError(msg)
    if "properties" not in config or not isinstance(config["properties"], list):
        msg = "wattpilot.yaml must contain a list 'properties'"
        raise ValueError(msg)

    for entry in config["messages"]:
        if not isinstance(entry, dict) or "key" not in entry:
            msg = "Each message entry must be a mapping with a 'key'"
            raise ValueError(msg)

    for prop in config["properties"]:
        if not isinstance(prop, dict) or "key" not in prop:
            msg = "Each property entry must be a mapping with a 'key'"
            raise ValueError(msg)

    return config


def _add_unique(d: dict[str, Any], k: str, v: Any) -> dict[str, Any]:  # noqa: ANN401 -- charger values are dynamically typed
    if k in d:
        _LOGGER.warning("About to add duplicate key %s to dictionary - skipping!", k)
    else:
        d[k] = v
    return d


def load_api_definition() -> ApiDefinition:
    """
    Load and parse ``wattpilot.yaml`` from package resources.

    Array and object properties keep their ``childProps`` entries as data;
    the adopted client's expansion of them into virtual properties had no
    caller here and was removed.
    """
    # Derived, never written down: the same code runs as
    # custom_components.wattpilot.api and, in the side-by-side development
    # copy, as custom_components.wattpilot_dev.api. A hardcoded path made the
    # dev copy fail setup with ModuleNotFoundError.
    package = __package__
    try:
        raw_text = (
            import_resources.files(f"{package}.resources")
            .joinpath("wattpilot.yaml")
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        data = pkgutil.get_data(package, "resources/wattpilot.yaml")
        if data is None:
            msg = "Could not load wattpilot.yaml"
            raise FileNotFoundError(msg) from None
        try:
            raw_text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = f"Failed to decode wattpilot.yaml as UTF-8: {exc.reason}"
            raise ValueError(msg) from exc

    api_def = ApiDefinition()

    try:
        api_def.config = validate_api_definition(yaml.safe_load(raw_text or "{}"))
        api_def.messages = {m["key"]: m for m in api_def.config["messages"]}

        for p in api_def.config["properties"]:
            api_def.properties = _add_unique(api_def.properties, p["key"], p)

    except yaml.YAMLError as exc:
        _LOGGER.fatal("Failed to parse wattpilot.yaml: %s", exc)
        raise

    return api_def

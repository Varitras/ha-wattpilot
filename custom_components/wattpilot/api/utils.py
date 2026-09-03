"""Shared utilities for JSON encoding."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


class JSONNamespaceEncoder(json.JSONEncoder):
    """JSON encoder that handles :class:`~types.SimpleNamespace` objects."""

    def default(self, o: object) -> Any:  # noqa: ANN401 -- charger values are dynamically typed
        """Return a JSON-serialisable form of o."""
        if isinstance(o, SimpleNamespace):
            return o.__dict__
        return super().default(o)


def value_to_json(value: Any) -> str:  # noqa: ANN401 -- charger values are dynamically typed
    """Serialize *value* to JSON, including :class:`~types.SimpleNamespace`."""
    return json.dumps(value, cls=JSONNamespaceEncoder)

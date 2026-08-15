"""Late-bound compatibility dependencies used by extracted application services."""

from __future__ import annotations

from typing import Any


class DynamicRuntime:
    """Resolve characterized launcher dependencies at call time."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

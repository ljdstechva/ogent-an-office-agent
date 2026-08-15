"""Load characterized legacy runtime fragments into the launcher namespace."""

from __future__ import annotations

from pathlib import Path
from typing import Any


FRAGMENT_NAMES = (
    "bootstrap",
    "runtime_basics",
    "preview_bridge",
    "state",
    "state_registry",
    "references",
    "reference_materialization",
    "preview_watch",
    "documents",
    "agent_adapters",
    "preview_confirmation",
    "turn_dispatch",
    "selection_dispatch",
    "lifecycle",
    "server_entrypoint",
)


def load_runtime(namespace: dict[str, Any]) -> None:
    """Execute fixed package-owned fragments in their characterized order."""
    fragment_root = Path(__file__).resolve().with_name("runtime")
    for name in FRAGMENT_NAMES:
        path = fragment_root / f"{name}.pyfrag"
        source = path.read_text(encoding="utf-8")
        exec(compile(source, str(path), "exec"), namespace, namespace)

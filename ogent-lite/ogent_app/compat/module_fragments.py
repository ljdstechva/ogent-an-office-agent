"""Load fixed, package-owned compatibility fragments into one module."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


SAFE_NAME = re.compile(r"[a-z0-9_]+\Z")


def load_module_fragments(
    namespace: dict[str, Any],
    group: str,
    names: tuple[str, ...],
) -> None:
    if not SAFE_NAME.fullmatch(group) or any(
        not SAFE_NAME.fullmatch(name) for name in names
    ):
        raise RuntimeError("Invalid compatibility fragment manifest.")
    root = Path(__file__).resolve().with_name("fragments") / group
    for name in names:
        path = root / f"{name}.pyfrag"
        source = path.read_text(encoding="utf-8")
        exec(compile(source, str(path), "exec"), namespace, namespace)

"""Central filesystem trust-boundary policy for Ogent workspaces."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path


WINDOWS_RESERVED_STEMS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class PathPolicyError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class WorkspacePathPolicy:
    """Normalize user paths and enforce containment without following intent."""

    def __init__(self, supported_upload_suffixes: Iterable[str]) -> None:
        self.supported_upload_suffixes = frozenset(
            str(suffix).casefold() for suffix in supported_upload_suffixes
        )

    @staticmethod
    def safe_component(value: str, *, maximum: int = 80) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-._")
        return cleaned[: max(1, int(maximum))] or "document"

    def safe_upload_filename(self, value: str) -> str:
        leaf = Path(str(value).replace("\\", "/")).name.strip()
        suffix = Path(leaf).suffix.casefold()
        if suffix not in self.supported_upload_suffixes:
            raise PathPolicyError(
                "Drop a .docx, .xlsx, .pptx, or .pdf file.",
                415,
            )
        stem = leaf[: -len(suffix)] if suffix else leaf
        stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "-", stem).strip(" .")
        if not stem:
            stem = "document"
        if stem.casefold() in WINDOWS_RESERVED_STEMS:
            stem = f"_{stem}"
        return f"{stem[:160]}{suffix}"

    @staticmethod
    def is_within(path: Path, root: Path) -> bool:
        try:
            Path(path).resolve(strict=False).relative_to(
                Path(root).resolve(strict=False)
            )
            return True
        except ValueError:
            return False

    @staticmethod
    def normalize_existing_file(
        raw_path: str,
        *,
        base_directory: Path | None = None,
    ) -> Path:
        value = str(raw_path).strip().strip('"').strip("'")
        if not value:
            raise PathPolicyError("Paste a document path.")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (base_directory or Path.cwd()) / path
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            raise PathPolicyError(f"File not found: {path}", 404) from None
        if not resolved.is_file():
            raise PathPolicyError(f"Not a file: {resolved}")
        return resolved

    @staticmethod
    def canonical_key(path: Path) -> str:
        return os.path.normcase(str(Path(path).resolve(strict=False)))

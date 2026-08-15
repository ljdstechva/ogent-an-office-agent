"""Typed OfficeCLI operations used by backend preflight and verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ogent_app.domain.capability import MutationCategory

from .executor import OfficeCliExecution, OfficeCliExecutionError, OfficeCliExecutor


MAX_BATCH_OPERATIONS = 500
SAFE_VIEW_MODES = {"stats", "outline", "issues", "text", "annotated", "forms"}


def _stable_path(value: str, *, allow_root: bool = False) -> str:
    path = str(value).strip()
    if (
        not path.startswith("/")
        or (path == "/" and not allow_root)
        or len(path) > 2_048
        or any(character in path for character in "\x00\r\n")
    ):
        raise OfficeCliExecutionError("An OfficeCLI stable path is invalid.")
    return path


class TypedOfficeCliGateway:
    def __init__(self, executor: OfficeCliExecutor) -> None:
        self.executor = executor

    @staticmethod
    def validate_document(document: Path) -> Path:
        candidate = Path(document)
        resolved = candidate.resolve(strict=True)
        if (
            candidate.is_symlink()
            or not resolved.is_file()
            or resolved.suffix.casefold() not in {".docx", ".xlsx", ".pptx"}
        ):
            raise OfficeCliExecutionError(
                "The active Office document failed verification."
            )
        return resolved

    def inspect_document(
        self,
        document: Path,
        *,
        mode: str = "stats",
    ) -> OfficeCliExecution:
        active = self.validate_document(document)
        selected_mode = str(mode).casefold()
        if selected_mode not in SAFE_VIEW_MODES:
            raise OfficeCliExecutionError(
                "The requested OfficeCLI inspection mode is not allowed."
            )
        return self.executor.execute(
            ["view", str(active), selected_mode, "--json"],
            cwd=active.parent,
        )

    def read_nodes(
        self,
        document: Path,
        paths: Iterable[str],
        *,
        depth: int = 2,
    ) -> tuple[OfficeCliExecution, ...]:
        active = self.validate_document(document)
        safe_depth = max(0, min(8, int(depth)))
        return tuple(
            self.executor.execute(
                [
                    "get",
                    str(active),
                    _stable_path(path),
                    "--depth",
                    str(safe_depth),
                    "--json",
                ],
                cwd=active.parent,
            )
            for path in paths
        )

    def query_nodes(
        self,
        document: Path,
        selector: str,
    ) -> OfficeCliExecution:
        active = self.validate_document(document)
        query = str(selector).strip()
        if (
            not query
            or len(query) > 4_096
            or any(character in query for character in "\x00\r\n")
        ):
            raise OfficeCliExecutionError("The OfficeCLI selector is invalid.")
        return self.executor.execute(
            ["query", str(active), query, "--json"],
            cwd=active.parent,
        )

    def apply_atomic_batch(
        self,
        document: Path,
        commands: list[dict[str, Any]],
    ) -> OfficeCliExecution:
        active = self.validate_document(document)
        if not commands or len(commands) > MAX_BATCH_OPERATIONS:
            raise OfficeCliExecutionError(
                "An atomic OfficeCLI batch must contain 1 to 500 operations."
            )
        encoded = json.dumps(
            commands,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self.executor.execute(
            [
                "batch",
                str(active),
                "--commands",
                encoded,
                "--json",
            ],
            cwd=active.parent,
            timeout_seconds=300,
        )

    def validate(self, document: Path) -> OfficeCliExecution:
        active = self.validate_document(document)
        return self.executor.execute(
            ["validate", str(active), "--json"],
            cwd=active.parent,
        )

    def refresh_fields(self, document: Path) -> OfficeCliExecution:
        active = self.validate_document(document)
        if active.suffix.casefold() != ".docx":
            raise OfficeCliExecutionError(
                "OfficeCLI field refresh is available only for DOCX."
            )
        return self.executor.execute(
            ["refresh", str(active), "--json"],
            cwd=active.parent,
            timeout_seconds=300,
        )

    @staticmethod
    def safe_result(execution: OfficeCliExecution) -> dict[str, Any]:
        output = execution.stdout or execution.stderr
        summary: dict[str, Any] = {
            "success": execution.exit_code == 0,
            "exit_code": execution.exit_code,
            "output_bytes": len(output.encode("utf-8")),
            "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        }
        try:
            payload = json.loads(execution.stdout)
        except (TypeError, ValueError):
            return summary
        if isinstance(payload, dict):
            summary["json_keys"] = sorted(str(key) for key in payload)[:100]
            for key in ("success", "count", "total", "valid", "format"):
                value = payload.get(key)
                if isinstance(value, (bool, int, float, str)):
                    summary[key] = value
        return summary

    @staticmethod
    def category(operation: str) -> MutationCategory:
        return {
            "inspect_document": MutationCategory.READ,
            "read_nodes": MutationCategory.READ,
            "query_nodes": MutationCategory.READ,
            "validate_document": MutationCategory.VALIDATION,
            "apply_atomic_batch": MutationCategory.MUTATION,
            "refresh_fields": MutationCategory.REFRESH,
        }.get(operation, MutationCategory.READ)

"""One-time import of pre-SQLite Ogent metadata before legacy cleanup."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .connection import SqliteDatabase, utc_now_iso
from .metadata_repository import MetadataRepository
from .turn_repository import TurnRepository
from .workspace_repository import WorkspaceRepository


MAX_RECENT_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MEMORY_BYTES = 256 * 1024 * 1024
WORKSPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


@dataclasses.dataclass(frozen=True)
class LegacyImportSummary:
    recent_documents: int = 0
    recovery_backups: int = 0
    workspaces: int = 0
    turns: int = 0


class LegacyMetadataImporter:
    def __init__(
        self,
        database: SqliteDatabase,
        workspaces: WorkspaceRepository,
        turns: TurnRepository,
        metadata: MetadataRepository,
    ) -> None:
        self.database = database
        self.workspaces = workspaces
        self.turns = turns
        self.metadata = metadata

    def import_all(
        self,
        *,
        recent_path: Path,
        backup_root: Path,
        session_memory_root: Path,
    ) -> LegacyImportSummary:
        self.database.initialize()
        recent = self._import_recents(Path(recent_path))
        backups = self._import_backups(Path(backup_root))
        workspaces, turns = self._import_session_memories(Path(session_memory_root))
        return LegacyImportSummary(recent, backups, workspaces, turns)

    @staticmethod
    def _path_source_key(prefix: str, path: Path) -> str:
        normalized = os.path.normcase(str(path.resolve(strict=False)))
        return f"{prefix}:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _read_json(path: Path, *, maximum_bytes: int) -> Any:
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > maximum_bytes
        ):
            raise ValueError("Legacy JSON input is missing, linked, or oversized.")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _mark_imported(
        connection: Any,
        source_key: str,
        detail: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO legacy_imports(source_key, imported_at, detail_json) "
            "VALUES (?, ?, ?) ON CONFLICT(source_key) DO NOTHING",
            (
                source_key,
                utc_now_iso(),
                json.dumps(
                    detail,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            ),
        )

    @staticmethod
    def _is_imported(connection: Any, source_key: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM legacy_imports WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            is not None
        )

    def _import_recents(self, path: Path) -> int:
        if not path.exists():
            return 0
        source_key = self._path_source_key("recent", path)
        with self.database.reader() as connection:
            if self._is_imported(connection, source_key):
                return 0
        try:
            payload = self._read_json(path, maximum_bytes=MAX_RECENT_BYTES)
        except (OSError, ValueError, json.JSONDecodeError):
            return 0
        if not isinstance(payload, list):
            return 0
        values = [
            str(item) for item in payload if isinstance(item, str) and str(item).strip()
        ][:200]
        base = dt.datetime.fromtimestamp(
            path.stat().st_mtime,
            tz=dt.timezone.utc,
        )
        with self.database.transaction() as connection:
            if self._is_imported(connection, source_key):
                return 0
            for index, value in enumerate(values):
                opened_at = (base - dt.timedelta(microseconds=index)).isoformat()
                resolved = str(Path(value).expanduser().resolve(strict=False))
                connection.execute(
                    "INSERT INTO recent_documents("
                    "canonical_path_key, source_path, last_opened_at"
                    ") VALUES (?, ?, ?) "
                    "ON CONFLICT(canonical_path_key) DO UPDATE SET "
                    "source_path = excluded.source_path, "
                    "last_opened_at = MAX("
                    "recent_documents.last_opened_at, excluded.last_opened_at"
                    ")",
                    (
                        self.metadata.canonical_path_key(resolved),
                        resolved,
                        opened_at,
                    ),
                )
            self._mark_imported(
                connection,
                source_key,
                {"kind": "recent_documents", "count": len(values)},
            )
        return len(values)

    def _import_backups(self, root: Path) -> int:
        if not root.is_dir() or root.is_symlink():
            return 0
        imported = 0
        for directory in sorted(root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest_path = directory / "manifest.json"
            source_key = self._path_source_key("backup", manifest_path)
            with self.database.reader() as connection:
                if self._is_imported(connection, source_key):
                    continue
            try:
                manifest = self._read_json(
                    manifest_path,
                    maximum_bytes=MAX_MANIFEST_BYTES,
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            backup_id = str(manifest.get("backup_id") or "")
            if backup_id != directory.name:
                continue
            required = {
                key: str(manifest.get(key) or "")
                for key in (
                    "backup_file",
                    "source_path",
                    "source_name",
                    "extension",
                    "sha256",
                    "application_version",
                    "created_at",
                    "expires_at",
                )
            }
            if any(not value for value in required.values()):
                continue
            raw_byte_size = manifest.get("byte_size")
            if raw_byte_size is None:
                continue
            try:
                byte_size = int(raw_byte_size)
            except (TypeError, ValueError):
                continue
            if byte_size < 0:
                continue
            with self.database.transaction() as connection:
                if self._is_imported(connection, source_key):
                    continue
                connection.execute(
                    "INSERT INTO recovery_backups("
                    "backup_id, backup_file, source_path, source_name, "
                    "extension, sha256, byte_size, application_version, "
                    "created_at, expires_at, pending_delete, delete_error, "
                    "manifest_path, imported_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(backup_id) DO UPDATE SET "
                    "pending_delete = excluded.pending_delete, "
                    "delete_error = excluded.delete_error, "
                    "manifest_path = excluded.manifest_path",
                    (
                        backup_id,
                        required["backup_file"],
                        required["source_path"],
                        required["source_name"],
                        required["extension"],
                        required["sha256"],
                        byte_size,
                        required["application_version"],
                        required["created_at"],
                        required["expires_at"],
                        int(bool(manifest.get("pending_delete"))),
                        (
                            str(manifest.get("delete_error"))
                            if manifest.get("delete_error")
                            else None
                        ),
                        str(manifest_path.resolve(strict=False)),
                        utc_now_iso(),
                    ),
                )
                self._mark_imported(
                    connection,
                    source_key,
                    {"kind": "recovery_backup", "backup_id": backup_id},
                )
            imported += 1
        return imported

    def _import_session_memories(self, root: Path) -> tuple[int, int]:
        if not root.is_dir() or root.is_symlink():
            return 0, 0
        workspace_count = 0
        turn_count = 0
        for path in sorted(root.rglob("memory.json")):
            if path.is_symlink():
                continue
            source_key = self._path_source_key("session-memory", path)
            with self.database.reader() as connection:
                if self._is_imported(connection, source_key):
                    continue
            try:
                payload = self._read_json(path, maximum_bytes=MAX_MEMORY_BYTES)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            raw_workspace_id = str(payload.get("session_id") or "")
            if not WORKSPACE_ID_PATTERN.fullmatch(raw_workspace_id):
                continue
            raw_turns = payload.get("turns")
            if not isinstance(raw_turns, list):
                continue
            workspace_id = self._available_workspace_id(
                raw_workspace_id,
                source_key,
            )
            prepared: list[tuple[dict[str, Any], Any]] = []
            for item in raw_turns:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "")
                text = item.get("text")
                if role not in {"user", "assistant"} or not isinstance(text, str):
                    continue
                prepared.append((item, self.turns.prepare_content(text)))
            with self.database.transaction() as connection:
                if self._is_imported(connection, source_key):
                    continue
                self.workspaces.create(
                    workspace_id,
                    connection=connection,
                )
                for sequence, (item, blob) in enumerate(prepared, start=1):
                    metadata = {
                        key: item.get(key)
                        for key in (
                            "document",
                            "attachment_ids",
                            "attachments",
                            "preview_selections",
                            "decisions",
                            "completed_actions",
                            "verification",
                        )
                        if item.get(key) not in (None, [], {}, ())
                    }
                    self.turns.append_prepared(
                        connection,
                        workspace_id,
                        str(item["role"]),
                        str(item["text"]),
                        blob,
                        provider=self._optional_text(item.get("provider")),
                        model=self._optional_text(item.get("model")),
                        effort=self._optional_text(item.get("effort")),
                        run_outcome=self._optional_text(item.get("run_outcome")),
                        metadata=metadata,
                        sequence=sequence,
                        created_at=(
                            str(item.get("timestamp"))
                            if item.get("timestamp")
                            else None
                        ),
                    )
                self._mark_imported(
                    connection,
                    source_key,
                    {
                        "kind": "session_memory",
                        "legacy_session_id": raw_workspace_id,
                        "workspace_id": workspace_id,
                        "turns": len(prepared),
                    },
                )
            workspace_count += 1
            turn_count += len(prepared)
        return workspace_count, turn_count

    def _available_workspace_id(
        self,
        requested: str,
        source_key: str,
    ) -> str:
        existing = self.workspaces.get(requested)
        if existing is None or self.turns.count(requested) == 0:
            return requested
        return hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

"""Lossless durable turns with paged retrieval."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import uuid
from typing import Any

from ogent_app.domain.workspace import TurnRecord

from .blob_store import BlobRef, ContentAddressedBlobStore
from .connection import SqliteDatabase, utc_now_iso


MAX_PAGE_SIZE = 200
DISPLAY_EXCERPT_CHARS = 2_000


@dataclasses.dataclass(frozen=True)
class TurnPage:
    items: tuple[TurnRecord, ...]
    next_sequence: int | None


class TurnRepository:
    def __init__(
        self,
        database: SqliteDatabase,
        blobs: ContentAddressedBlobStore,
    ) -> None:
        self.database = database
        self.blobs = blobs

    def prepare_content(self, raw_content: str) -> BlobRef:
        return self.blobs.put_text(raw_content)

    def append(
        self,
        workspace_id: str,
        role: str,
        raw_content: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        run_outcome: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnRecord:
        blob = self.prepare_content(raw_content)
        with self.database.transaction() as connection:
            return self.append_prepared(
                connection,
                workspace_id,
                role,
                raw_content,
                blob,
                provider=provider,
                model=model,
                effort=effort,
                run_outcome=run_outcome,
                metadata=metadata,
            )

    def append_prepared(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        role: str,
        raw_content: str,
        blob: BlobRef,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        run_outcome: str | None = None,
        metadata: dict[str, Any] | None = None,
        turn_id: str | None = None,
        sequence: int | None = None,
        created_at: str | None = None,
    ) -> TurnRecord:
        if role not in {"user", "assistant"}:
            raise ValueError("Durable turns must be user or assistant.")
        timestamp = created_at or utc_now_iso()
        identifier = turn_id or uuid.uuid4().hex
        turn_sequence = sequence
        if turn_sequence is None:
            turn_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 "
                    "FROM turns WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()[0]
            )
        safe_metadata = json.loads(
            json.dumps(metadata or {}, ensure_ascii=False, default=str)
        )
        connection.execute(
            "INSERT OR IGNORE INTO blobs("
            "id, sha256, byte_size, media_type, relative_path, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                blob.blob_id,
                blob.sha256,
                blob.byte_size,
                blob.media_type,
                blob.relative_path,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO turns("
            "id, workspace_id, sequence, role, raw_content_blob_id, "
            "display_excerpt, character_count, provider, model, effort, "
            "created_at, run_outcome, metadata_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                workspace_id,
                turn_sequence,
                role,
                blob.blob_id,
                raw_content[:DISPLAY_EXCERPT_CHARS],
                len(raw_content),
                provider,
                model,
                effort,
                timestamp,
                run_outcome,
                json.dumps(
                    safe_metadata,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return TurnRecord(
            turn_id=identifier,
            workspace_id=workspace_id,
            sequence=turn_sequence,
            role=role,
            raw_content_blob_id=blob.blob_id,
            display_excerpt=raw_content[:DISPLAY_EXCERPT_CHARS],
            character_count=len(raw_content),
            provider=provider,
            model=model,
            effort=effort,
            created_at=timestamp,
            run_outcome=run_outcome,
            metadata=safe_metadata,
        )

    def page(
        self,
        workspace_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 50,
    ) -> TurnPage:
        page_size = min(MAX_PAGE_SIZE, max(1, int(limit)))
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM turns WHERE workspace_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (workspace_id, max(0, int(after_sequence)), page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        visible = rows[:page_size]
        items = tuple(self._record(row) for row in visible)
        return TurnPage(
            items=items,
            next_sequence=items[-1].sequence if has_more and items else None,
        )

    def tail(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
    ) -> TurnPage:
        page_size = min(MAX_PAGE_SIZE, max(1, int(limit)))
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM turns WHERE workspace_id = ? "
                "ORDER BY sequence DESC LIMIT ?",
                (workspace_id, page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        visible = list(reversed(rows[:page_size]))
        items = tuple(self._record(row) for row in visible)
        return TurnPage(
            items=items,
            next_sequence=items[0].sequence if has_more and items else None,
        )

    def count(self, workspace_id: str) -> int:
        with self.database.reader() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM turns WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()[0]
            )

    def get(self, turn_id: str) -> TurnRecord | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE id = ?",
                (str(turn_id),),
            ).fetchone()
        return self._record(row) if row is not None else None

    def update_outcome(
        self,
        turn_id: str,
        *,
        run_outcome: str,
        metadata_update: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> TurnRecord:
        if connection is None:
            with self.database.transaction() as transaction:
                return self.update_outcome(
                    turn_id,
                    run_outcome=run_outcome,
                    metadata_update=metadata_update,
                    connection=transaction,
                )
        row = connection.execute(
            "SELECT * FROM turns WHERE id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        metadata = json.loads(str(row["metadata_json"] or "{}"))
        metadata.update(
            json.loads(
                json.dumps(
                    metadata_update or {},
                    ensure_ascii=False,
                    default=str,
                )
            )
        )
        connection.execute(
            "UPDATE turns SET run_outcome = ?, metadata_json = ? WHERE id = ?",
            (
                str(run_outcome),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                turn_id,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM turns WHERE id = ?",
            (turn_id,),
        ).fetchone()
        assert updated is not None
        return self._record(updated)

    def clear_workspace(
        self,
        workspace_id: str,
        *,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            "DELETE FROM runs WHERE workspace_id = ?",
            (workspace_id,),
        )
        connection.execute(
            "DELETE FROM turns WHERE workspace_id = ?",
            (workspace_id,),
        )

    def raw_content(self, turn_id: str) -> str:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT raw_content_blob_id FROM turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return self.blobs.read_text(str(row["raw_content_blob_id"]))

    @staticmethod
    def _record(row: sqlite3.Row) -> TurnRecord:
        return TurnRecord(
            turn_id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            sequence=int(row["sequence"]),
            role=str(row["role"]),
            raw_content_blob_id=str(row["raw_content_blob_id"]),
            display_excerpt=str(row["display_excerpt"]),
            character_count=int(row["character_count"]),
            provider=row["provider"],
            model=row["model"],
            effort=row["effort"],
            created_at=str(row["created_at"]),
            run_outcome=row["run_outcome"],
            metadata=json.loads(str(row["metadata_json"] or "{}")),
        )

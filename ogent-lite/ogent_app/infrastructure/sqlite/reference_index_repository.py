"""Lossless extracted-reference blobs with bounded FTS projections."""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterable

from ogent_app.domain.reference_index import (
    ReferenceIndexRecord,
    ReferenceIndexStatus,
    ReferenceSearchHit,
)

from .blob_store import BlobRef, ContentAddressedBlobStore
from .connection import SqliteDatabase, utc_now_iso


REFERENCE_CHUNK_CHARACTERS = 4_000
MAX_REFERENCE_SEARCH_RESULTS = 200


class ReferenceIndexRepository:
    def __init__(
        self,
        database: SqliteDatabase,
        blobs: ContentAddressedBlobStore,
    ) -> None:
        self.database = database
        self.blobs = blobs

    def begin(
        self,
        *,
        workspace_id: str,
        attachment_id: str,
        original_name: str,
        source_sha256: str,
    ) -> ReferenceIndexRecord:
        now = utc_now_iso()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO reference_indexes("
                "attachment_id, workspace_id, original_name, source_sha256, "
                "status, text_blob_id, character_count, chunk_count, "
                "error_code, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, NULL, 0, 0, NULL, ?, ?) "
                "ON CONFLICT(attachment_id) DO UPDATE SET "
                "workspace_id = excluded.workspace_id, "
                "original_name = excluded.original_name, "
                "source_sha256 = excluded.source_sha256, "
                "status = excluded.status, text_blob_id = NULL, "
                "character_count = 0, chunk_count = 0, error_code = NULL, "
                "updated_at = excluded.updated_at",
                (
                    attachment_id,
                    workspace_id,
                    original_name,
                    source_sha256,
                    ReferenceIndexStatus.INDEXING.value,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM reference_chunks WHERE attachment_id = ?",
                (attachment_id,),
            )
        record = self.get(attachment_id)
        assert record is not None
        return record

    def complete(
        self,
        attachment_id: str,
        text: str,
        *,
        partial: bool = False,
    ) -> ReferenceIndexRecord:
        canonical = str(text)
        blob = self.blobs.put_text(canonical)
        chunks = tuple(
            canonical[index : index + REFERENCE_CHUNK_CHARACTERS]
            for index in range(0, len(canonical), REFERENCE_CHUNK_CHARACTERS)
        )
        now = utc_now_iso()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT attachment_id FROM reference_indexes WHERE attachment_id = ?",
                (attachment_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(attachment_id)
            self._insert_blob(connection, blob, now)
            connection.execute(
                "DELETE FROM reference_chunks WHERE attachment_id = ?",
                (attachment_id,),
            )
            connection.executemany(
                "INSERT INTO reference_chunks("
                "id, attachment_id, chunk_index, text, character_count"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        uuid.uuid4().hex,
                        attachment_id,
                        index,
                        chunk.replace("\x00", " "),
                        len(chunk),
                    )
                    for index, chunk in enumerate(chunks)
                ),
            )
            connection.execute(
                "UPDATE reference_indexes SET status = ?, text_blob_id = ?, "
                "character_count = ?, chunk_count = ?, error_code = NULL, "
                "updated_at = ? WHERE attachment_id = ?",
                (
                    (
                        ReferenceIndexStatus.PARTIAL
                        if partial
                        else ReferenceIndexStatus.COMPLETE
                    ).value,
                    blob.blob_id,
                    len(canonical),
                    len(chunks),
                    now,
                    attachment_id,
                ),
            )
        record = self.get(attachment_id)
        assert record is not None
        return record

    def fail(
        self,
        attachment_id: str,
        error_code: str,
        *,
        cancelled: bool = False,
    ) -> ReferenceIndexRecord:
        now = utc_now_iso()
        safe_code = self._safe_error_code(error_code)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE reference_indexes SET status = ?, error_code = ?, "
                "updated_at = ? WHERE attachment_id = ?",
                (
                    (
                        ReferenceIndexStatus.CANCELLED
                        if cancelled
                        else ReferenceIndexStatus.FAILED
                    ).value,
                    safe_code,
                    now,
                    attachment_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(attachment_id)
        record = self.get(attachment_id)
        assert record is not None
        return record

    def get(self, attachment_id: str) -> ReferenceIndexRecord | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM reference_indexes WHERE attachment_id = ?",
                (attachment_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def search(
        self,
        workspace_id: str,
        attachment_ids: Iterable[str],
        query: str,
        *,
        limit: int = 20,
    ) -> tuple[ReferenceSearchHit, ...]:
        identifiers = tuple(
            dict.fromkeys(
                str(identifier).strip()
                for identifier in attachment_ids
                if str(identifier).strip()
            )
        )
        terms = re.findall(r"[\w.-]+", str(query), re.UNICODE)
        if not identifiers or not terms:
            return ()
        expression = " AND ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:20]
        )
        placeholders = ",".join("?" for _ in identifiers)
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT idx.attachment_id, idx.original_name, "
                "chunk.chunk_index, fts.text, "
                "bm25(reference_chunks_fts) AS rank "
                "FROM reference_chunks_fts AS fts "
                "JOIN reference_chunks AS chunk ON chunk.id = fts.chunk_id "
                "JOIN reference_indexes AS idx "
                "ON idx.attachment_id = chunk.attachment_id "
                "WHERE idx.workspace_id = ? "
                f"AND idx.attachment_id IN ({placeholders}) "
                "AND idx.status IN (?, ?) "
                "AND reference_chunks_fts MATCH ? "
                "ORDER BY rank, idx.attachment_id, chunk.chunk_index LIMIT ?",
                (
                    workspace_id,
                    *identifiers,
                    ReferenceIndexStatus.COMPLETE.value,
                    ReferenceIndexStatus.PARTIAL.value,
                    expression,
                    max(
                        1,
                        min(MAX_REFERENCE_SEARCH_RESULTS, int(limit)),
                    ),
                ),
            ).fetchall()
        return tuple(
            ReferenceSearchHit(
                str(row["attachment_id"]),
                str(row["original_name"]),
                int(row["chunk_index"]),
                str(row["text"]),
                float(row["rank"]),
            )
            for row in rows
        )

    def read_text(self, attachment_id: str) -> str:
        record = self.get(attachment_id)
        if record is None or record.text_blob_id is None:
            raise KeyError(attachment_id)
        return self.blobs.read_text(record.text_blob_id)

    def forget(self, attachment_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM reference_indexes WHERE attachment_id = ?",
                (attachment_id,),
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> ReferenceIndexRecord:
        return ReferenceIndexRecord(
            str(row["attachment_id"]),
            str(row["workspace_id"]),
            str(row["original_name"]),
            str(row["source_sha256"]),
            ReferenceIndexStatus(str(row["status"])),
            int(row["character_count"]),
            int(row["chunk_count"]),
            row["error_code"],
            str(row["created_at"]),
            str(row["updated_at"]),
            row["text_blob_id"],
        )

    @staticmethod
    def _safe_error_code(value: str) -> str:
        clean = "".join(
            character
            for character in str(value)
            if character.isalnum() or character in "._-"
        )[:128]
        return clean or "UnknownError"

    @staticmethod
    def _insert_blob(
        connection: sqlite3.Connection,
        blob: BlobRef,
        now: str,
    ) -> None:
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
                now,
            ),
        )

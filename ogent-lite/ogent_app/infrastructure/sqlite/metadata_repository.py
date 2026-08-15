"""Durable recents and imported recovery-manifest metadata."""

from __future__ import annotations

import os
from pathlib import Path

from .connection import SqliteDatabase, utc_now_iso


class MetadataRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    @staticmethod
    def canonical_path_key(source_path: str | Path) -> str:
        return os.path.normcase(
            str(Path(source_path).expanduser().resolve(strict=False))
        )

    def remember_document(
        self,
        source_path: str | Path,
        *,
        opened_at: str | None = None,
    ) -> None:
        value = str(Path(source_path).expanduser().resolve(strict=False))
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO recent_documents("
                "canonical_path_key, source_path, last_opened_at"
                ") VALUES (?, ?, ?) "
                "ON CONFLICT(canonical_path_key) DO UPDATE SET "
                "source_path = excluded.source_path, "
                "last_opened_at = excluded.last_opened_at",
                (
                    self.canonical_path_key(value),
                    value,
                    opened_at or utc_now_iso(),
                ),
            )

    def recent_documents(self, *, limit: int = 12) -> tuple[str, ...]:
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT source_path FROM recent_documents "
                "ORDER BY last_opened_at DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        return tuple(str(row["source_path"]) for row in rows)

    def recovery_count(self) -> int:
        with self.database.reader() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM recovery_backups").fetchone()[
                    0
                ]
            )

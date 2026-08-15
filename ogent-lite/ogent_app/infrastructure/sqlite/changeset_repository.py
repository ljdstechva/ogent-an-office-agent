"""Durable document changeset records."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable

from ogent_app.domain.verification import ChangesetRecord

from .connection import SqliteDatabase, utc_now_iso


class ChangesetRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def record(
        self,
        *,
        run_id: str,
        pre_revision_sha256: str,
        post_revision_sha256: str,
        affected_paths: Iterable[str],
        assertions: dict[str, Any],
        rollback_blob_id: str | None,
    ) -> str:
        identifier = uuid.uuid4().hex
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO changesets("
                "id, run_id, pre_revision_sha256, post_revision_sha256, "
                "affected_paths_json, assertions_json, rollback_blob_id, "
                "created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    run_id,
                    pre_revision_sha256,
                    post_revision_sha256,
                    json.dumps(
                        list(affected_paths),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        assertions,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    rollback_blob_id,
                    utc_now_iso(),
                ),
            )
        return identifier

    def get(self, changeset_id: str) -> ChangesetRecord | None:
        with self.database.reader() as connection:
            row = connection.execute(
                self._select_sql() + " WHERE changeset.id = ?",
                (str(changeset_id),),
            ).fetchone()
        return self._record(row) if row is not None else None

    def for_run(self, run_id: str) -> ChangesetRecord | None:
        with self.database.reader() as connection:
            row = connection.execute(
                self._select_sql() + " WHERE changeset.run_id = ? "
                "ORDER BY changeset.created_at DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
        return self._record(row) if row is not None else None

    def latest_for_workspace(
        self,
        workspace_id: str,
    ) -> ChangesetRecord | None:
        with self.database.reader() as connection:
            row = connection.execute(
                self._select_sql() + " WHERE run.workspace_id = ? "
                "ORDER BY changeset.created_at DESC LIMIT 1",
                (str(workspace_id),),
            ).fetchone()
        return self._record(row) if row is not None else None

    def record_undo(
        self,
        *,
        changeset_id: str,
        workspace_id: str,
        safety_blob_id: str,
        restored_sha256: str,
        validation: dict[str, Any],
    ) -> str:
        identifier = uuid.uuid4().hex
        with self.database.transaction() as connection:
            owned = connection.execute(
                "SELECT 1 FROM changesets AS changeset "
                "JOIN runs AS run ON run.id = changeset.run_id "
                "WHERE changeset.id = ? AND run.workspace_id = ?",
                (changeset_id, workspace_id),
            ).fetchone()
            if owned is None:
                raise KeyError(changeset_id)
            connection.execute(
                "INSERT INTO changeset_undos("
                "id, changeset_id, workspace_id, safety_blob_id, "
                "restored_sha256, validation_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    changeset_id,
                    workspace_id,
                    safety_blob_id,
                    restored_sha256,
                    json.dumps(
                        validation,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ),
                    utc_now_iso(),
                ),
            )
        return identifier

    @staticmethod
    def _select_sql() -> str:
        return (
            "SELECT changeset.*, run.workspace_id, "
            "undo.created_at AS undone_at, "
            "undo.restored_sha256 AS restored_sha256, "
            "undo.validation_json AS undo_validation_json "
            "FROM changesets AS changeset "
            "JOIN runs AS run ON run.id = changeset.run_id "
            "LEFT JOIN changeset_undos AS undo "
            "ON undo.changeset_id = changeset.id"
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> ChangesetRecord:
        return ChangesetRecord(
            changeset_id=str(row["id"]),
            run_id=str(row["run_id"]),
            workspace_id=str(row["workspace_id"]),
            pre_revision_sha256=str(row["pre_revision_sha256"]),
            post_revision_sha256=str(row["post_revision_sha256"]),
            affected_paths=tuple(
                str(path) for path in json.loads(str(row["affected_paths_json"]))
            ),
            assertions=dict(json.loads(str(row["assertions_json"]))),
            rollback_blob_id=(
                str(row["rollback_blob_id"])
                if row["rollback_blob_id"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            undone_at=(str(row["undone_at"]) if row["undone_at"] is not None else None),
            restored_sha256=(
                str(row["restored_sha256"])
                if row["restored_sha256"] is not None
                else None
            ),
            undo_validation=(
                dict(json.loads(str(row["undo_validation_json"])))
                if row["undo_validation_json"] is not None
                else {}
            ),
        )

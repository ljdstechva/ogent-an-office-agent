"""Durable workspace records."""

from __future__ import annotations

import sqlite3

from ogent_app.domain.workspace import WorkspaceRecord, WorkspaceStatus

from .connection import SqliteDatabase, utc_now_iso


class WorkspaceRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def create(
        self,
        workspace_id: str,
        *,
        selected_provider: str | None = None,
        selected_model: str | None = None,
        selected_effort: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> WorkspaceRecord:
        timestamp = utc_now_iso()
        if connection is not None:
            self._insert(
                connection,
                workspace_id,
                timestamp,
                selected_provider,
                selected_model,
                selected_effort,
            )
        else:
            with self.database.transaction() as transaction:
                self._insert(
                    transaction,
                    workspace_id,
                    timestamp,
                    selected_provider,
                    selected_model,
                    selected_effort,
                )
        record = self.get(workspace_id, connection=connection)
        assert record is not None
        return record

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        workspace_id: str,
        timestamp: str,
        provider: str | None,
        model: str | None,
        effort: str | None,
    ) -> None:
        connection.execute(
            "INSERT INTO workspaces("
            "id, conversation_generation, created_at, last_active_at, status, "
            "selected_provider, selected_model, selected_effort"
            ") VALUES (?, 1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (
                workspace_id,
                timestamp,
                timestamp,
                WorkspaceStatus.ACTIVE.value,
                provider,
                model,
                effort,
            ),
        )

    def get(
        self,
        workspace_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WorkspaceRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
        else:
            with self.database.reader() as reader:
                row = reader.execute(
                    "SELECT * FROM workspaces WHERE id = ?",
                    (workspace_id,),
                ).fetchone()
        return self._record(row) if row is not None else None

    def list_restorable(self) -> tuple[WorkspaceRecord, ...]:
        with self.database.reader() as reader:
            rows = reader.execute(
                "SELECT * FROM workspaces WHERE status != ? "
                "ORDER BY last_active_at DESC",
                (WorkspaceStatus.CLOSED.value,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def touch(
        self,
        workspace_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        timestamp = utc_now_iso()
        if connection is not None:
            connection.execute(
                "UPDATE workspaces SET last_active_at = ? WHERE id = ?",
                (timestamp, workspace_id),
            )
            return
        with self.database.transaction() as transaction:
            self.touch(workspace_id, connection=transaction)

    def set_status(
        self,
        workspace_id: str,
        status: WorkspaceStatus,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE workspaces SET status = ?, last_active_at = ? WHERE id = ?",
                (status.value, utc_now_iso(), workspace_id),
            )

    def increment_generation(
        self,
        workspace_id: str,
        *,
        connection: sqlite3.Connection,
    ) -> WorkspaceRecord:
        connection.execute(
            "UPDATE workspaces SET "
            "conversation_generation = conversation_generation + 1, "
            "last_active_at = ? WHERE id = ?",
            (utc_now_iso(), workspace_id),
        )
        record = self.get(workspace_id, connection=connection)
        if record is None:
            raise KeyError(workspace_id)
        return record

    @staticmethod
    def _record(row: sqlite3.Row) -> WorkspaceRecord:
        return WorkspaceRecord(
            workspace_id=str(row["id"]),
            document_id=row["document_id"],
            conversation_generation=int(row["conversation_generation"]),
            created_at=str(row["created_at"]),
            last_active_at=str(row["last_active_at"]),
            status=WorkspaceStatus(row["status"]),
            selected_provider=row["selected_provider"],
            selected_model=row["selected_model"],
            selected_effort=row["selected_effort"],
        )

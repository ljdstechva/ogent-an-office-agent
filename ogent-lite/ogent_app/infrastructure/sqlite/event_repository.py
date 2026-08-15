"""Durable typed run-event append and bounded replay."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from ogent_app.domain.workspace import RunEvent

from .connection import SqliteDatabase, utc_now_iso


MAX_REPLAY_EVENTS = 1_000


class EventRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def append(
        self,
        workspace_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> RunEvent:
        if connection is None:
            with self.database.transaction() as transaction:
                return self.append(
                    workspace_id,
                    event_type,
                    payload,
                    run_id=run_id,
                    connection=transaction,
                )
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 "
                "FROM run_events WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0]
        )
        event = RunEvent(
            event_id=uuid.uuid4().hex,
            workspace_id=workspace_id,
            run_id=run_id,
            sequence=sequence,
            event_type=str(event_type),
            payload=json.loads(json.dumps(payload, ensure_ascii=False, default=str)),
            created_at=utc_now_iso(),
        )
        connection.execute(
            "INSERT INTO run_events("
            "id, workspace_id, run_id, sequence, event_type, payload_json, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.workspace_id,
                event.run_id,
                event.sequence,
                event.event_type,
                json.dumps(
                    event.payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                event.created_at,
            ),
        )
        return event

    def replay(
        self,
        workspace_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> tuple[RunEvent, ...]:
        page_size = min(MAX_REPLAY_EVENTS, max(1, int(limit)))
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM run_events "
                "WHERE workspace_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (workspace_id, max(0, int(after_sequence)), page_size),
            ).fetchall()
        return tuple(
            RunEvent(
                event_id=str(row["id"]),
                workspace_id=str(row["workspace_id"]),
                run_id=row["run_id"],
                sequence=int(row["sequence"]),
                event_type=str(row["event_type"]),
                payload=json.loads(str(row["payload_json"])),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )

    def last_sequence(self, workspace_id: str) -> int:
        with self.database.reader() as connection:
            return int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM run_events "
                    "WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()[0]
            )

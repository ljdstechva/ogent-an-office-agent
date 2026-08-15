"""Lifecycle registry for one single-writer actor per durable workspace."""

from __future__ import annotations

import threading

from ogent_app.infrastructure.sqlite import (
    EventRepository,
    RunRepository,
    SqliteDatabase,
    TurnRepository,
    WorkspaceRepository,
)

from .workspace_actor import WorkspaceActor


class WorkspaceActorRegistry:
    def __init__(
        self,
        database: SqliteDatabase,
        workspaces: WorkspaceRepository,
        turns: TurnRepository,
        runs: RunRepository,
        events: EventRepository,
    ) -> None:
        self.database = database
        self.workspaces = workspaces
        self.turns = turns
        self.runs = runs
        self.events = events
        self.lock = threading.RLock()
        self.actors: dict[str, WorkspaceActor] = {}

    def get_or_create(self, workspace_id: str) -> WorkspaceActor:
        with self.lock:
            actor = self.actors.get(workspace_id)
            if actor is None:
                actor = WorkspaceActor(
                    workspace_id,
                    self.database,
                    self.workspaces,
                    self.turns,
                    self.runs,
                    self.events,
                )
                self.actors[workspace_id] = actor
            return actor

    def restore(self) -> tuple[WorkspaceActor, ...]:
        return tuple(
            self.get_or_create(record.workspace_id)
            for record in self.workspaces.list_restorable()
        )

    def stop_all(self) -> None:
        with self.lock:
            actors = list(self.actors.values())
            self.actors.clear()
        for actor in actors:
            actor.stop()

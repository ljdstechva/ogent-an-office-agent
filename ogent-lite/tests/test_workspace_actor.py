from __future__ import annotations

import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ogent_app.application.workspace_actor import (
    AcceptTurn,
    AppendTurn,
    GetWorkspaceState,
    RecoverInterruptedRun,
    TransitionRun,
    WorkspaceBusyError,
)
from ogent_app.application.workspace_actor_registry import WorkspaceActorRegistry
from ogent_app.domain.run import RunMode, ScopeMode
from ogent_app.domain.workspace import RunState
from ogent_app.infrastructure.sqlite import (
    ContentAddressedBlobStore,
    EventRepository,
    RunRepository,
    SqliteDatabase,
    TurnRepository,
    WorkspaceRepository,
)


class WorkspaceActorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = SqliteDatabase(self.root / "ogent.db")
        self.blobs = ContentAddressedBlobStore(self.root / "blobs")
        self.workspaces = WorkspaceRepository(self.database)
        self.turns = TurnRepository(self.database, self.blobs)
        self.runs = RunRepository(self.database)
        self.events = EventRepository(self.database)
        self.registry = self.new_registry()

    def tearDown(self) -> None:
        self.registry.stop_all()
        self.temporary.cleanup()

    def new_registry(self) -> WorkspaceActorRegistry:
        return WorkspaceActorRegistry(
            self.database,
            self.workspaces,
            self.turns,
            self.runs,
            self.events,
        )

    @staticmethod
    def accept(content: str = "Review the document.") -> AcceptTurn:
        return AcceptTurn(
            content=content,
            provider="codex",
            model="fixture",
            effort="automatic",
            mode=RunMode.REVIEW,
            scope=ScopeMode.WHOLE_DOCUMENT,
        )

    def test_actor_accepts_one_run_and_enforces_typed_state_machine(self) -> None:
        actor = self.registry.get_or_create("workspace-actor")
        accepted = actor.dispatch(self.accept())

        with self.assertRaises(WorkspaceBusyError):
            actor.dispatch(self.accept("Second request"))

        for state in (
            RunState.CAPABILITY_BOOTSTRAP,
            RunState.DOCUMENT_REFRESH,
            RunState.SCOPE_RESOLVED,
            RunState.PLAN_READY,
            RunState.EXECUTING,
            RunState.VERIFYING,
            RunState.PREVIEW_SYNC,
            RunState.COMPLETED,
        ):
            run = actor.dispatch(TransitionRun(accepted.run.run_id, state))
            self.assertEqual(run.state, state)

        snapshot = actor.dispatch(GetWorkspaceState())
        self.assertIsNone(snapshot.active_run)
        self.assertGreater(snapshot.last_event_sequence, 1)

    def test_acceptance_is_atomic_when_event_persistence_fails(self) -> None:
        actor = self.registry.get_or_create("workspace-atomic")
        with mock.patch.object(
            self.events,
            "append",
            side_effect=OSError("event store unavailable"),
        ):
            with self.assertRaisesRegex(OSError, "event store unavailable"):
                actor.dispatch(self.accept())

        self.assertEqual(
            self.turns.page("workspace-atomic").items,
            (),
        )
        with self.database.reader() as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE workspace_id = ?",
                ("workspace-atomic",),
            ).fetchone()[0]
        self.assertEqual(run_count, 0)

    def test_crash_restart_restores_history_and_fails_interrupted_run(self) -> None:
        actor = self.registry.get_or_create("workspace-restart")
        accepted = actor.dispatch(self.accept("Persistent request"))
        actor.dispatch(
            AppendTurn(
                role="assistant",
                content="Persistent response",
                run_outcome="working",
            )
        )
        self.registry.stop_all()

        self.registry = self.new_registry()
        restored = {item.workspace_id: item for item in self.registry.restore()}[
            "workspace-restart"
        ]
        state = restored.dispatch(GetWorkspaceState())
        self.assertEqual(state.active_run.run_id, accepted.run.run_id)
        failed = restored.dispatch(RecoverInterruptedRun())

        self.assertEqual(failed.state, RunState.FAILED)
        self.assertTrue(failed.verification["interrupted"])
        page = self.turns.page("workspace-restart")
        self.assertEqual(
            [self.turns.raw_content(turn.turn_id) for turn in page.items],
            ["Persistent request", "Persistent response"],
        )

    def test_concurrent_commands_are_serialized_without_lost_turns(self) -> None:
        actor = self.registry.get_or_create("workspace-concurrent")
        total = 160

        def append(index: int) -> None:
            actor.dispatch(
                AppendTurn(
                    role="user",
                    content=f"message-{index:03}",
                )
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(append, range(total)))

        page = self.turns.page(
            "workspace-concurrent",
            limit=200,
        )
        self.assertEqual(len(page.items), total)
        self.assertEqual(
            [turn.sequence for turn in page.items],
            list(range(1, total + 1)),
        )


if __name__ == "__main__":
    unittest.main()

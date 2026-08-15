from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ogent_app.application.run_planner import RunPlanner
from ogent_app.application.workspace_actor import (
    AcceptTurn,
    CheckpointRunStep,
    TransitionRunStep,
)
from ogent_app.application.workspace_actor_registry import WorkspaceActorRegistry
from ogent_app.domain.planning import (
    RunComplexity,
    RunPlan,
    RunStepState,
)
from ogent_app.domain.run import (
    RunContract,
    RunMode,
    ScopeMode,
)
from ogent_app.infrastructure.sqlite import (
    ContentAddressedBlobStore,
    EventRepository,
    RunRepository,
    SqliteDatabase,
    TurnRepository,
    WorkspaceRepository,
)


class RunPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = SqliteDatabase(self.root / "ogent.db")
        self.blobs = ContentAddressedBlobStore(self.root / "blobs")
        self.workspaces = WorkspaceRepository(self.database)
        self.turns = TurnRepository(self.database, self.blobs)
        self.runs = RunRepository(self.database)
        self.events = EventRepository(self.database)
        self.registry = WorkspaceActorRegistry(
            self.database,
            self.workspaces,
            self.turns,
            self.runs,
            self.events,
        )
        self.planner = RunPlanner()

    def tearDown(self) -> None:
        self.registry.stop_all()
        self.temporary.cleanup()

    def test_fast_and_structured_plans_are_deterministic(self) -> None:
        fast_contract = RunContract(
            RunMode.EDIT,
            ScopeMode.SELECTED_ONLY,
            ("/body/p[1]",),
        )
        first = self.planner.build(
            "Make this bold.",
            fast_contract,
            target_node_ids=("node-1",),
            has_document=True,
        )
        second = self.planner.build(
            "Make this bold.",
            fast_contract,
            target_node_ids=("node-1",),
            has_document=True,
        )
        broad = self.planner.build(
            "Review the entire document.",
            RunContract(RunMode.REVIEW, ScopeMode.WHOLE_DOCUMENT),
            has_document=True,
        )

        self.assertEqual(first.public(), second.public())
        self.assertEqual(first.complexity, RunComplexity.FAST_PATH)
        self.assertEqual(broad.complexity, RunComplexity.STRUCTURED)
        self.assertTrue(broad.coverage_requirement["require_complete_index"])
        self.assertTrue(first.steps[-2].mutates is False)
        self.assertEqual(
            sum(step.estimated_work_units for step in first.steps),
            first.estimated_work_units,
        )
        self.assertEqual(
            RunPlan.from_public(first.public()),
            first,
        )

    def test_plan_and_checkpoints_survive_repository_restart(self) -> None:
        actor = self.registry.get_or_create("plan-workspace")
        contract = RunContract(
            RunMode.EDIT,
            ScopeMode.SELECTED_ONLY,
            ("/body/p[1]",),
        )
        plan = self.planner.build(
            "Make this bold.",
            contract,
            target_node_ids=("node-1",),
            has_document=True,
        )
        accepted = actor.dispatch(
            AcceptTurn(
                "Make this bold.",
                "codex",
                "fixture",
                "automatic",
                contract.mode,
                contract.scope,
                plan,
            )
        )
        actor.dispatch(
            TransitionRunStep(
                accepted.run.run_id,
                "inspect",
                RunStepState.RUNNING,
            )
        )
        actor.dispatch(
            CheckpointRunStep(
                accepted.run.run_id,
                "inspect",
                {"partition": 2, "last_path": "/body/p[1]"},
            )
        )
        actor.dispatch(
            TransitionRunStep(
                accepted.run.run_id,
                "inspect",
                RunStepState.COMPLETED,
                verification={"receipt": "inspection-1"},
            )
        )

        restarted = RunRepository(SqliteDatabase(self.root / "ogent.db"))
        restored = restarted.get(accepted.run.run_id)
        steps = restarted.steps(accepted.run.run_id)

        self.assertEqual(restored.plan, plan)
        self.assertEqual(steps[0].state, RunStepState.COMPLETED)
        self.assertEqual(steps[0].checkpoint["partition"], 2)
        self.assertEqual(
            steps[0].verification["receipt"],
            "inspection-1",
        )
        self.assertEqual(
            restarted.resume_point(accepted.run.run_id).step.step_id,
            "execute",
        )

    def test_dependencies_and_run_scoped_step_ids_are_enforced(self) -> None:
        run_ids: list[str] = []
        for workspace_id in ("workspace-a", "workspace-b"):
            actor = self.registry.get_or_create(workspace_id)
            contract = RunContract(
                RunMode.ANALYZE,
                ScopeMode.ATTACHMENTS_ONLY,
            )
            plan = self.planner.build(
                "Summarize this file.",
                contract,
                attachment_ids=("attachment-1",),
                has_document=False,
            )
            accepted = actor.dispatch(
                AcceptTurn(
                    "Summarize this file.",
                    "codex",
                    "fixture",
                    "automatic",
                    contract.mode,
                    contract.scope,
                    plan,
                )
            )
            run_ids.append(accepted.run.run_id)

        with self.assertRaisesRegex(ValueError, "dependencies"):
            self.runs.transition_step(
                run_ids[0],
                "execute",
                RunStepState.RUNNING,
            )
        self.assertEqual(len(self.runs.steps(run_ids[0])), 3)
        self.assertEqual(len(self.runs.steps(run_ids[1])), 3)


if __name__ == "__main__":
    unittest.main()

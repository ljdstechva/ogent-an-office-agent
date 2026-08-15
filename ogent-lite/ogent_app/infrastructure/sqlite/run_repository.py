"""Durable run state and validated transitions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from ogent_app.domain.planning import (
    RunPlan,
    RunStep,
    RunStepRecord,
    RunStepState,
)
from ogent_app.domain.run import RunMode, ScopeMode
from ogent_app.domain.workspace import (
    RunRecord,
    RunState,
    validate_run_transition,
)

from .connection import SqliteDatabase, utc_now_iso


class RunRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def create(
        self,
        workspace_id: str,
        request_turn_id: str,
        *,
        mode: RunMode,
        scope: ScopeMode,
        run_id: str | None = None,
        plan: RunPlan | dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> RunRecord:
        if connection is None:
            with self.database.transaction() as transaction:
                return self.create(
                    workspace_id,
                    request_turn_id,
                    mode=mode,
                    scope=scope,
                    run_id=run_id,
                    plan=plan,
                    connection=transaction,
                )
        run_plan = (
            plan
            if isinstance(plan, RunPlan)
            else RunPlan.from_public(plan)
            if plan
            else None
        )
        if run_plan is not None and (
            run_plan.mode is not mode or run_plan.scope is not scope
        ):
            raise ValueError("The run plan does not match the run contract.")
        identifier = run_id or uuid.uuid4().hex
        timestamp = utc_now_iso()
        plan_value = run_plan.public() if run_plan is not None else {}
        connection.execute(
            "INSERT INTO runs("
            "id, workspace_id, request_turn_id, state, mode, scope, "
            "plan_json, dependencies_json, expected_mutations_json, "
            "coverage_target_json, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                workspace_id,
                request_turn_id,
                RunState.ACCEPTED.value,
                mode.value,
                scope.value,
                self._json(plan_value),
                self._json(plan_value.get("dependencies", {})),
                self._json(plan_value.get("expected_mutations", [])),
                self._json(plan_value.get("coverage_requirement", {})),
                timestamp,
                timestamp,
            ),
        )
        if run_plan is not None:
            connection.executemany(
                "INSERT INTO run_steps("
                "id, run_id, logical_id, sequence, state, description, "
                "target_node_ids_json, mutates, tool, verification_json, "
                "dependencies_json, proof, estimated_work_units, "
                "checkpoint_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, '{}')",
                (
                    (
                        f"{identifier}:{step.step_id}",
                        identifier,
                        step.step_id,
                        step.sequence,
                        RunStepState.PENDING.value,
                        step.description,
                        self._json(step.target_node_ids),
                        int(step.mutates),
                        step.tool,
                        self._json(step.dependencies),
                        step.proof,
                        step.estimated_work_units,
                    )
                    for step in run_plan.steps
                ),
            )
        record = self.get(identifier, connection=connection)
        assert record is not None
        return record

    def transition(
        self,
        run_id: str,
        target: RunState,
        *,
        verification: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> RunRecord:
        if connection is None:
            with self.database.transaction() as transaction:
                return self.transition(
                    run_id,
                    target,
                    verification=verification,
                    connection=transaction,
                )
        current = self.get(run_id, connection=connection)
        if current is None:
            raise KeyError(run_id)
        validate_run_transition(current.state, target)
        verified = verification if verification is not None else current.verification
        connection.execute(
            "UPDATE runs SET state = ?, verification_json = ?, "
            "updated_at = ? WHERE id = ?",
            (
                target.value,
                json.dumps(verified, ensure_ascii=False),
                utc_now_iso(),
                run_id,
            ),
        )
        updated = self.get(run_id, connection=connection)
        assert updated is not None
        return updated

    def get(
        self,
        run_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> RunRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        else:
            with self.database.reader() as reader:
                row = reader.execute(
                    "SELECT * FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            request_turn_id=str(row["request_turn_id"]),
            state=RunState(row["state"]),
            mode=RunMode(row["mode"]),
            scope=ScopeMode(row["scope"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            cancellation_requested=bool(row["cancellation_requested"]),
            verification=json.loads(str(row["verification_json"])),
            plan=(
                RunPlan.from_public(json.loads(str(row["plan_json"])))
                if json.loads(str(row["plan_json"]))
                else None
            ),
        )

    def active_for_workspace(self, workspace_id: str) -> RunRecord | None:
        terminal = (
            RunState.COMPLETED.value,
            RunState.FAILED.value,
            RunState.CANCELLED.value,
        )
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT id FROM runs WHERE workspace_id = ? "
                "AND state NOT IN (?, ?, ?) ORDER BY created_at DESC LIMIT 1",
                (workspace_id, *terminal),
            ).fetchone()
        return self.get(str(row["id"])) if row is not None else None

    def latest_for_workspace(self, workspace_id: str) -> RunRecord | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT id FROM runs WHERE workspace_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
        return self.get(str(row["id"])) if row is not None else None

    def request_cancellation(
        self,
        run_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> RunRecord:
        if connection is None:
            with self.database.transaction() as transaction:
                return self.request_cancellation(
                    run_id,
                    connection=transaction,
                )
        cursor = connection.execute(
            "UPDATE runs SET cancellation_requested = 1, updated_at = ? WHERE id = ?",
            (utc_now_iso(), run_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(run_id)
        record = self.get(run_id, connection=connection)
        assert record is not None
        return record

    def steps(
        self,
        run_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[RunStepRecord, ...]:
        if connection is not None:
            rows = connection.execute(
                "SELECT * FROM run_steps WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        else:
            with self.database.reader() as reader:
                rows = reader.execute(
                    "SELECT * FROM run_steps WHERE run_id = ? ORDER BY sequence",
                    (run_id,),
                ).fetchall()
        return tuple(self._step_record(row) for row in rows)

    def transition_step(
        self,
        run_id: str,
        step_id: str,
        target: RunStepState,
        *,
        checkpoint: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        error_code: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> RunStepRecord:
        if connection is None:
            with self.database.transaction() as transaction:
                return self.transition_step(
                    run_id,
                    step_id,
                    target,
                    checkpoint=checkpoint,
                    verification=verification,
                    error_code=error_code,
                    connection=transaction,
                )
        run = self.get(run_id, connection=connection)
        if run is None:
            raise KeyError(run_id)
        row = connection.execute(
            "SELECT * FROM run_steps WHERE run_id = ? AND logical_id = ?",
            (run_id, step_id),
        ).fetchone()
        if row is None:
            raise KeyError(step_id)
        current = RunStepState(str(row["state"]))
        if current is target:
            return self._step_record(row)
        allowed = {
            RunStepState.PENDING: {
                RunStepState.RUNNING,
                RunStepState.CANCELLED,
            },
            RunStepState.RUNNING: {
                RunStepState.COMPLETED,
                RunStepState.FAILED,
                RunStepState.CANCELLED,
            },
            RunStepState.FAILED: {RunStepState.RUNNING},
            RunStepState.CANCELLED: {RunStepState.RUNNING},
            RunStepState.COMPLETED: set(),
        }
        if target not in allowed[current]:
            raise ValueError(
                f"Run step cannot transition from {current.value} to {target.value}."
            )
        if target is RunStepState.RUNNING:
            if run.cancellation_requested:
                raise ValueError("A cancelled run cannot start another step.")
            dependencies = tuple(json.loads(str(row["dependencies_json"])))
            if dependencies:
                placeholders = ",".join("?" for _ in dependencies)
                completed = connection.execute(
                    "SELECT COUNT(*) FROM run_steps WHERE run_id = ? "
                    f"AND logical_id IN ({placeholders}) AND state = ?",
                    (
                        run_id,
                        *dependencies,
                        RunStepState.COMPLETED.value,
                    ),
                ).fetchone()[0]
                if int(completed) != len(dependencies):
                    raise ValueError("A run step cannot start before its dependencies.")
        now = utc_now_iso()
        checkpoint_value = (
            checkpoint
            if checkpoint is not None
            else json.loads(str(row["checkpoint_json"]))
        )
        verification_value = (
            verification
            if verification is not None
            else json.loads(str(row["verification_json"]))
        )
        started_at = now if target is RunStepState.RUNNING else row["started_at"]
        completed_at = now if target.terminal else None
        connection.execute(
            "UPDATE run_steps SET state = ?, checkpoint_json = ?, "
            "verification_json = ?, started_at = ?, completed_at = ?, "
            "error_code = ? WHERE run_id = ? AND logical_id = ?",
            (
                target.value,
                self._json(checkpoint_value),
                self._json(verification_value),
                started_at,
                completed_at,
                self._safe_error_code(error_code),
                run_id,
                step_id,
            ),
        )
        connection.execute(
            "UPDATE runs SET updated_at = ? WHERE id = ?",
            (now, run_id),
        )
        updated = connection.execute(
            "SELECT * FROM run_steps WHERE run_id = ? AND logical_id = ?",
            (run_id, step_id),
        ).fetchone()
        assert updated is not None
        return self._step_record(updated)

    def checkpoint_step(
        self,
        run_id: str,
        step_id: str,
        checkpoint: dict[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> RunStepRecord:
        if connection is None:
            with self.database.transaction() as transaction:
                return self.checkpoint_step(
                    run_id,
                    step_id,
                    checkpoint,
                    connection=transaction,
                )
        row = connection.execute(
            "SELECT * FROM run_steps WHERE run_id = ? AND logical_id = ?",
            (run_id, step_id),
        ).fetchone()
        if row is None:
            raise KeyError(step_id)
        if RunStepState(str(row["state"])) is not RunStepState.RUNNING:
            raise ValueError("Only a running step can record a checkpoint.")
        now = utc_now_iso()
        connection.execute(
            "UPDATE run_steps SET checkpoint_json = ? "
            "WHERE run_id = ? AND logical_id = ?",
            (self._json(checkpoint), run_id, step_id),
        )
        connection.execute(
            "UPDATE runs SET updated_at = ? WHERE id = ?",
            (now, run_id),
        )
        updated = connection.execute(
            "SELECT * FROM run_steps WHERE run_id = ? AND logical_id = ?",
            (run_id, step_id),
        ).fetchone()
        assert updated is not None
        return self._step_record(updated)

    def resume_point(self, run_id: str) -> RunStepRecord | None:
        return next(
            (
                record
                for record in self.steps(run_id)
                if record.state is not RunStepState.COMPLETED
            ),
            None,
        )

    @staticmethod
    def _safe_error_code(value: str | None) -> str | None:
        if value is None:
            return None
        clean = "".join(
            character
            for character in str(value)
            if character.isalnum() or character in "._-"
        )[:128]
        return clean or "UnknownError"

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _step_record(row: sqlite3.Row) -> RunStepRecord:
        step = RunStep(
            step_id=str(row["logical_id"]),
            sequence=int(row["sequence"]),
            description=str(row["description"]),
            target_node_ids=tuple(json.loads(str(row["target_node_ids_json"]))),
            mutates=bool(row["mutates"]),
            tool=row["tool"],
            proof=str(row["proof"]),
            dependencies=tuple(json.loads(str(row["dependencies_json"]))),
            estimated_work_units=int(row["estimated_work_units"]),
        )
        return RunStepRecord(
            step,
            RunStepState(str(row["state"])),
            json.loads(str(row["checkpoint_json"])),
            json.loads(str(row["verification_json"])),
            row["started_at"],
            row["completed_at"],
            row["error_code"],
        )

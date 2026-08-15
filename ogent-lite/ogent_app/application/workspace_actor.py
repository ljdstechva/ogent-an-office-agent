"""Single-writer actor for one durable Ogent workspace."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import queue
import threading
from typing import Any

from ogent_app.domain.planning import RunStepState
from ogent_app.domain.workspace import (
    RunRecord,
    RunState,
    WorkspaceRuntimeState,
    WorkspaceStatus,
)
from ogent_app.infrastructure.sqlite import (
    EventRepository,
    RunRepository,
    SqliteDatabase,
    TurnRepository,
    WorkspaceRepository,
)

from .workspace_commands import (
    AcceptTurn,
    AcceptedTurn,
    AppendTurn,
    CheckpointRunStep,
    ClearConversation,
    GetWorkspaceState,
    RecordEvent,
    RecoverInterruptedRun,
    RequestRunCancellation,
    SetPreviewState,
    TransitionRun,
    TransitionRunStep,
    UpdateTurnOutcome,
    WorkspaceBusyError,
    WorkspaceCommand,
    WorkspaceEnvelope,
)
from .workspace_recovery import (
    clear_workspace_conversation,
    recover_interrupted_run,
)


_STOP = object()


class WorkspaceActor:
    """Serialize all mutations for exactly one workspace."""

    def __init__(
        self,
        workspace_id: str,
        database: SqliteDatabase,
        workspaces: WorkspaceRepository,
        turns: TurnRepository,
        runs: RunRepository,
        events: EventRepository,
    ) -> None:
        self.workspace_id = workspace_id
        self.database = database
        self.workspaces = workspaces
        self.turns = turns
        self.runs = runs
        self.events = events
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            workspace = self.workspaces.create(workspace_id)
        self.state = WorkspaceRuntimeState(
            workspace=workspace,
            active_run=self.runs.active_for_workspace(workspace_id),
            last_event_sequence=self.events.last_sequence(workspace_id),
        )
        self.mailbox: queue.Queue[WorkspaceEnvelope | object] = queue.Queue()
        self.thread = threading.Thread(
            target=self._run,
            name=f"ogent-workspace-{workspace_id}",
            daemon=True,
        )
        self.thread.start()

    def dispatch(
        self,
        command: WorkspaceCommand,
        *,
        timeout: float = 10.0,
    ) -> Any:
        if not self.thread.is_alive():
            raise RuntimeError("The workspace actor is not running.")
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self.mailbox.put(WorkspaceEnvelope(command, future))
        return future.result(timeout=timeout)

    def _run(self) -> None:
        while True:
            envelope = self.mailbox.get()
            if envelope is _STOP:
                return
            assert isinstance(envelope, WorkspaceEnvelope)
            try:
                result = self._handle(envelope.command)
            except BaseException as exc:
                envelope.future.set_exception(exc)
            else:
                envelope.future.set_result(result)

    def _handle(self, command: WorkspaceCommand) -> Any:
        if isinstance(command, AcceptTurn):
            return self._accept(command)
        if isinstance(command, AppendTurn):
            turn = self.turns.append(
                self.workspace_id,
                command.role,
                command.content,
                provider=command.provider,
                model=command.model,
                effort=command.effort,
                run_outcome=command.run_outcome,
                metadata=command.metadata,
            )
            self._refresh_workspace()
            return turn
        if isinstance(command, TransitionRun):
            return self._transition(command)
        if isinstance(command, TransitionRunStep):
            return self._transition_step(command)
        if isinstance(command, CheckpointRunStep):
            return self._checkpoint_step(command)
        if isinstance(command, RequestRunCancellation):
            return self._request_cancellation(command)
        if isinstance(command, RecordEvent):
            event = self.events.append(
                self.workspace_id,
                command.event_type,
                command.payload,
                run_id=command.run_id,
            )
            self.state = dataclasses.replace(
                self.state,
                last_event_sequence=event.sequence,
            )
            return event
        if isinstance(command, UpdateTurnOutcome):
            turn = self.turns.update_outcome(
                command.turn_id,
                run_outcome=command.run_outcome,
                metadata_update=command.metadata_update,
            )
            self._refresh_workspace()
            return turn
        if isinstance(command, ClearConversation):
            return clear_workspace_conversation(self, command)
        if isinstance(command, SetPreviewState):
            self.state = dataclasses.replace(
                self.state,
                preview_state=command.state,
            )
            return self.state
        if isinstance(command, RecoverInterruptedRun):
            return recover_interrupted_run(self, command)
        if isinstance(command, GetWorkspaceState):
            return self.state
        raise TypeError(f"Unsupported workspace command: {type(command).__name__}")

    def _accept(self, command: AcceptTurn) -> AcceptedTurn:
        active = self.state.active_run
        if active is not None and not active.state.terminal:
            raise WorkspaceBusyError("This workspace already has an active run.")
        blob = self.turns.prepare_content(command.content)
        with self.database.transaction() as connection:
            turn = self.turns.append_prepared(
                connection,
                self.workspace_id,
                "user",
                command.content,
                blob,
                provider=command.provider,
                model=command.model,
                effort=command.effort,
                run_outcome=RunState.ACCEPTED.value,
                metadata=command.metadata,
            )
            run = self.runs.create(
                self.workspace_id,
                turn.turn_id,
                mode=command.mode,
                scope=command.scope,
                run_id=command.run_id,
                plan=command.plan,
                connection=connection,
            )
            event = self.events.append(
                self.workspace_id,
                "run.accepted",
                {
                    "run_id": run.run_id,
                    "turn_id": turn.turn_id,
                    "turn_sequence": turn.sequence,
                    "mode": run.mode.value,
                    "scope": run.scope.value,
                    "complexity": (
                        run.plan.complexity.value if run.plan is not None else None
                    ),
                },
                run_id=run.run_id,
                connection=connection,
            )
            self.workspaces.touch(
                self.workspace_id,
                connection=connection,
            )
        self._refresh_workspace(
            active_run=run,
            last_event_sequence=event.sequence,
        )
        return AcceptedTurn(turn, run, event.sequence)

    def _transition(self, command: TransitionRun) -> RunRecord:
        with self.database.transaction() as connection:
            run = self.runs.transition(
                command.run_id,
                command.target,
                verification=command.verification,
                connection=connection,
            )
            event = self.events.append(
                self.workspace_id,
                f"run.{command.target.value}",
                {
                    "run_id": run.run_id,
                    "state": run.state.value,
                    "verification": run.verification,
                },
                run_id=run.run_id,
                connection=connection,
            )
            self.workspaces.touch(
                self.workspace_id,
                connection=connection,
            )
        self._refresh_workspace(
            active_run=None if run.state.terminal else run,
            last_event_sequence=event.sequence,
        )
        return run

    def _transition_step(
        self,
        command: TransitionRunStep,
    ) -> Any:
        event_type = {
            RunStepState.RUNNING: "run.step.started",
            RunStepState.COMPLETED: "run.step.completed",
            RunStepState.FAILED: "run.step.failed",
            RunStepState.CANCELLED: "run.step.cancelled",
            RunStepState.PENDING: "run.step.pending",
        }[command.target]
        with self.database.transaction() as connection:
            step = self.runs.transition_step(
                command.run_id,
                command.step_id,
                command.target,
                checkpoint=command.checkpoint,
                verification=command.verification,
                error_code=command.error_code,
                connection=connection,
            )
            event = self.events.append(
                self.workspace_id,
                event_type,
                {
                    "run_id": command.run_id,
                    "step": step.public(),
                },
                run_id=command.run_id,
                connection=connection,
            )
            self.workspaces.touch(
                self.workspace_id,
                connection=connection,
            )
        self.state = dataclasses.replace(
            self.state,
            last_event_sequence=event.sequence,
        )
        return step, event

    def _checkpoint_step(
        self,
        command: CheckpointRunStep,
    ) -> Any:
        with self.database.transaction() as connection:
            step = self.runs.checkpoint_step(
                command.run_id,
                command.step_id,
                command.checkpoint,
                connection=connection,
            )
            event = self.events.append(
                self.workspace_id,
                "run.step.checkpoint",
                {
                    "run_id": command.run_id,
                    "step": step.public(),
                },
                run_id=command.run_id,
                connection=connection,
            )
            self.workspaces.touch(
                self.workspace_id,
                connection=connection,
            )
        self.state = dataclasses.replace(
            self.state,
            last_event_sequence=event.sequence,
        )
        return step, event

    def _request_cancellation(
        self,
        command: RequestRunCancellation,
    ) -> Any:
        with self.database.transaction() as connection:
            run = self.runs.request_cancellation(
                command.run_id,
                connection=connection,
            )
            event = self.events.append(
                self.workspace_id,
                "run.cancellation_requested",
                {"run_id": run.run_id},
                run_id=run.run_id,
                connection=connection,
            )
            self.workspaces.touch(
                self.workspace_id,
                connection=connection,
            )
        self._refresh_workspace(
            active_run=run,
            last_event_sequence=event.sequence,
        )
        return run, event

    def _refresh_workspace(
        self,
        *,
        active_run: RunRecord | None | object = ...,
        last_event_sequence: int | None = None,
    ) -> None:
        workspace = self.workspaces.get(self.workspace_id)
        assert workspace is not None
        self.state = dataclasses.replace(
            self.state,
            workspace=workspace,
            active_run=(self.state.active_run if active_run is ... else active_run),
            last_event_sequence=(
                self.state.last_event_sequence
                if last_event_sequence is None
                else last_event_sequence
            ),
        )

    def stop(self, *, mark_closed: bool = False) -> None:
        if mark_closed:
            self.workspaces.set_status(
                self.workspace_id,
                WorkspaceStatus.CLOSED,
            )
        if self.thread.is_alive():
            self.mailbox.put(_STOP)
            self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("The workspace actor did not stop.")

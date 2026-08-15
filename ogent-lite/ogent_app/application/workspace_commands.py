"""Typed commands and envelopes accepted by a workspace actor."""

from __future__ import annotations

import concurrent.futures
import dataclasses
from typing import Any

from ogent_app.domain.planning import RunPlan, RunStepState
from ogent_app.domain.run import RunMode, ScopeMode
from ogent_app.domain.workspace import (
    PreviewState,
    RunRecord,
    RunState,
    TurnRecord,
)


class WorkspaceBusyError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class AcceptTurn:
    content: str
    provider: str
    model: str
    effort: str
    mode: RunMode
    scope: ScopeMode
    plan: RunPlan | None = None
    run_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class AcceptedTurn:
    turn: TurnRecord
    run: RunRecord
    event_sequence: int


@dataclasses.dataclass(frozen=True)
class AppendTurn:
    role: str
    content: str
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    run_outcome: str | None = None
    metadata: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class TransitionRun:
    run_id: str
    target: RunState
    verification: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class TransitionRunStep:
    run_id: str
    step_id: str
    target: RunStepState
    checkpoint: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    error_code: str | None = None


@dataclasses.dataclass(frozen=True)
class CheckpointRunStep:
    run_id: str
    step_id: str
    checkpoint: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class RequestRunCancellation:
    run_id: str


@dataclasses.dataclass(frozen=True)
class RecordEvent:
    event_type: str
    payload: dict[str, Any]
    run_id: str | None = None


@dataclasses.dataclass(frozen=True)
class UpdateTurnOutcome:
    turn_id: str
    run_outcome: str
    metadata_update: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class ClearConversation:
    reason: str = "new_chat"


@dataclasses.dataclass(frozen=True)
class SetPreviewState:
    state: PreviewState


@dataclasses.dataclass(frozen=True)
class RecoverInterruptedRun:
    reason: str = "backend_restart"


@dataclasses.dataclass(frozen=True)
class GetWorkspaceState:
    pass


WorkspaceCommand = (
    AcceptTurn
    | AppendTurn
    | TransitionRun
    | TransitionRunStep
    | CheckpointRunStep
    | RequestRunCancellation
    | RecordEvent
    | UpdateTurnOutcome
    | ClearConversation
    | SetPreviewState
    | RecoverInterruptedRun
    | GetWorkspaceState
)


@dataclasses.dataclass
class WorkspaceEnvelope:
    command: WorkspaceCommand
    future: concurrent.futures.Future[Any]

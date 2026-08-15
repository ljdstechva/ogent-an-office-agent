"""Typed durable workspace, run, preview, turn, and event state."""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

from .planning import RunPlan
from .run import RunMode, ScopeMode


class WorkspaceStatus(str, enum.Enum):
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class RunState(str, enum.Enum):
    ACCEPTED = "accepted"
    CAPABILITY_BOOTSTRAP = "capability_bootstrap"
    DOCUMENT_REFRESH = "document_refresh"
    SCOPE_RESOLVED = "scope_resolved"
    PLAN_READY = "plan_ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    PREVIEW_SYNC = "preview_sync"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }


RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.ACCEPTED: frozenset(
        {
            RunState.CAPABILITY_BOOTSTRAP,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.CAPABILITY_BOOTSTRAP: frozenset(
        {
            RunState.DOCUMENT_REFRESH,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.DOCUMENT_REFRESH: frozenset(
        {
            RunState.SCOPE_RESOLVED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.SCOPE_RESOLVED: frozenset(
        {
            RunState.PLAN_READY,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.PLAN_READY: frozenset(
        {
            RunState.EXECUTING,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.EXECUTING: frozenset(
        {
            RunState.VERIFYING,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.VERIFYING: frozenset(
        {
            RunState.PREVIEW_SYNC,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.PREVIEW_SYNC: frozenset(
        {
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


class InvalidRunTransition(ValueError):
    pass


def validate_run_transition(current: RunState, target: RunState) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidRunTransition(
            f"Run state cannot transition from {current.value} to {target.value}."
        )


class PreviewState(str, enum.Enum):
    EMPTY = "empty"
    LOADING = "loading"
    LIVE = "live"
    WORD_VIEW = "word_view"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclasses.dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: str
    document_id: str | None
    conversation_generation: int
    created_at: str
    last_active_at: str
    status: WorkspaceStatus
    selected_provider: str | None = None
    selected_model: str | None = None
    selected_effort: str | None = None


@dataclasses.dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    workspace_id: str
    sequence: int
    role: str
    raw_content_blob_id: str
    display_excerpt: str
    character_count: int
    provider: str | None
    model: str | None
    effort: str | None
    created_at: str
    run_outcome: str | None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class RunRecord:
    run_id: str
    workspace_id: str
    request_turn_id: str
    state: RunState
    mode: RunMode
    scope: ScopeMode
    created_at: str
    updated_at: str
    cancellation_requested: bool = False
    verification: dict[str, Any] = dataclasses.field(default_factory=dict)
    plan: RunPlan | None = None


@dataclasses.dataclass(frozen=True)
class RunEvent:
    event_id: str
    workspace_id: str
    run_id: str | None
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclasses.dataclass(frozen=True)
class WorkspaceRuntimeState:
    workspace: WorkspaceRecord
    active_run: RunRecord | None = None
    preview_state: PreviewState = PreviewState.EMPTY
    last_event_sequence: int = 0

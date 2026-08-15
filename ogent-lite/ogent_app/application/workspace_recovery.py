"""Restart recovery and atomic conversation reset for a workspace actor."""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from ogent_app.domain.workspace import RunRecord, RunState, WorkspaceRuntimeState

from .workspace_commands import (
    ClearConversation,
    RecoverInterruptedRun,
    TransitionRun,
    WorkspaceBusyError,
)


class WorkspaceRecoveryHost(Protocol):
    workspace_id: str
    state: WorkspaceRuntimeState
    database: Any
    turns: Any
    workspaces: Any
    events: Any

    def _transition(self, command: TransitionRun) -> RunRecord: ...


def recover_interrupted_run(
    host: WorkspaceRecoveryHost,
    command: RecoverInterruptedRun,
) -> RunRecord | None:
    active = host.state.active_run
    if active is None or active.state.terminal:
        return None
    return host._transition(
        TransitionRun(
            active.run_id,
            RunState.FAILED,
            verification={
                "interrupted": True,
                "reason": command.reason,
            },
        )
    )


def clear_workspace_conversation(
    host: WorkspaceRecoveryHost,
    command: ClearConversation,
) -> WorkspaceRuntimeState:
    active = host.state.active_run
    if active is not None and not active.state.terminal:
        raise WorkspaceBusyError(
            "The active run must finish before starting a new chat."
        )
    with host.database.transaction() as connection:
        host.turns.clear_workspace(
            host.workspace_id,
            connection=connection,
        )
        workspace = host.workspaces.increment_generation(
            host.workspace_id,
            connection=connection,
        )
        event = host.events.append(
            host.workspace_id,
            "conversation.reset",
            {
                "generation": workspace.conversation_generation,
                "reason": command.reason,
            },
            connection=connection,
        )
    host.state = dataclasses.replace(
        host.state,
        workspace=workspace,
        active_run=None,
        last_event_sequence=event.sequence,
    )
    return host.state

"""Server-owned rollback and outcome-verification records."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class RollbackSnapshot:
    run_id: str
    document: Path
    blob_id: str
    package_sha256: str
    byte_size: int
    created_at: str


@dataclasses.dataclass(frozen=True)
class OutcomeVerification:
    accepted: bool
    outcome: str
    package_changed: bool
    affected_paths: tuple[str, ...]
    assertions: dict[str, Any]
    rollback_required: bool = False
    reason: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ChangesetRecord:
    changeset_id: str
    run_id: str
    workspace_id: str
    pre_revision_sha256: str
    post_revision_sha256: str
    affected_paths: tuple[str, ...]
    assertions: dict[str, Any]
    rollback_blob_id: str | None
    created_at: str
    undone_at: str | None = None
    restored_sha256: str | None = None
    undo_validation: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def undone(self) -> bool:
        return self.undone_at is not None

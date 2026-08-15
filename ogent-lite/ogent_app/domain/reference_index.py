"""Durable attachment extraction and search state."""

from __future__ import annotations

import dataclasses
import enum


class ReferenceIndexStatus(str, enum.Enum):
    QUEUED = "queued"
    INDEXING = "indexing"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            ReferenceIndexStatus.COMPLETE,
            ReferenceIndexStatus.PARTIAL,
            ReferenceIndexStatus.FAILED,
            ReferenceIndexStatus.CANCELLED,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ReferenceIndexRecord:
    attachment_id: str
    workspace_id: str
    original_name: str
    source_sha256: str
    status: ReferenceIndexStatus
    character_count: int
    chunk_count: int
    error_code: str | None
    created_at: str
    updated_at: str
    text_blob_id: str | None = None

    def public(self) -> dict[str, object]:
        return {
            "attachment_id": self.attachment_id,
            "status": self.status.value,
            "character_count": self.character_count,
            "chunk_count": self.chunk_count,
            "error_code": self.error_code,
            "updated_at": self.updated_at,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ReferenceSearchHit:
    attachment_id: str
    original_name: str
    chunk_index: int
    text: str
    rank: float

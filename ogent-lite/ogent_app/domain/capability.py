"""Deterministic OfficeCLI capability and audit records."""

from __future__ import annotations

import dataclasses
import enum
from typing import Any


class DocumentKind(str, enum.Enum):
    WORD = "word"
    EXCEL = "excel"
    POWERPOINT = "pptx"

    @property
    def extension(self) -> str:
        return {
            DocumentKind.WORD: ".docx",
            DocumentKind.EXCEL: ".xlsx",
            DocumentKind.POWERPOINT: ".pptx",
        }[self]


FORMAT_SKILLS: dict[str, DocumentKind] = {
    ".docx": DocumentKind.WORD,
    ".xlsx": DocumentKind.EXCEL,
    ".pptx": DocumentKind.POWERPOINT,
}


@dataclasses.dataclass(frozen=True)
class SkillPolicy:
    officecli_version: str
    skill_name: str
    policy_sha256: str
    policy_blob_id: str
    text: str
    loaded_at: str


@dataclasses.dataclass(frozen=True)
class CapabilityReceipt:
    receipt_id: str
    run_id: str
    workspace_id: str
    skill_name: str
    skill_sha256: str
    policy_blob_id: str
    officecli_version: str
    document_path_key: str
    document_revision: int
    package_sha256: str
    probe_operation: str
    probe: dict[str, Any]
    created_at: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.receipt_id,
            "run_id": self.run_id,
            "skill": self.skill_name,
            "skill_sha256": self.skill_sha256,
            "officecli_version": self.officecli_version,
            "document_revision": self.document_revision,
            "package_sha256": self.package_sha256,
            "probe_operation": self.probe_operation,
            "probe": dict(self.probe),
            "created_at": self.created_at,
        }


@dataclasses.dataclass(frozen=True)
class CapabilityBootstrapResult:
    document_kind: DocumentKind
    policy: SkillPolicy
    receipt: CapabilityReceipt
    stable_paths: tuple[str, ...] = ()


class MutationCategory(str, enum.Enum):
    READ = "read"
    MUTATION = "mutation"
    VALIDATION = "validation"
    REFRESH = "refresh"


@dataclasses.dataclass(frozen=True)
class ToolReceipt:
    receipt_id: str
    run_id: str
    operation: str
    started_at: str
    ended_at: str | None
    exit_status: int | None
    mutation_category: MutationCategory
    arguments: dict[str, Any]
    result: dict[str, Any]
    skill_name: str | None = None
    skill_sha256: str | None = None
    document_revision: int | None = None
    package_sha256: str | None = None
    output_sha256: str | None = None
    output_bytes: int | None = None

    @property
    def successful(self) -> bool:
        return self.exit_status == 0

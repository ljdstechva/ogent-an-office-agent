"""User-facing verified changeset review and guarded one-run undo."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ogent_app.domain.verification import ChangesetRecord, RollbackSnapshot
from ogent_app.infrastructure.sqlite import (
    ChangesetRepository,
    DocumentRepository,
)

from .rollback_manager import RollbackManager


class ChangeReviewError(RuntimeError):
    pass


class ChangeReviewService:
    _ASSERTION_KEYS = (
        "gateway_mutation_receipt",
        "targeted_readback",
        "officecli_validate",
        "mutation_evidence",
        "document_mutated",
        "completion_kind",
        "rolled_back",
    )

    def __init__(
        self,
        changesets: ChangesetRepository,
        documents: DocumentRepository,
        rollback: RollbackManager,
    ) -> None:
        self.changesets = changesets
        self.documents = documents
        self.rollback = rollback

    def review(
        self,
        workspace_id: str,
        *,
        document_id: str | None,
        document: Path | None,
    ) -> dict[str, Any]:
        record = self.changesets.latest_for_workspace(workspace_id)
        if record is None:
            return self._empty("No verified edit run exists in this workspace.")
        return self._public(
            record,
            document_id=document_id,
            document=document,
        )

    def undo(
        self,
        workspace_id: str,
        changeset_id: str,
        *,
        document_id: str,
        document: Path,
        validate: Callable[[Path], dict[str, Any]],
    ) -> dict[str, Any]:
        record = self.changesets.get(changeset_id)
        if record is None or record.workspace_id != workspace_id:
            raise ChangeReviewError("That verified change is unavailable.")
        latest = self.changesets.latest_for_workspace(workspace_id)
        if latest is None or latest.changeset_id != record.changeset_id:
            raise ChangeReviewError(
                "Only the most recent verified document change can be undone."
            )
        if record.undone:
            raise ChangeReviewError("That verified change was already undone.")
        if not record.rollback_blob_id:
            raise ChangeReviewError("The verified pre-run package is unavailable.")
        target = Path(document).expanduser().resolve(strict=True)
        current_hash = self._package_sha256(target)
        if current_hash != record.post_revision_sha256:
            raise ChangeReviewError(
                "The document changed after this run. Undo was blocked to "
                "protect newer work."
            )

        safety = self.rollback.create(
            target,
            run_id=f"undo-safety-{uuid.uuid4().hex}",
        )
        prior = RollbackSnapshot(
            run_id=record.run_id,
            document=target,
            blob_id=record.rollback_blob_id,
            package_sha256=record.pre_revision_sha256,
            byte_size=0,
            created_at=record.created_at,
        )
        try:
            restored = self.rollback.restore(prior, target)
            validation = dict(validate(target))
            if validation.get("accepted") is not True:
                raise ChangeReviewError(
                    "The restored package did not pass OfficeCLI validation."
                )
            if restored != record.pre_revision_sha256:
                raise ChangeReviewError(
                    "The restored package did not match the verified pre-run hash."
                )
            self.changesets.record_undo(
                changeset_id=record.changeset_id,
                workspace_id=workspace_id,
                safety_blob_id=safety.blob_id,
                restored_sha256=restored,
                validation=validation,
            )
        except Exception:
            self.rollback.restore(safety, target)
            raise
        updated = self.changesets.get(record.changeset_id)
        assert updated is not None
        return self._public(
            updated,
            document_id=document_id,
            document=target,
        )

    def _public(
        self,
        record: ChangesetRecord,
        *,
        document_id: str | None,
        document: Path | None,
    ) -> dict[str, Any]:
        current_hash: str | None = None
        unavailable = False
        if document is not None:
            try:
                current_hash = self._package_sha256(
                    Path(document).expanduser().resolve(strict=True)
                )
            except OSError:
                unavailable = True
        can_undo = bool(
            not record.undone
            and record.rollback_blob_id
            and current_hash == record.post_revision_sha256
        )
        if record.undone:
            reason = "This run has already been undone."
        elif unavailable or document is None:
            reason = "The active document is unavailable."
        elif current_hash != record.post_revision_sha256:
            reason = (
                "The document changed after this run, so undo is blocked to "
                "protect newer work."
            )
        elif not record.rollback_blob_id:
            reason = "The verified pre-run package is unavailable."
        else:
            reason = None
        assertions = {
            key: self._safe_scalar(record.assertions[key])
            for key in self._ASSERTION_KEYS
            if key in record.assertions
        }
        preview = record.assertions.get("preview")
        if isinstance(preview, dict):
            assertions["preview_status"] = self._safe_scalar(
                preview.get("status", "unknown")
            )
            assertions["preview_confirmed"] = bool(preview.get("confirmed"))
        excerpts, changed_metadata = self._excerpts(
            record,
            document_id=document_id,
        )
        return {
            "changeset_id": record.changeset_id,
            "run_id": record.run_id,
            "created_at": record.created_at,
            "outcome": record.assertions.get(
                "completion_kind",
                "edit_completed",
            ),
            "affected_paths": list(record.affected_paths),
            "assertions": assertions,
            "pre_revision_sha256": record.pre_revision_sha256,
            "post_revision_sha256": record.post_revision_sha256,
            "excerpts": excerpts,
            "formula_style_changes": changed_metadata,
            "can_undo": can_undo,
            "undone": record.undone,
            "undo_reason": reason,
            "undone_at": record.undone_at,
        }

    def _excerpts(
        self,
        record: ChangesetRecord,
        *,
        document_id: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not document_id:
            return [], []
        before_revision = self.documents.revision_for_package(
            document_id,
            record.pre_revision_sha256,
        )
        after_revision = self.documents.revision_for_package(
            document_id,
            record.post_revision_sha256,
        )
        paths = tuple(path for path in record.affected_paths if path and path != "/")
        if not before_revision or not after_revision or not paths:
            return [], []
        before = {
            item.node.stable_path: item.node
            for item in self.documents.nodes_for_paths(
                before_revision.revision_id,
                paths,
                include_text=True,
            )
        }
        after = {
            item.node.stable_path: item.node
            for item in self.documents.nodes_for_paths(
                after_revision.revision_id,
                paths,
                include_text=True,
            )
        }
        excerpts: list[dict[str, Any]] = []
        metadata_changes: list[dict[str, Any]] = []
        for path in paths:
            before_node = before.get(path)
            after_node = after.get(path)
            if before_node is None and after_node is None:
                continue
            excerpts.append(
                {
                    "path": path,
                    "before": self._excerpt(before_node.text if before_node else ""),
                    "after": self._excerpt(after_node.text if after_node else ""),
                }
            )
            before_metadata = before_node.metadata if before_node else {}
            after_metadata = after_node.metadata if after_node else {}
            changed_keys = sorted(
                key
                for key in set(before_metadata) | set(after_metadata)
                if before_metadata.get(key) != after_metadata.get(key)
            )
            relevant = [
                key
                for key in changed_keys
                if any(
                    term in key.casefold()
                    for term in (
                        "formula",
                        "style",
                        "format",
                        "font",
                        "fill",
                        "number",
                    )
                )
            ]
            if relevant:
                metadata_changes.append(
                    {
                        "path": path,
                        "fields": relevant[:20],
                    }
                )
        return excerpts, metadata_changes

    @staticmethod
    def _empty(reason: str) -> dict[str, Any]:
        return {
            "affected_paths": [],
            "assertions": {},
            "excerpts": [],
            "formula_style_changes": [],
            "can_undo": False,
            "undone": False,
            "undo_reason": reason,
        }

    @staticmethod
    def _package_sha256(document: Path) -> str:
        digest = hashlib.sha256()
        with Path(document).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _excerpt(value: str, limit: int = 800) -> str:
        text = str(value)
        return text if len(text) <= limit else f"{text[:limit]}…"

    @staticmethod
    def _safe_scalar(value: Any) -> bool | str | int | float | None:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:200]

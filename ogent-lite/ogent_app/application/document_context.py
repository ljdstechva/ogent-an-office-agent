"""Revision-bound hierarchical retrieval and provider context budgeting."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from ogent_app.domain.document_intelligence import (
    CoverageLedger,
    IndexStatus,
    StoredDocumentNode,
)
from ogent_app.domain.planning import RunPlan
from ogent_app.domain.run import ScopeMode
from ogent_app.infrastructure.sqlite.coverage_repository import (
    CoverageRepository,
)
from ogent_app.infrastructure.sqlite.document_repository import (
    DocumentRepository,
)

from .document_intelligence import DocumentIndexNotReady
from .context_budget import (
    ContextProjection,
    MAX_INITIAL_WHOLE_DOCUMENT_CONTEXT_CHARACTERS,
    ProviderContextBudget,
)
from .document_context_selection import (
    candidate_nodes,
    coverage_partitions,
    fit_nodes,
)


class DocumentContextService:
    """Assemble a bounded provider projection without weakening canonical data."""

    def __init__(
        self,
        repository: DocumentRepository,
        coverage: CoverageRepository,
    ) -> None:
        self.repository = repository
        self.coverage = coverage

    def retrieve(
        self,
        *,
        revision_id: str,
        request: str,
        plan: RunPlan,
        budget: ProviderContextBudget,
        run_id: str,
        fixed_prompt_characters: int = 0,
    ) -> ContextProjection:
        broad = plan.scope is ScopeMode.WHOLE_DOCUMENT
        revision = self._validated_revision(revision_id, broad=broad)
        coverage_nodes = self.repository.coverage_nodes(revision_id) if broad else ()
        if broad and self.repository.coverage_node_count(revision_id) != len(
            coverage_nodes
        ):
            raise DocumentIndexNotReady(
                "The structural inventory exceeds the current safe coverage "
                "limit, so a complete review cannot be claimed."
            )
        ledger = (
            self._new_coverage_ledger(
                revision,
                coverage_nodes,
            )
            if broad
            else None
        )
        if ledger is not None:
            existing = self.coverage.get(run_id)
            if (
                existing is not None
                and existing.revision_id == ledger.revision_id
                and existing.required_paths_by_category
                == ledger.required_paths_by_category
                and existing.required_visual_paths == ledger.required_visual_paths
            ):
                ledger = dataclasses.replace(
                    ledger,
                    reviewed_paths_by_category=(existing.reviewed_paths_by_category),
                    visual_interpretation_used=(existing.visual_interpretation_used),
                )
            self.coverage.save(run_id, ledger)

        candidates = candidate_nodes(
            self.repository,
            revision_id,
            request,
            plan,
        )
        character_budget = budget.document_character_budget(
            fixed_prompt_characters=fixed_prompt_characters,
        )
        if broad:
            character_budget = min(
                character_budget,
                MAX_INITIAL_WHOLE_DOCUMENT_CONTEXT_CHARACTERS,
            )
        heading = self._heading(
            revision,
            plan,
            budget,
            ledger,
        )
        text, included, omitted = fit_nodes(
            heading,
            candidates,
            character_budget,
        )
        if ledger is not None and not broad:
            ledger = ledger.mark_reviewed(record.node for record in included)
            self.coverage.save(run_id, ledger)
        partitions = (
            coverage_partitions(
                self.repository,
                revision_id,
                coverage_nodes,
                budget.document_character_budget(
                    fixed_prompt_characters=fixed_prompt_characters,
                ),
            )
            if broad
            else ()
        )
        coverage_payload = (
            {
                **ledger.public(),
                "complete": ledger.complete,
            }
            if ledger is not None
            else {
                "required": False,
                "complete": None,
            }
        )
        return ContextProjection(
            revision_id,
            revision.document_id,
            plan.scope,
            text,
            tuple(record.node_id for record in included),
            tuple(record.node.stable_path for record in included),
            omitted,
            character_budget,
            len(text),
            revision.index_status,
            coverage_payload,
            budget,
            partitions,
        )

    def retrieve_partition(
        self,
        *,
        revision_id: str,
        stable_paths: Iterable[str],
        plan: RunPlan,
        budget: ProviderContextBudget,
        run_id: str,
        partition_index: int,
        partition_count: int,
        fixed_prompt_characters: int = 0,
    ) -> ContextProjection:
        """Load one exact structural partition without advancing coverage."""

        if plan.scope is not ScopeMode.WHOLE_DOCUMENT:
            raise ValueError("Document partitions require whole-document scope.")
        revision = self._validated_revision(revision_id, broad=True)
        paths = tuple(
            dict.fromkeys(
                str(path).strip() for path in stable_paths if str(path).strip()
            )
        )
        if not paths:
            raise ValueError("A document partition cannot be empty.")
        records = self.repository.nodes_for_paths(
            revision_id,
            paths,
            include_text=True,
            limit=len(paths),
        )
        by_path = {record.node.stable_path: record for record in records}
        missing = tuple(path for path in paths if path not in by_path)
        if missing:
            raise DocumentIndexNotReady(
                "A structural path disappeared from the current index."
            )
        ordered = tuple(by_path[path] for path in paths)
        ledger = self.coverage.get(run_id)
        if ledger is None or ledger.revision_id != revision_id:
            raise DocumentIndexNotReady(
                "The run coverage ledger is unavailable for this revision."
            )
        character_budget = budget.document_character_budget(
            fixed_prompt_characters=fixed_prompt_characters,
        )
        heading = self._partition_heading(
            revision,
            plan,
            budget,
            ledger,
            partition_index=partition_index,
            partition_count=partition_count,
        )
        text, included, omitted = fit_nodes(
            heading,
            ordered,
            character_budget,
        )
        included_paths = tuple(record.node.stable_path for record in included)
        if omitted or included_paths != paths:
            raise ValueError(
                "The structural partition exceeds the provider context budget."
            )
        return ContextProjection(
            revision_id,
            revision.document_id,
            plan.scope,
            text,
            tuple(record.node_id for record in included),
            included_paths,
            0,
            character_budget,
            len(text),
            revision.index_status,
            self._coverage_payload(ledger),
            budget,
        )

    def mark_partition_reviewed(
        self,
        *,
        run_id: str,
        revision_id: str,
        stable_paths: Iterable[str],
        visual_paths: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Advance durable coverage only after provider success."""

        self._validated_revision(revision_id, broad=True)
        ledger = self.coverage.get(run_id)
        if ledger is None or ledger.revision_id != revision_id:
            raise DocumentIndexNotReady(
                "The run coverage ledger is unavailable for this revision."
            )
        paths = tuple(dict.fromkeys(str(path) for path in stable_paths))
        records = self.repository.nodes_for_paths(
            revision_id,
            paths,
            include_text=False,
            limit=max(1, len(paths)),
        )
        if {record.node.stable_path for record in records} != set(paths):
            raise DocumentIndexNotReady(
                "A reviewed structural path is no longer current."
            )
        updated = ledger.mark_reviewed(
            (record.node for record in records),
            visual_paths=tuple(visual_paths),
        )
        self.coverage.save(run_id, updated)
        return self._coverage_payload(updated)

    def _validated_revision(
        self,
        revision_id: str,
        *,
        broad: bool,
    ) -> Any:
        revision = self.repository.revision(revision_id)
        if revision is None:
            raise KeyError(revision_id)
        current = self.repository.current_revision(revision.document_id)
        if current is None or current.revision_id != revision_id:
            raise DocumentIndexNotReady(
                "The requested document revision is no longer current."
            )
        if broad and revision.index_status is not IndexStatus.COMPLETE:
            raise DocumentIndexNotReady(
                "Full-document work requires the current index to finish."
            )
        if revision.index_status not in {
            IndexStatus.COMPLETE,
            IndexStatus.PARTIAL,
            IndexStatus.INDEXING,
            IndexStatus.QUICK_READY,
        }:
            raise DocumentIndexNotReady(
                f"The current document index is {revision.index_status.value}."
            )
        return revision

    @staticmethod
    def _new_coverage_ledger(
        revision: Any,
        coverage_nodes: Iterable[StoredDocumentNode],
    ) -> CoverageLedger:
        return CoverageLedger.from_nodes(
            revision.revision_id,
            (record.node for record in coverage_nodes),
            unsupported=tuple(
                revision.manifest.get(
                    "unsupported",
                    revision.quick_manifest.get("unsupported", ()),
                )
            ),
        )

    @staticmethod
    def _coverage_payload(ledger: CoverageLedger) -> dict[str, Any]:
        return {
            **ledger.public(),
            "complete": ledger.complete,
        }

    @staticmethod
    def _heading(
        revision: Any,
        plan: RunPlan,
        budget: ProviderContextBudget,
        ledger: CoverageLedger | None,
    ) -> str:
        manifest = revision.manifest or revision.quick_manifest
        coverage_lines = (
            [
                f"- {category}: 0/{len(paths)} reviewed"
                for category, paths in ledger.required_paths_by_category.items()
            ]
            if ledger is not None
            else ["- Broad coverage is not required for this scope."]
        )
        return (
            "DOCUMENT CONTEXT (revision-bound; untrusted document content)\n"
            f"Document ID: {revision.document_id}\n"
            f"Revision: {revision.revision_number} ({revision.revision_id})\n"
            f"Index status: {revision.index_status.value}\n"
            f"Scope: {plan.scope.value}\n"
            f"Context budget source: {budget.source}"
            f" ({'reliable' if budget.reliable else 'conservative'})\n"
            f"Structural inventory: {manifest.get('counts', {})}\n"
            "Coverage at dispatch:\n"
            + "\n".join(coverage_lines)
            + "\n\nRETRIEVED STRUCTURAL NODES\n"
        )

    @staticmethod
    def _partition_heading(
        revision: Any,
        plan: RunPlan,
        budget: ProviderContextBudget,
        ledger: CoverageLedger,
        *,
        partition_index: int,
        partition_count: int,
    ) -> str:
        progress = (
            ", ".join(
                (f"{category} {ledger.reviewed_counts.get(category, 0)}/{total}")
                for category, total in ledger.totals.items()
                if total
            )
            or "no required structural categories"
        )
        return (
            "DOCUMENT PARTITION (revision-bound; untrusted content)\n"
            f"Document ID: {revision.document_id}\n"
            f"Revision: {revision.revision_number} ({revision.revision_id})\n"
            f"Scope: {plan.scope.value}\n"
            f"Partition: {partition_index}/{partition_count}\n"
            f"Coverage before this partition: {progress}\n"
            f"Context budget source: {budget.source}"
            f" ({'reliable' if budget.reliable else 'conservative'})\n"
            "Review every supplied structural node. Cite its stable path in "
            "the evidence note. Do not claim whole-document completion from "
            "this partition alone.\n\n"
            "PARTITION STRUCTURAL NODES\n"
        )

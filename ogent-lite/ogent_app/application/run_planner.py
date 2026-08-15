"""Fast-path classification and deterministic visible run planning."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ogent_app.domain.planning import (
    RunComplexity,
    RunPlan,
    RunStep,
)
from ogent_app.domain.run import RunContract, RunMode, ScopeMode


_MULTI_ACTION = re.compile(
    r"(?i)\b(?:and then|then|after that|followed by|also|as well as)\b"
)
_BROAD_OBJECTS = re.compile(r"(?i)\b(?:all|every|throughout|entire|whole|across)\b")


class RunPlanner:
    """Produce a provider-independent plan before a turn is accepted."""

    def classify(
        self,
        message: str,
        contract: RunContract,
        *,
        target_node_count: int = 0,
        attachment_count: int = 0,
    ) -> RunComplexity:
        text = str(message)
        structured = (
            contract.scope
            in {
                ScopeMode.WHOLE_DOCUMENT,
                ScopeMode.SPECIFIED_SECTIONS,
                ScopeMode.SPECIFIED_SHEETS,
                ScopeMode.SPECIFIED_SLIDES,
            }
            or contract.mode in {RunMode.REVIEW, RunMode.COMPARE}
            or target_node_count > 3
            or attachment_count > 1
            or len(text) > 1_500
            or bool(_MULTI_ACTION.search(text))
            or bool(_BROAD_OBJECTS.search(text))
        )
        return RunComplexity.STRUCTURED if structured else RunComplexity.FAST_PATH

    def build(
        self,
        message: str,
        contract: RunContract,
        *,
        target_node_ids: Iterable[str] = (),
        attachment_ids: Iterable[str] = (),
        has_document: bool,
    ) -> RunPlan:
        targets = tuple(dict.fromkeys(str(item) for item in target_node_ids))
        attachments = tuple(dict.fromkeys(str(item) for item in attachment_ids))
        complexity = self.classify(
            message,
            contract,
            target_node_count=len(targets),
            attachment_count=len(attachments),
        )
        steps: list[RunStep] = []

        def add(
            step_id: str,
            description: str,
            *,
            mutates: bool = False,
            tool: str | None,
            proof: str,
            units: int = 1,
        ) -> None:
            dependencies = (steps[-1].step_id,) if steps else ()
            steps.append(
                RunStep(
                    step_id,
                    len(steps) + 1,
                    description,
                    targets,
                    mutates,
                    tool,
                    proof,
                    dependencies,
                    units,
                )
            )

        if has_document:
            add(
                "inspect",
                "Inspect the active revision and resolve the requested scope.",
                tool="inspect_document",
                proof="A revision-bound inspection receipt and scoped node set exist.",
                units=2 if complexity is RunComplexity.STRUCTURED else 1,
            )
        if attachments:
            add(
                "references",
                "Retrieve the attachment portions needed for this request.",
                tool="reference_index",
                proof="Required attachment derivatives are ready and bounded.",
                units=max(1, len(attachments)),
            )
        add(
            "execute",
            (
                "Apply the requested document changes through one restricted "
                "OfficeCLI gateway."
                if contract.requires_mutation
                else "Analyze the retrieved evidence and compose the answer."
            ),
            mutates=contract.requires_mutation,
            tool=("apply_atomic_batch" if contract.requires_mutation else "provider"),
            proof=(
                "At least one successful mutation receipt exists."
                if contract.requires_mutation
                else "The answer cites revision-bound structural evidence."
            ),
            units=3 if complexity is RunComplexity.STRUCTURED else 1,
        )
        add(
            "verify",
            (
                "Read back affected elements and validate the finished artifact."
                if contract.requires_mutation
                else "Verify scope coverage and the evidence used in the answer."
            ),
            tool=(
                "validate_document" if contract.requires_mutation else "coverage_ledger"
            ),
            proof=(
                "Readback, validation, hashes, and affected paths agree."
                if contract.requires_mutation
                else "Coverage is disclosed without unsupported completeness claims."
            ),
            units=2,
        )
        if contract.requires_mutation and has_document:
            add(
                "preview",
                "Confirm the live preview or report an honest degraded state.",
                tool="preview_confirmation",
                proof="Preview status is recorded for the post-edit revision.",
            )

        verification = [
            "run_contract_preserved",
            "provider_context_within_budget",
        ]
        expected_mutations: list[str] = []
        if contract.requires_mutation:
            expected_mutations.extend(
                contract.selected_paths or ("requested_document_scope",)
            )
            verification.extend(
                (
                    "package_hash_changed",
                    "mutation_receipt_present",
                    "targeted_readback_passed",
                    "officecli_validate_passed",
                    "preview_status_recorded",
                )
            )
        else:
            verification.extend(
                (
                    "inspection_evidence_present",
                    "stable_path_citations_present",
                    "coverage_disclosed",
                )
            )
        broad = contract.scope is ScopeMode.WHOLE_DOCUMENT
        coverage = {
            "required": broad,
            "scope": contract.scope.value,
            "require_complete_index": broad,
            "categories": (
                [
                    "sections",
                    "tables",
                    "figures",
                    "charts",
                    "sheets_or_slides",
                    "process_flows",
                ]
                if broad
                else []
            ),
        }
        return RunPlan(
            goal=str(message),
            mode=contract.mode,
            scope=contract.scope,
            steps=tuple(steps),
            dependencies={step.step_id: step.dependencies for step in steps},
            target_node_ids=targets,
            expected_mutations=tuple(expected_mutations),
            verification_assertions=tuple(verification),
            coverage_requirement=coverage,
            estimated_work_units=sum(step.estimated_work_units for step in steps),
            complexity=complexity,
        )

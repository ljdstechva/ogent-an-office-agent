"""Sequential, checkpointed provider execution for exhaustive document review."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .document_context import (
    ContextProjection,
    DocumentContextService,
    ProviderContextBudget,
)
from .partition_artifacts import (
    AnalysisNote,
    PartitionedExecutionResult,
    checkpoint_payload,
    manifest_blob,
    notes_context,
    pack_notes,
    partition_request,
    persist_note,
    provisional_partition_text,
    reduction_request,
    restore_partition_notes,
    synthesis_context,
    synthesis_request,
)
from .provider_execution import ProviderExecutionResult
from .visual_evidence import VisualEvidence


MAX_SYNTHESIS_CONTEXT_CHARACTERS = 64_000

EventObserver = Callable[[str, dict[str, Any]], None]
DispatchProvider = Callable[
    [str, EventObserver | None, tuple[Path, ...]],
    ProviderExecutionResult,
]
PromptFactory = Callable[[str, str], str]
CheckpointWriter = Callable[[dict[str, Any]], None]
VisualEvidenceFactory = Callable[
    [tuple[str, ...], int],
    VisualEvidence,
]


def execute_partitioned_analysis(
    *,
    request: str,
    run_id: str,
    plan: Any,
    projection: ContextProjection,
    context_service: DocumentContextService,
    budget: ProviderContextBudget,
    fixed_prompt_characters: int,
    prompt_factory: PromptFactory,
    dispatch: DispatchProvider,
    assistant_stream: Any,
    checkpoint: CheckpointWriter,
    stop_requested: Callable[[], bool],
    blob_store: Any | None,
    visual_evidence: VisualEvidenceFactory | None = None,
    resume_checkpoint: dict[str, Any] | None = None,
) -> PartitionedExecutionResult:
    """Review structural partitions, reduce their notes, then synthesize."""

    projection = dataclasses.replace(
        projection,
        partitions=_normalize_partitions(
            projection,
            context_service=context_service,
            plan=plan,
            budget=budget,
            run_id=run_id,
            fixed_prompt_characters=fixed_prompt_characters,
        ),
    )
    partitions = projection.partitions
    restored = restore_partition_notes(
        blob_store,
        resume_checkpoint or {},
        revision_id=projection.revision_id,
        partition_count=len(partitions),
    )
    notes = list(restored)
    coverage = dict(projection.coverage)
    for completed_paths in partitions[: len(notes)]:
        coverage = context_service.mark_partition_reviewed(
            run_id=run_id,
            revision_id=projection.revision_id,
            stable_paths=completed_paths,
        )
    manifest_blob_id = manifest_blob(
        blob_store,
        projection,
        notes,
    )
    last_result = ProviderExecutionResult(0, "", (), {})

    for offset, paths in enumerate(
        partitions[len(notes) :],
        start=len(notes) + 1,
    ):
        if stop_requested():
            break
        partition = context_service.retrieve_partition(
            revision_id=projection.revision_id,
            stable_paths=paths,
            plan=plan,
            budget=budget,
            run_id=run_id,
            partition_index=offset,
            partition_count=len(partitions),
            fixed_prompt_characters=fixed_prompt_characters,
        )
        partition_prompt = partition_request(
            request,
            offset,
            len(partitions),
        )
        visual = (
            visual_evidence(
                paths,
                max(
                    0,
                    partition.character_budget - partition.character_count,
                ),
            )
            if visual_evidence is not None
            else VisualEvidence()
        )
        result = dispatch(
            prompt_factory(
                partition_prompt,
                partition.text + visual.context,
            ),
            None,
            visual.image_paths,
        )
        last_result = result
        if result.exit_code != 0:
            checkpoint(
                checkpoint_payload(
                    projection,
                    notes,
                    manifest_blob_id,
                    next_partition=offset,
                    phase="partition_failed",
                    provider_exit_code=result.exit_code,
                )
            )
            return PartitionedExecutionResult(
                result,
                len(notes),
                len(partitions),
                0,
                coverage,
                tuple(notes),
                manifest_blob_id,
            )
        note = persist_note(
            blob_store,
            f"Partition {offset}/{len(partitions)}",
            assistant_stream.sanitize(str(result.final_text or "")),
        )
        notes.append(note)
        coverage = context_service.mark_partition_reviewed(
            run_id=run_id,
            revision_id=projection.revision_id,
            stable_paths=paths,
            visual_paths=visual.interpreted_paths,
        )
        manifest_blob_id = manifest_blob(
            blob_store,
            projection,
            notes,
        )
        assistant_stream.append_segment(
            provisional_partition_text(note, offset, len(partitions)),
            source_event="partition.completed",
            metadata={
                "partition_index": offset,
                "partition_count": len(partitions),
            },
        )
        checkpoint(
            checkpoint_payload(
                projection,
                notes,
                manifest_blob_id,
                next_partition=(offset + 1 if offset < len(partitions) else None),
                phase="partition",
                coverage=coverage,
                visual_evidence=visual.public(),
            )
        )

    if stop_requested():
        return PartitionedExecutionResult(
            last_result,
            len(notes),
            len(partitions),
            0,
            coverage,
            tuple(notes),
            manifest_blob_id,
        )

    reduced, reduction_rounds, reduction_failure = _reduce_notes(
        request=request,
        initial_notes=tuple(notes),
        budget=budget,
        fixed_prompt_characters=fixed_prompt_characters,
        prompt_factory=prompt_factory,
        dispatch=dispatch,
        assistant_stream=assistant_stream,
        checkpoint=checkpoint,
        stop_requested=stop_requested,
        blob_store=blob_store,
        projection=projection,
        partition_manifest_blob_id=manifest_blob_id,
    )
    if reduction_failure is not None:
        return PartitionedExecutionResult(
            reduction_failure,
            len(notes),
            len(partitions),
            reduction_rounds,
            coverage,
            tuple(notes),
            manifest_blob_id,
        )
    if stop_requested():
        return PartitionedExecutionResult(
            last_result,
            len(notes),
            len(partitions),
            reduction_rounds,
            coverage,
            tuple(notes),
            manifest_blob_id,
        )
    final_context = synthesis_context(
        projection,
        reduced,
        coverage,
    )
    final_result = dispatch(
        prompt_factory(
            synthesis_request(request, len(partitions), coverage),
            final_context,
        ),
        assistant_stream.observe,
        (),
    )
    checkpoint(
        checkpoint_payload(
            projection,
            notes,
            manifest_blob_id,
            next_partition=None,
            phase="synthesis",
            coverage=coverage,
            provider_exit_code=final_result.exit_code,
            reduction_rounds=reduction_rounds,
        )
    )
    return PartitionedExecutionResult(
        final_result,
        len(notes),
        len(partitions),
        reduction_rounds,
        coverage,
        tuple(notes),
        manifest_blob_id,
    )


def _normalize_partitions(
    projection: ContextProjection,
    *,
    context_service: DocumentContextService,
    plan: Any,
    budget: ProviderContextBudget,
    run_id: str,
    fixed_prompt_characters: int,
) -> tuple[tuple[str, ...], ...]:
    pending = list(projection.partitions)
    normalized: list[tuple[str, ...]] = []
    while pending:
        paths = pending.pop(0)
        try:
            context_service.retrieve_partition(
                revision_id=projection.revision_id,
                stable_paths=paths,
                plan=plan,
                budget=budget,
                run_id=run_id,
                partition_index=len(normalized) + 1,
                partition_count=len(normalized) + len(pending) + 1,
                fixed_prompt_characters=fixed_prompt_characters,
            )
        except ValueError:
            if len(paths) <= 1:
                raise
            middle = len(paths) // 2
            pending[0:0] = [paths[:middle], paths[middle:]]
            continue
        normalized.append(paths)
    return tuple(normalized)


def _reduce_notes(
    *,
    request: str,
    initial_notes: tuple[AnalysisNote, ...],
    budget: ProviderContextBudget,
    fixed_prompt_characters: int,
    prompt_factory: PromptFactory,
    dispatch: DispatchProvider,
    assistant_stream: Any,
    checkpoint: CheckpointWriter,
    stop_requested: Callable[[], bool],
    blob_store: Any | None,
    projection: ContextProjection,
    partition_manifest_blob_id: str | None,
) -> tuple[tuple[AnalysisNote, ...], int, ProviderExecutionResult | None]:
    notes = initial_notes
    target = max(
        12_000,
        min(
            MAX_SYNTHESIS_CONTEXT_CHARACTERS,
            budget.document_character_budget(
                fixed_prompt_characters=fixed_prompt_characters,
            )
            - len(projection.text)
            - 4_000,
        ),
    )
    rounds = 0
    while sum(len(note.context_text()) for note in notes) > target:
        rounds += 1
        groups = pack_notes(notes, target)
        reduced: list[AnalysisNote] = []
        for group_index, group in enumerate(groups, 1):
            if stop_requested():
                return notes, rounds, None
            context = notes_context(
                group,
                heading=(
                    "INTERMEDIATE PARTITION NOTES "
                    f"(reduction round {rounds}, group "
                    f"{group_index}/{len(groups)})"
                ),
            )
            result = dispatch(
                prompt_factory(
                    reduction_request(request),
                    context,
                ),
                None,
                (),
            )
            if result.exit_code != 0:
                return notes, rounds, result
            reduced.append(
                persist_note(
                    blob_store,
                    f"Reduction {rounds}.{group_index}",
                    assistant_stream.sanitize(str(result.final_text or "")),
                )
            )
            checkpoint(
                {
                    "phase": "reduction",
                    "revision_id": projection.revision_id,
                    "partition_count": len(projection.partitions),
                    "completed_partitions": len(initial_notes),
                    "partition_manifest_blob_id": (partition_manifest_blob_id),
                    "reduction_round": rounds,
                    "reduction_group": group_index,
                    "reduction_group_count": len(groups),
                }
            )
        if len(reduced) >= len(notes):
            raise RuntimeError("Hierarchical analysis reduction did not make progress.")
        notes = tuple(reduced)
        assistant_stream.append_segment(
            f"\nConsolidated analysis round {rounds}.\n",
            source_event="analysis.reduction.completed",
            metadata={"reduction_round": rounds},
        )
    return notes, rounds, None

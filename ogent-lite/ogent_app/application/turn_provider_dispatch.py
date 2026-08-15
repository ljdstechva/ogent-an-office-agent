"""Prepare and dispatch standard or partitioned provider work for one turn."""

from __future__ import annotations

import dataclasses
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .document_context import ContextProjection, ProviderContextBudget
from .partitioned_provider_execution import execute_partitioned_analysis
from .provider_events import AssistantStreamAccumulator
from .provider_execution import ProviderExecutionResult, execute_provider
from .visual_evidence import VisualEvidencePreparer


@dataclasses.dataclass(slots=True)
class TurnProviderDispatchRequest:
    runtime: Any
    session: Any
    message: str
    document: Path | None
    source: Path | None
    provider: str
    model: str
    effort: str
    run_id: str
    references: list[Any]
    prepared_references: list[Any]
    run_root: Path | None
    memory_context: str
    selection_snapshot: Any | None
    document_mode: str
    run_contract: Any
    capability_context: str
    document_context: str
    reference_context: str
    context_projection: ContextProjection | None
    context_budget: ProviderContextBudget | None
    run_plan: Any | None
    working_directory: Path
    image_paths: list[Path]
    sandbox: str
    writable_directories: list[Path]
    timing: Any
    audit_log_path: Path | None
    capability: Any
    initial_package_sha256: str | None
    conversation_generation: int


@dataclasses.dataclass(frozen=True, slots=True)
class TurnProviderDispatchResult:
    execution: ProviderExecutionResult
    assistant_stream: AssistantStreamAccumulator
    prompt_bytes: int
    partitioned_execution: dict[str, Any] | None = None


def dispatch_turn_provider(
    request: TurnProviderDispatchRequest,
) -> TurnProviderDispatchResult:
    """Dispatch one ordinary call or a checkpointed exhaustive analysis."""

    runtime = request.runtime
    stream = AssistantStreamAccumulator(
        request.session,
        run_id=request.run_id,
        provider=request.provider,
        expected_generation=request.conversation_generation,
        sanitize=lambda text: runtime._redact_reference_detail(
            text,
            attachments=request.references,
        ),
    )

    def prompt_factory(message: str, document_context: str) -> str:
        return runtime.agent_prompt(
            message,
            request.document,
            request.source,
            request.prepared_references,
            request.run_root,
            memory_context=request.memory_context,
            preview_selection=request.selection_snapshot,
            document_mode=request.document_mode,
            run_contract=request.run_contract,
            capability_context=request.capability_context,
            document_context=document_context,
            reference_context=request.reference_context,
        )

    prompt_bytes = 0

    def dispatch(
        prompt: str,
        observer: Callable[[str, dict[str, Any]], None] | None,
        additional_images: tuple[Path, ...],
    ) -> ProviderExecutionResult:
        nonlocal prompt_bytes
        prompt_bytes += len(prompt.encode("utf-8"))
        images = list(dict.fromkeys((*request.image_paths, *additional_images)))
        return execute_provider(
            runtime,
            session=request.session,
            provider=request.provider,
            prompt=prompt,
            working_directory=request.working_directory,
            model=request.model,
            effort=request.effort,
            run_id=request.run_id,
            image_paths=images,
            sandbox=request.sandbox,
            writable_directories=request.writable_directories,
            document=request.document,
            references=request.prepared_references,
            timing=request.timing,
            run_contract=request.run_contract,
            audit_log_path=request.audit_log_path,
            capability=request.capability,
            initial_package_sha256=request.initial_package_sha256,
            run_root=request.run_root,
            conversation_generation=request.conversation_generation,
            event_observer=observer,
        )

    if not _requires_partitioned_analysis(request):
        execution = _guard_stream(
            stream,
            lambda: dispatch(
                prompt_factory(
                    request.message,
                    request.document_context,
                ),
                stream.observe,
                (),
            ),
        )
        return TurnProviderDispatchResult(
            execution,
            stream,
            prompt_bytes,
        )

    projection = request.context_projection
    budget = request.context_budget
    plan = request.run_plan
    assert projection is not None
    assert budget is not None
    assert plan is not None
    context_service = runtime.DOCUMENT_CONTEXT_SERVICE
    assert context_service is not None
    base_prompt_characters = len(prompt_factory(request.message, "")) + 4_000

    def write_checkpoint(payload: dict[str, Any]) -> None:
        runtime.checkpoint_run_step(
            request.session,
            request.run_id,
            "execute",
            payload,
            expected_generation=request.conversation_generation,
        )

    def stopped() -> bool:
        with request.session.lock:
            return (
                request.session.stop_requested
                or request.session.run_id != request.run_id
                or request.session.closed
                or request.session.conversation_generation
                != request.conversation_generation
            )

    document = request.document
    assert document is not None
    with tempfile.TemporaryDirectory(
        prefix="ogent-visual-evidence-"
    ) as visual_directory:
        visual_preparer = VisualEvidencePreparer(
            runtime,
            document=document,
            revision_id=projection.revision_id,
            supported_modalities=budget.supported_modalities,
            output_root=Path(visual_directory),
        )
        partitioned = _guard_stream(
            stream,
            lambda: execute_partitioned_analysis(
                request=request.message,
                run_id=request.run_id,
                plan=plan,
                projection=projection,
                context_service=context_service,
                budget=budget,
                fixed_prompt_characters=base_prompt_characters,
                prompt_factory=prompt_factory,
                dispatch=dispatch,
                assistant_stream=stream,
                checkpoint=write_checkpoint,
                stop_requested=stopped,
                blob_store=getattr(
                    runtime,
                    "DURABLE_BLOB_STORE",
                    None,
                ),
                visual_evidence=lambda paths, characters: visual_preparer.prepare(
                    paths,
                    character_budget=characters,
                ),
                resume_checkpoint=_resume_checkpoint(
                    runtime,
                    request.session,
                    request.run_id,
                ),
            ),
        )
    return TurnProviderDispatchResult(
        partitioned.provider_result,
        stream,
        prompt_bytes,
        partitioned.public(),
    )


def _requires_partitioned_analysis(
    request: TurnProviderDispatchRequest,
) -> bool:
    return bool(
        request.run_contract.analysis_only
        and request.context_projection is not None
        and request.context_projection.partitions
        and request.context_budget is not None
        and request.run_plan is not None
        and request.runtime.DOCUMENT_CONTEXT_SERVICE is not None
    )


def _resume_checkpoint(
    runtime: Any,
    session: Any,
    run_id: str,
) -> dict[str, Any] | None:
    with session.lock:
        seeded = (
            dict(session.run_resume_checkpoint)
            if session.run_id == run_id and session.run_resume_checkpoint is not None
            else None
        )
        if seeded is not None:
            session.run_resume_checkpoint = None
    if seeded is not None:
        return seeded
    repository = getattr(runtime, "RUN_REPOSITORY", None)
    if repository is None:
        return None
    execute_step = next(
        (step for step in repository.steps(run_id) if step.step.step_id == "execute"),
        None,
    )
    return dict(execute_step.checkpoint) if execute_step is not None else None


def _guard_stream(
    stream: AssistantStreamAccumulator,
    action: Callable[[], Any],
) -> Any:
    try:
        return action()
    except Exception:
        stream.fail(cancelled=False)
        raise

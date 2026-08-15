"""Application coordinator for one accepted Ogent turn."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from ogent_app.domain.run import RunContract

from .document_context import ContextProjection, ProviderContextBudget
from .dynamic_runtime import DynamicRuntime
from .provider_events import AssistantStreamAccumulator
from .reference_preparation import prepare_references_for_run
from .turn_provider_dispatch import (
    TurnProviderDispatchRequest,
    dispatch_turn_provider,
)
from .turn_verification import VerificationRequest, verify_turn

SessionState = Any
ReferenceAttachment = Any
PreviewSelectionSnapshot = Any
RunTiming = Any


class TurnCoordinator:
    """Application boundary for one accepted, serialized document turn."""

    def __init__(self, runtime: DynamicRuntime) -> None:
        self.runtime = runtime

    def run(
        self,
        session: SessionState,
        message: str,
        document: Path | None,
        source: Path | None,
        provider: str,
        model: str,
        effort: str,
        run_id: str,
        references: list[ReferenceAttachment],
        selection_snapshot: PreviewSelectionSnapshot | None,
        user_sequence: int | None,
        timing: RunTiming,
        conversation_generation: int | None = None,
        client_id: str | None = None,
        run_contract: RunContract | None = None,
    ) -> None:
        run_turn(
            self.runtime,
            session,
            message,
            document,
            source,
            provider,
            model,
            effort,
            run_id,
            references,
            selection_snapshot,
            user_sequence,
            timing,
            conversation_generation=conversation_generation,
            client_id=client_id,
            run_contract=run_contract,
        )


def run_turn(  # noqa: C901
    runtime: DynamicRuntime,
    session: SessionState,
    message: str,
    document: Path | None,
    source: Path | None,
    provider: str,
    model: str,
    effort: str,
    run_id: str,
    references: list[ReferenceAttachment],
    selection_snapshot: PreviewSelectionSnapshot | None,
    user_sequence: int | None,
    timing: RunTiming,
    conversation_generation: int | None = None,
    client_id: str | None = None,
    run_contract: RunContract | None = None,
) -> None:
    """Execute the durable turn saga; reviewed exception in ARCHITECTURE.md."""
    AUTOMATIC_EFFORT = runtime.AUTOMATIC_EFFORT
    STATE = runtime.STATE
    SessionMemoryError = runtime.SessionMemoryError
    UserFacingError = runtime.UserFacingError
    _finish_session_run = runtime._finish_session_run
    _redact_reference_detail = runtime._redact_reference_detail
    cleanup_run_references = runtime.cleanup_run_references
    emit_references = runtime.emit_references
    ensure_watch = runtime.ensure_watch
    package_sha256 = runtime.package_sha256
    preview_identity_public = runtime.preview_identity_public
    provider_label = runtime.provider_label
    resolve_run_contract = runtime.resolve_run_contract
    stable_watch_url = runtime.stable_watch_url
    transition_run_step = runtime.transition_run_step
    checkpoint_run_step = runtime.checkpoint_run_step
    cancel_incomplete_run_steps = runtime.cancel_incomplete_run_steps

    with session.lock:
        if conversation_generation is None:
            conversation_generation = (
                session.run_conversation_generation or session.conversation_generation
            )
        if client_id is None:
            client_id = session.run_client_id
        claimed_contract = session.run_contract if session.run_id == run_id else None
        run_plan = session.run_plan if session.run_id == run_id else None
    assert conversation_generation is not None
    if run_contract is None:
        run_contract = claimed_contract
    if run_contract is None:
        # Compatibility for direct worker tests and callers predating the
        # start-run contract claim. Production runs always use the claimed
        # session contract.
        run_contract = resolve_run_contract(
            message,
            has_active_document=document is not None,
            has_attachments=bool(references),
            selected_paths=(
                target.path
                for target in (
                    selection_snapshot.targets if selection_snapshot is not None else ()
                )
            ),
        )
    assert run_contract is not None
    timing.mark(
        "run_contract",
        mode=run_contract.mode.value,
        scope=run_contract.scope.value,
        requires_mutation=run_contract.requires_mutation,
    )
    started = time.perf_counter()
    terminal_status = "error"
    terminal_extra: dict[str, Any] = {"kind": provider}
    references_cleaned = False
    provider_name = provider_label(provider)
    run_root: Path | None = None
    materialized_references: list[ReferenceAttachment] = []
    prepared_references: list[ReferenceAttachment] = []
    verification: dict[str, Any] = {
        "run_contract": run_contract.public(),
        "run_plan": (run_plan.public() if run_plan is not None else None),
    }
    capability_context = ""
    capability = None
    audit_log_path: Path | None = None
    gateway_receipts: tuple[Any, ...] = ()
    preview_baseline: Any = None
    initial_package_fingerprint: str | None = None
    rollback_snapshot = None
    rollback_performed = False
    context_projection: ContextProjection | None = None
    context_budget: ProviderContextBudget | None = None
    document_context = ""
    reference_context = ""
    assistant_stream: AssistantStreamAccumulator | None = None

    def step(
        step_id: str,
        target: Any,
        *,
        checkpoint: dict[str, Any] | None = None,
        step_verification: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any] | None:
        return transition_run_step(
            session,
            run_id,
            step_id,
            target,
            checkpoint=checkpoint,
            verification=step_verification,
            error_code=error_code,
            expected_generation=conversation_generation,
        )

    def restore_run_snapshot_if_changed(reason: str) -> bool:
        nonlocal rollback_performed
        if (
            rollback_performed
            or rollback_snapshot is None
            or document is None
            or runtime.ROLLBACK_MANAGER is None
            or not document.is_file()
            or package_sha256(document) == rollback_snapshot.package_sha256
        ):
            return False
        restored_hash = runtime.ROLLBACK_MANAGER.restore(
            rollback_snapshot,
            document,
        )
        rollback_performed = True
        verification.update(
            {
                "rolled_back": True,
                "rollback_reason": reason,
                "rollback_package_sha256": restored_hash,
            }
        )
        return True

    try:
        with session.lock:
            if (
                session.stop_requested
                or session.run_id != run_id
                or session.closed
                or session.conversation_generation != conversation_generation
            ):
                raise UserFacingError("Stopped. No further agent work is running.", 409)
        step("inspect", runtime.RunStepState.RUNNING)
        if document is not None and STATE.enforce_capability_contract:
            bootstrap = runtime.DOCUMENT_CAPABILITY_BOOTSTRAP
            if bootstrap is None:
                raise UserFacingError(
                    "The deterministic OfficeCLI capability service is unavailable.",
                    503,
                )
            if session.workspace_actor is None:
                raise UserFacingError(
                    "The durable workspace actor is unavailable.",
                    503,
                )
            session.workspace_actor.dispatch(
                runtime.TransitionRun(
                    run_id,
                    runtime.RunState.CAPABILITY_BOOTSTRAP,
                )
            )
            with session.lock:
                expected_document = session.active_doc
                expected_revision = session.document_revision
            if expected_document != document:
                raise UserFacingError(
                    "The active document changed before capability bootstrap.",
                    409,
                )
            capability = bootstrap.bootstrap(
                run_id=run_id,
                workspace_id=session.session_id,
                document=document,
                document_revision=expected_revision,
                scope=run_contract.scope,
                selected_paths=run_contract.selected_paths,
            )
            with session.lock:
                if (
                    session.active_doc != document
                    or session.document_revision != expected_revision
                    or session.run_id != run_id
                ):
                    raise UserFacingError(
                        "The active document changed during capability bootstrap.",
                        409,
                    )
                session.run_capability = capability
                session.document_package_sha256 = capability.receipt.package_sha256
            capability_context = capability.policy.text
            verification["capability_receipt"] = capability.receipt.public()
            timing.mark(
                "capability_bootstrap",
                skill=capability.policy.skill_name,
                officecli_version=capability.policy.officecli_version,
                probe_operation=capability.receipt.probe_operation,
            )
            session.emit(
                "capability",
                capability.receipt.public(),
                expected_generation=conversation_generation,
            )
            for target in (
                runtime.RunState.DOCUMENT_REFRESH,
                runtime.RunState.SCOPE_RESOLVED,
                runtime.RunState.PLAN_READY,
                runtime.RunState.EXECUTING,
            ):
                session.workspace_actor.dispatch(runtime.TransitionRun(run_id, target))
        elif document is None:
            verification["analysis_only_no_office_artifact"] = True
        if document is not None:
            audit_root = (
                runtime.WORK_ROOT / session.session_id / "tool-audits"
            ).resolve(strict=False)
            workspace_root = (runtime.WORK_ROOT / session.session_id).resolve(
                strict=False
            )
            audit_root.relative_to(workspace_root)
            audit_root.mkdir(parents=True, exist_ok=True)
            candidate_audit_log_path = Path(audit_root / f"{run_id}.jsonl")
            if candidate_audit_log_path.exists():
                raise UserFacingError(
                    "The run audit identifier already exists.",
                    409,
                )
            audit_log_path = candidate_audit_log_path
            ensure_watch(session)
            if document.is_file():
                initial_package_fingerprint = package_sha256(document)
            if (
                STATE.enforce_capability_contract
                and capability is not None
                and initial_package_fingerprint is not None
            ):
                if capability.receipt.package_sha256 != initial_package_fingerprint:
                    raise UserFacingError(
                        "The active document changed after OfficeCLI preflight.",
                        409,
                    )
                rollback_manager = runtime.ROLLBACK_MANAGER
                if rollback_manager is None:
                    raise UserFacingError(
                        "The per-run rollback service is unavailable.",
                        503,
                    )
                rollback_snapshot = rollback_manager.create(
                    document,
                    run_id=run_id,
                )
                verification["rollback_snapshot"] = {
                    "blob_id": rollback_snapshot.blob_id,
                    "package_sha256": rollback_snapshot.package_sha256,
                    "byte_size": rollback_snapshot.byte_size,
                }
            if (
                document.suffix.casefold() == ".docx"
                and initial_package_fingerprint is not None
            ):
                preview_baseline = session.preview_sync.begin_run(
                    package_sha256=initial_package_fingerprint,
                    client_id=client_id,
                )
        with session.lock:
            if (
                session.run_id != run_id
                or session.conversation_generation != conversation_generation
            ):
                return
            session.run_status = "working"
            document_mode = session.document_mode
        session.emit(
            "run",
            {
                "status": "working",
                "kind": provider,
                "run_id": run_id,
                "label": (
                    "Preparing retained attachments"
                    if references
                    else f"Running {provider_name}"
                ),
            },
            expected_generation=conversation_generation,
        )
        STATE.broadcast_sessions()
        effort_label = (
            "CLI-default effort" if effort == AUTOMATIC_EFFORT else f"{effort} effort"
        )
        session.add_activity(
            provider,
            f"Using {provider_name} model {model} with {effort_label}; "
            "fresh bounded Ogent memory context.",
            expected_generation=conversation_generation,
        )

        if document is not None:
            with session.lock:
                index_payload = (
                    dict(session.document_index)
                    if session.document_index is not None
                    else None
                )
            context_service = runtime.DOCUMENT_CONTEXT_SERVICE
            if (
                context_service is not None
                and run_plan is not None
                and index_payload is not None
                and index_payload.get("revision_id")
            ):
                catalog = runtime.AGENT_CATALOG.get_catalog(provider)
                model_capability = (
                    next(
                        (item for item in catalog.models if item.id == model),
                        None,
                    )
                    if catalog is not None
                    else None
                )
                context_budget = ProviderContextBudget.from_model_capability(
                    provider,
                    model,
                    model_capability,
                )
                with session.lock:
                    fast_run = session.run_fast and session.run_id == run_id
                if fast_run:
                    # Fast mode retrieves less document context; MCP use,
                    # validation, checkpoints, and error reporting are unchanged.
                    context_budget = context_budget.fast_variant()
                try:
                    context_projection = cast(
                        ContextProjection,
                        context_service.retrieve(
                            revision_id=str(index_payload["revision_id"]),
                            request=message,
                            plan=run_plan,
                            budget=context_budget,
                            run_id=run_id,
                            fixed_prompt_characters=(
                                len(message) + len(capability_context)
                            ),
                        ),
                    )
                except runtime.DocumentIndexNotReady as exc:
                    raise UserFacingError(str(exc), 409) from exc
                document_context = context_projection.text
                verification["context_projection"] = context_projection.public()
                checkpoint_run_step(
                    session,
                    run_id,
                    "inspect",
                    {
                        "revision_id": context_projection.revision_id,
                        "included_paths": list(context_projection.included_paths),
                        "partition_count": len(context_projection.partitions),
                        "next_partition": (
                            1 if context_projection.partitions else None
                        ),
                    },
                    expected_generation=conversation_generation,
                )
            elif run_plan is not None and run_plan.coverage_requirement.get(
                "require_complete_index"
            ):
                raise UserFacingError(
                    "Full-document work requires a ready current index.",
                    409,
                )
        step(
            "inspect",
            runtime.RunStepState.COMPLETED,
            step_verification={
                "revision_id": (
                    context_projection.revision_id
                    if context_projection is not None
                    else None
                ),
                "context_bounded": context_projection is not None,
            },
        )

        agent_derived: Path | None = None
        if references:
            step("references", runtime.RunStepState.RUNNING)
            prepared_run = prepare_references_for_run(
                runtime,
                session,
                run_id=run_id,
                references=references,
                message=message,
                expected_generation=conversation_generation,
                timing=timing,
            )
            materialized_references = list(prepared_run.materialized)
            prepared_references = list(prepared_run.prepared)
            run_root = prepared_run.run_root
            agent_derived = prepared_run.agent_derived
            reference_context = prepared_run.indexed_context
            step(
                "references",
                runtime.RunStepState.COMPLETED,
                step_verification={
                    "prepared": len(prepared_references),
                    "materialized": len(materialized_references),
                },
            )
        with session.lock:
            if (
                session.stop_requested
                or session.run_id != run_id
                or session.closed
                or session.conversation_generation != conversation_generation
            ):
                raise UserFacingError("Stopped. No further agent work is running.", 409)
        image_paths = [
            image_path
            for attachment in prepared_references
            for image_path in attachment.image_paths
        ]
        if document is not None:
            working_directory = document.parent
        elif agent_derived is not None:
            working_directory = agent_derived
        else:
            raise UserFacingError(
                "Open an Office document or use a retained attachment first.",
                409,
            )
        sandbox = "workspace-write" if run_contract.requires_mutation else "read-only"
        writable_directories = (
            [agent_derived]
            if document is not None and agent_derived is not None
            else []
        )
        session.emit(
            "run",
            {
                "status": "working",
                "kind": provider,
                "run_id": run_id,
                "label": f"{provider_name} is analyzing temporary references"
                if prepared_references
                else f"{provider_name} is editing",
            },
            expected_generation=conversation_generation,
        )
        selection_payload = (
            selection_snapshot.to_dict()["targets"]
            if selection_snapshot is not None
            else []
        )
        with session.lock:
            memory_current = session.conversation_generation == conversation_generation
        if session.durable_conversation is not None and memory_current:
            memory_context = session.durable_conversation.build_provider_context(
                current_user_sequence=user_sequence,
            )
        elif session.memory is not None and memory_current:
            context = session.memory.build_provider_context(
                message,
                provider=provider,
                model=model,
                effort=effort,
                fresh_context=True,
                current_user_sequence=user_sequence,
                new_attachment_ids=[item.attachment_id for item in references],
                preview_selections=selection_payload,
            )
            memory_context = context.text
        else:
            memory_context = ""
        timing.mark("prompt_ready")
        step("execute", runtime.RunStepState.RUNNING)
        dispatch_result = dispatch_turn_provider(
            TurnProviderDispatchRequest(
                runtime=runtime,
                session=session,
                message=message,
                document=document,
                source=source,
                provider=provider,
                model=model,
                effort=effort,
                run_id=run_id,
                references=references,
                prepared_references=prepared_references,
                run_root=run_root,
                memory_context=memory_context,
                selection_snapshot=selection_snapshot,
                document_mode=document_mode,
                run_contract=run_contract,
                capability_context=capability_context,
                document_context=document_context,
                reference_context=reference_context,
                context_projection=context_projection,
                context_budget=context_budget,
                run_plan=run_plan,
                working_directory=working_directory,
                image_paths=image_paths,
                sandbox=sandbox,
                writable_directories=writable_directories,
                timing=timing,
                audit_log_path=audit_log_path,
                capability=capability,
                initial_package_sha256=initial_package_fingerprint,
                conversation_generation=conversation_generation,
            )
        )
        provider_result = dispatch_result.execution
        assistant_stream = dispatch_result.assistant_stream
        timing.prompt_bytes = dispatch_result.prompt_bytes
        if dispatch_result.partitioned_execution is not None:
            verification["partitioned_execution"] = (
                dispatch_result.partitioned_execution
            )
        code = provider_result.exit_code
        final_text = provider_result.final_text
        stderr_tail = list(provider_result.stderr_tail)
        verification["provider_transport"] = provider_result.transport
        if audit_log_path is not None and audit_log_path.exists():
            ingestor = runtime.GATEWAY_AUDIT_INGESTOR
            if ingestor is None:
                raise UserFacingError(
                    "The OfficeCLI audit service is unavailable.",
                    503,
                )
            gateway_receipts = ingestor.ingest(
                audit_log_path,
                run_id=run_id,
            )
            verification["gateway_receipts"] = [
                {
                    "id": receipt.receipt_id,
                    "operation": receipt.operation,
                    "exit_status": receipt.exit_status,
                    "mutation_category": receipt.mutation_category.value,
                    "package_sha256": receipt.package_sha256,
                    "result": receipt.result,
                }
                for receipt in gateway_receipts
            ]
            audit_log_path.unlink()
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        with session.lock:
            stopped = (
                session.stop_requested
                or session.run_id != run_id
                or session.closed
                or session.conversation_generation != conversation_generation
            )
        if stopped:
            assistant_stream.fail(cancelled=True)
            cancel_incomplete_run_steps(
                session,
                run_id,
                expected_generation=conversation_generation,
            )
            restore_run_snapshot_if_changed("cancelled")
            session.add_message(
                "assistant",
                "Stopped. No further agent work is running.",
                provider=provider,
                model=model,
                effort=effort,
                run_outcome="stopped",
                expected_generation=conversation_generation,
            )
            terminal_status = "stopped"
            terminal_extra["elapsed_ms"] = elapsed_ms
            return
        if timing.focused_scope_violation:
            code = 2
            stderr_tail.append(timing.focused_scope_violation)
        if code != 0:
            assistant_stream.fail(cancelled=False)
            execute_checkpoint = {
                "provider_exit_code": code,
                **(dispatch_result.partitioned_execution or {}),
            }
            step(
                "execute",
                runtime.RunStepState.FAILED,
                checkpoint=execute_checkpoint,
                step_verification={"provider_exit_code": code},
                error_code=f"ProviderExit{code}",
            )
            restore_run_snapshot_if_changed("provider_failed")
            detail = "\n".join(stderr_tail[-6:]).strip()
            message_text = f"{provider_name} exited with code {code}."
            if detail:
                message_text += f" {detail}"
            with session.lock:
                session.last_error = message_text
            session.add_message(
                "assistant",
                message_text,
                provider=provider,
                model=model,
                effort=effort,
                run_outcome="error",
                expected_generation=conversation_generation,
            )
            terminal_status = "error"
            terminal_extra.update({"exit_code": code, "elapsed_ms": elapsed_ms})
            return
        step(
            "execute",
            runtime.RunStepState.COMPLETED,
            checkpoint={
                "provider_exit_code": code,
                "provider_output_characters": len(final_text or ""),
                **(dispatch_result.partitioned_execution or {}),
            },
            step_verification={"provider_exit_code": code},
        )
        verification_result = verify_turn(
            VerificationRequest(
                runtime=runtime,
                session=session,
                document=document,
                run_id=run_id,
                run_contract=run_contract,
                capability=capability,
                initial_package_sha256=initial_package_fingerprint,
                gateway_receipts=gateway_receipts,
                rollback_snapshot=rollback_snapshot,
                preview_baseline=preview_baseline,
                conversation_generation=conversation_generation,
                verification=verification,
                final_text=final_text or "",
                step=step,
            )
        )
        successful_outcome = verification_result.outcome
        final_text = verification_result.final_text
        verification["completion_kind"] = successful_outcome
        final_text = assistant_stream.finalize(final_text)
        session.add_message(
            "assistant",
            final_text,
            provider=provider,
            model=model,
            effort=effort,
            run_outcome=successful_outcome,
            verification=verification,
            expected_generation=conversation_generation,
        )
        with session.lock:
            memory_current = session.conversation_generation == conversation_generation
        if session.durable_conversation is not None and memory_current:
            session.update_run_request_outcome(
                successful_outcome,
                verification=verification,
                completed_actions=(
                    [final_text] if successful_outcome == "edit_completed" else []
                ),
            )
        elif session.memory is not None and memory_current:
            if user_sequence is not None:
                session.memory.update_turn_outcome(
                    user_sequence,
                    outcome=successful_outcome,
                    verification=verification,
                    completed_actions=(
                        [final_text] if successful_outcome == "edit_completed" else []
                    ),
                )
            session.memory.mark_provider_synced(
                provider,
                model,
                session.memory.sequence,
            )
            session.memory.record_run_summary(
                provider=provider,
                model=model,
                effort=effort,
                outcome=successful_outcome,
                verification=verification,
            )
        if document is not None:
            ensure_watch(session)
            with session.lock:
                watch_port = session.watch_port
                watch_generation = session.watch_generation
                document_revision = session.document_revision
            session.emit(
                "document",
                {
                    "source": str(source) if source else None,
                    "working": str(document),
                    "watch_url": stable_watch_url(
                        watch_port,
                        watch_generation,
                    ),
                    "watch_port": watch_port,
                    "watch_generation": watch_generation,
                    "document_revision": document_revision,
                    "preview_identity": preview_identity_public(session),
                    "complex_layout": session.complex_layout,
                    "complex_layout_detail": session.complex_layout_detail,
                },
                expected_generation=conversation_generation,
            )
        terminal_status = successful_outcome
        terminal_extra.update({"exit_code": 0, "elapsed_ms": elapsed_ms})
    except Exception as exc:
        rollback_failure: Exception | None = None
        try:
            restore_run_snapshot_if_changed("run_failed")
        except Exception as rollback_exc:
            rollback_failure = rollback_exc
            verification["rollback_failed"] = str(rollback_exc)
        with session.lock:
            stopped = (
                session.stop_requested
                or session.run_id != run_id
                or session.conversation_generation != conversation_generation
            )
        if stopped:
            if assistant_stream is not None:
                assistant_stream.fail(cancelled=True)
            cancel_incomplete_run_steps(
                session,
                run_id,
                expected_generation=conversation_generation,
            )
            session.add_message(
                "assistant",
                "Stopped. No further agent work is running.",
                provider=provider,
                model=model,
                effort=effort,
                run_outcome="stopped",
                expected_generation=conversation_generation,
            )
            terminal_status = "stopped"
        else:
            if assistant_stream is not None:
                assistant_stream.fail(cancelled=False)
            with session.lock:
                running_step = next(
                    (
                        str(item.get("id"))
                        for item in session.run_steps
                        if item.get("state") == runtime.RunStepState.RUNNING.value
                    ),
                    None,
                )
            if running_step is not None:
                try:
                    step(
                        running_step,
                        runtime.RunStepState.FAILED,
                        error_code=type(exc).__name__,
                        step_verification={"failed": True},
                    )
                except Exception as projection_error:
                    runtime.backend_log(
                        "run_step_failure_projection_failed",
                        session_id=session.session_id,
                        run_id=run_id,
                        error_type=type(projection_error).__name__,
                    )
            detail = _redact_reference_detail(
                (
                    f"{exc} Rollback also failed: {rollback_failure}"
                    if rollback_failure is not None
                    else str(exc)
                ),
                attachments=references,
                max_characters=1600,
            )
            with session.lock:
                session.last_error = detail
            label = (
                "The reference run failed" if references else "The document run failed"
            )
            session.add_message(
                "assistant",
                f"{label}: {detail}",
                provider=provider,
                model=model,
                effort=effort,
                run_outcome="error",
                expected_generation=conversation_generation,
            )
            terminal_status = "error"
        with session.lock:
            memory_current = session.conversation_generation == conversation_generation
        if session.durable_conversation is not None and memory_current:
            outcome = "stopped" if stopped else "error"
            try:
                session.update_run_request_outcome(
                    outcome,
                    verification=verification,
                )
            except Exception as projection_error:
                runtime.backend_log(
                    "run_outcome_projection_failed",
                    session_id=session.session_id,
                    run_id=run_id,
                    error_type=type(projection_error).__name__,
                )
        elif session.memory is not None and memory_current:
            outcome = "stopped" if stopped else "error"
            if user_sequence is not None:
                try:
                    session.memory.update_turn_outcome(
                        user_sequence,
                        outcome=outcome,
                        verification=verification,
                    )
                except SessionMemoryError as projection_error:
                    runtime.backend_log(
                        "legacy_turn_outcome_projection_failed",
                        session_id=session.session_id,
                        run_id=run_id,
                        error_type=type(projection_error).__name__,
                    )
            session.memory.record_run_summary(
                provider=provider,
                model=model,
                effort=effort,
                outcome=outcome,
                verification=verification,
            )
    finally:
        if references:
            try:
                references_cleaned = cleanup_run_references(session, run_id)
            except Exception as cleanup_exc:
                detail = _redact_reference_detail(
                    str(cleanup_exc),
                    max_characters=1600,
                )
                with session.reference_lock:
                    for attachment in materialized_references:
                        attachment.status = "Failed"
                        attachment.error_message = detail
                emit_references(
                    session,
                    expected_generation=conversation_generation,
                )
                with session.lock:
                    session.last_error = detail
                session.add_activity(
                    "references",
                    f"Materialized attachment cleanup failed: {detail}",
                    expected_generation=conversation_generation,
                )
                terminal_status = "error"
            if references_cleaned:
                session.add_activity(
                    "references",
                    "Materialized run copies deleted; canonical attachments "
                    "remain available in this workspace.",
                    expected_generation=conversation_generation,
                )
        outcome_for_timing = (
            "completed" if terminal_status == "completed" else terminal_status
        )
        timing_result = timing.finish(
            outcome=outcome_for_timing,
            usage=session.last_provider_usage,
        )
        session.add_activity(
            "timing",
            timing.concise_line(),
            expected_generation=conversation_generation,
        )
        with session.lock:
            session.last_timing = timing_result
            session.active_timing = None
        _finish_session_run(
            session,
            run_id,
            terminal_status,
            expected_generation=conversation_generation,
            **terminal_extra,
        )

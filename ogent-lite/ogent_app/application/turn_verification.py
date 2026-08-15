"""Server-owned verification, revision, preview, and changeset stages."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any, Callable


StepTransition = Callable[..., dict[str, Any] | None]


@dataclasses.dataclass(slots=True)
class VerificationRequest:
    runtime: Any
    session: Any
    document: Path | None
    run_id: str
    run_contract: Any
    capability: Any
    initial_package_sha256: str | None
    gateway_receipts: tuple[Any, ...]
    rollback_snapshot: Any
    preview_baseline: Any
    conversation_generation: int
    verification: dict[str, Any]
    final_text: str
    step: StepTransition


@dataclasses.dataclass(frozen=True, slots=True)
class VerificationResult:
    outcome: str
    final_text: str
    document_mutated: bool
    final_package_sha256: str | None


def verify_turn(request: VerificationRequest) -> VerificationResult:
    """Verify a provider result and publish its document-side proof."""
    runtime = request.runtime
    request.step("verify", runtime.RunStepState.RUNNING)
    outcome = (
        "edit_completed"
        if request.run_contract.requires_mutation
        else "analysis_completed"
    )
    final_text = request.final_text or _default_completion_text(request.document)
    if request.document is None:
        request.step(
            "verify",
            runtime.RunStepState.COMPLETED,
            step_verification={
                "completion_kind": outcome,
                "analysis_only": True,
            },
        )
        return VerificationResult(outcome, final_text, False, None)

    if (
        runtime.STATE.enforce_capability_contract
        and request.capability is not None
        and request.initial_package_sha256 is not None
    ):
        outcome, final_text, document_mutated, final_sha256 = _verify_capability_run(
            request, outcome, final_text
        )
    else:
        outcome, final_text, document_mutated, final_sha256 = _verify_compatibility_run(
            request, outcome, final_text
        )

    if document_mutated:
        _advance_document_revision(request, final_sha256)
    _complete_verification_step(request, outcome, document_mutated)
    _synchronize_preview(request, document_mutated, final_sha256, final_text)
    final_text = _preview_adjusted_text(
        request,
        document_mutated,
        final_sha256,
        final_text,
    )
    _record_changeset(request)
    return VerificationResult(
        outcome,
        final_text,
        document_mutated,
        final_sha256,
    )


def _default_completion_text(document: Path | None) -> str:
    if document is not None:
        return "The document task completed. Review the live document on the left."
    return "The retained attachments were analyzed."


def _verify_capability_run(
    request: VerificationRequest,
    outcome: str,
    final_text: str,
) -> tuple[str, str, bool, str]:
    runtime = request.runtime
    session = request.session
    session.workspace_actor.dispatch(
        runtime.TransitionRun(
            request.run_id,
            runtime.RunState.VERIFYING,
        )
    )
    verifier = runtime.OUTCOME_VERIFIER
    if verifier is None:
        raise runtime.UserFacingError(
            "The server-side outcome verifier is unavailable.",
            503,
        )
    decision = verifier.verify(
        run_id=request.run_id,
        contract=request.run_contract,
        document=request.document,
        initial_package_sha256=request.initial_package_sha256,
        capability=request.capability,
        provider_receipts=request.gateway_receipts,
        expected_paths=request.run_contract.selected_paths,
    )
    final_sha256 = runtime.package_sha256(request.document)
    document_mutated = decision.package_changed
    request.verification.update(decision.assertions)
    request.verification.update(
        {
            "document_mutated": document_mutated,
            "mutation_evidence": decision.accepted and document_mutated,
            "affected_paths": list(decision.affected_paths),
        }
    )
    if decision.rollback_required:
        _restore_rejected_run(request, decision.reason)
    outcome = decision.outcome
    if outcome == "no_change":
        final_text = "No change was made."
    session.workspace_actor.dispatch(
        runtime.TransitionRun(
            request.run_id,
            runtime.RunState.PREVIEW_SYNC,
        )
    )
    return outcome, final_text, document_mutated, final_sha256


def _restore_rejected_run(
    request: VerificationRequest,
    reason: str | None,
) -> None:
    runtime = request.runtime
    if request.rollback_snapshot is None:
        raise runtime.UserFacingError(
            "The run failed and no rollback snapshot is available.",
            500,
        )
    restored_hash = runtime.ROLLBACK_MANAGER.restore(
        request.rollback_snapshot,
        request.document,
    )
    request.verification.update(
        {
            "rolled_back": True,
            "rollback_package_sha256": restored_hash,
        }
    )
    raise runtime.UserFacingError(
        reason or "The document run failed server-side verification.",
        500,
    )


def _verify_compatibility_run(
    request: VerificationRequest,
    outcome: str,
    final_text: str,
) -> tuple[str, str, bool, str]:
    runtime = request.runtime
    document = request.document
    assert document is not None
    started = time.perf_counter()
    validation = runtime.run_quiet(
        ["officecli", "validate", str(document), "--json"],
        cwd=document.parent,
        timeout=90,
    )
    request.verification.update(
        {
            "officecli_validate": validation.returncode == 0,
            "validation_ms": round((time.perf_counter() - started) * 1000),
            "pre_package_sha256": request.initial_package_sha256,
        }
    )
    if validation.returncode != 0:
        raise runtime.UserFacingError(
            "OfficeCLI validation failed after the document run.",
            500,
        )
    final_sha256 = runtime.package_sha256(document)
    document_mutated = (
        request.initial_package_sha256 is not None
        and final_sha256 != request.initial_package_sha256
    )
    request.verification.update(
        {
            "post_package_sha256": final_sha256,
            "document_mutated": document_mutated,
            "mutation_evidence": document_mutated,
        }
    )
    if request.run_contract.requires_mutation and not document_mutated:
        return "no_change", "No change was made.", False, final_sha256
    if request.run_contract.analysis_only and document_mutated:
        request.verification["unexpected_mutation"] = True
        raise runtime.UserFacingError(
            "A read-only document run changed the active file. The run was rejected.",
            500,
        )
    return outcome, final_text, document_mutated, final_sha256


def _advance_document_revision(
    request: VerificationRequest,
    final_sha256: str,
) -> None:
    runtime = request.runtime
    session = request.session
    associated_mutation = (
        session.preview_sync.associate_latest_mutation(
            request.preview_baseline,
            final_sha256,
        )
        if request.preview_baseline is not None
        else None
    )
    revision, advanced = runtime.advance_document_revision(
        session,
        request.document,
        final_sha256,
        watch_event_fingerprint=(
            associated_mutation.event_fingerprint
            if associated_mutation is not None
            else None
        ),
    )
    request.verification["document_revision"] = revision
    if not advanced:
        return
    session.preview_selection.advance_revision(revision)
    runtime.emit_preview_selection(
        session,
        expected_generation=request.conversation_generation,
    )
    session.emit(
        "document_revision",
        {"revision": revision},
        expected_generation=request.conversation_generation,
    )


def _complete_verification_step(
    request: VerificationRequest,
    outcome: str,
    document_mutated: bool,
) -> None:
    request.step(
        "verify",
        request.runtime.RunStepState.COMPLETED,
        step_verification={
            "completion_kind": outcome,
            "document_mutated": document_mutated,
            "officecli_validate": request.verification.get(
                "officecli_validate",
                True,
            ),
        },
    )


def _synchronize_preview(
    request: VerificationRequest,
    document_mutated: bool,
    final_sha256: str,
    final_text: str,
) -> None:
    del final_text
    runtime = request.runtime
    document = request.document
    assert document is not None
    request.step("preview", runtime.RunStepState.RUNNING)
    if document.suffix.casefold() == ".docx":
        _confirm_docx_preview(request, document_mutated, final_sha256)
    request.step(
        "preview",
        runtime.RunStepState.COMPLETED,
        step_verification={
            "preview": request.verification.get(
                "preview",
                {
                    "confirmed": True,
                    "status": "live",
                },
            )
        },
    )


def _confirm_docx_preview(
    request: VerificationRequest,
    document_mutated: bool,
    final_sha256: str,
) -> None:
    runtime = request.runtime
    session = request.session
    if document_mutated and request.preview_baseline is not None:
        confirmation = runtime.confirm_word_preview(
            session,
            request.document,
            request.preview_baseline,
            final_sha256,
            expected_generation=request.conversation_generation,
        )
        request.verification["preview"] = confirmation.public_metadata()
        return
    if document_mutated:
        confirmation = runtime.PreviewConfirmation(
            confirmed=False,
            status="degraded",
            message=runtime.PREVIEW_DEGRADED_MESSAGE,
            recovery="baseline_unavailable",
        )
        runtime.set_preview_update_status(
            session,
            "degraded",
            runtime.PREVIEW_DEGRADED_MESSAGE,
            confirmation=confirmation,
            expected_generation=request.conversation_generation,
        )
        request.verification["preview"] = confirmation.public_metadata()
        return
    runtime.set_preview_update_status(
        session,
        "unchanged",
        "No preview refresh needed.",
        expected_generation=request.conversation_generation,
    )
    request.verification["preview"] = {
        "confirmed": True,
        "status": "unchanged",
        "message": "No preview refresh needed.",
    }


def _preview_adjusted_text(
    request: VerificationRequest,
    document_mutated: bool,
    final_sha256: str,
    final_text: str,
) -> str:
    del final_sha256
    document = request.document
    assert document is not None
    if (
        document.suffix.casefold() == ".docx"
        and document_mutated
        and not bool(request.verification.get("preview", {}).get("confirmed"))
    ):
        return f"{final_text.rstrip()}\n\n{request.runtime.PREVIEW_DEGRADED_MESSAGE}"
    return final_text


def _record_changeset(request: VerificationRequest) -> None:
    runtime = request.runtime
    if (
        not runtime.STATE.enforce_capability_contract
        or request.rollback_snapshot is None
    ):
        return
    affected_paths = tuple(request.run_contract.selected_paths or ("/",))
    changeset_id = runtime.ROLLBACK_MANAGER.record_changeset(
        request.rollback_snapshot,
        post_revision_sha256=runtime.package_sha256(request.document),
        affected_paths=affected_paths,
        assertions=request.verification,
    )
    request.verification["changeset_id"] = changeset_id

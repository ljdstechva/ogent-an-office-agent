"""Provider dispatch for a prepared Ogent turn."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

from ogent_app.infrastructure.fault_injection import FaultPoint


@dataclass(frozen=True, slots=True)
class ProviderExecutionResult:
    """Provider process outcome needed by the turn coordinator."""

    exit_code: int
    final_text: str
    stderr_tail: tuple[str, ...]
    transport: dict[str, Any]


def execute_provider(
    runtime: Any,
    *,
    session: Any,
    provider: str,
    prompt: str,
    working_directory: Path,
    model: str,
    effort: str,
    run_id: str,
    image_paths: list[Path],
    sandbox: str,
    writable_directories: list[Path],
    document: Path | None,
    references: list[Any] | tuple[Any, ...],
    timing: Any,
    run_contract: Any,
    audit_log_path: Path | None,
    capability: Any,
    initial_package_sha256: str | None,
    run_root: Path | None,
    conversation_generation: int,
    event_observer: Callable[[str, dict[str, Any]], None] | None = None,
) -> ProviderExecutionResult:
    """Execute the configured provider with one normalized request contract."""

    fault_injector = getattr(runtime, "FAULT_INJECTOR", None)
    if fault_injector is not None:
        fault_injector.trigger(FaultPoint.PROVIDER_CRASH)

    transport_policy = getattr(
        runtime,
        "PROVIDER_TRANSPORT_POLICY",
        None,
    )
    transport = (
        transport_policy.decide(
            provider,
            model,
            transport_available=False,
            workspace_isolated=True,
            sessions_resumable=False,
        ).public()
        if transport_policy is not None
        else {
            "provider": provider,
            "model": model,
            "requested_warm": False,
            "selected": "cold_process",
            "reason": "policy_unavailable",
            "workspace_isolated": True,
        }
    )

    allowed_document_paths = (
        run_contract.selected_paths
        if run_contract.scope
        in {
            runtime.ScopeMode.SELECTED_ONLY,
            runtime.ScopeMode.LOCAL_REGION,
        }
        else ()
    )
    capability_receipt = capability.receipt if capability is not None else None
    capability_policy = capability.policy if capability is not None else None
    common = {
        "office_document": document,
        "allow_document_mutation": run_contract.requires_mutation,
        "references": references,
        "timing": timing,
        "scope_mode": run_contract.scope,
        "allowed_document_paths": allowed_document_paths,
        "audit_log_path": audit_log_path,
        "document_revision": (
            capability_receipt.document_revision
            if capability_receipt is not None
            else None
        ),
        "capability_skill_name": (
            capability_policy.skill_name if capability_policy is not None else None
        ),
        "capability_skill_sha256": (
            capability_policy.policy_sha256 if capability_policy is not None else None
        ),
        "initial_package_sha256": initial_package_sha256,
        "conversation_generation": conversation_generation,
        "event_observer": event_observer,
    }
    if provider == "codex":
        code, provider_session_id, final_text, stderr_tail = runtime._run_codex_once(
            session,
            prompt,
            working_directory,
            None,
            model,
            effort,
            run_id,
            image_paths=image_paths,
            sandbox=sandbox,
            writable_directories=writable_directories,
            **common,
        )
    else:
        additional_directories = [run_root] if run_root is not None else []
        code, provider_session_id, final_text, stderr_tail = runtime._run_claude_once(
            session,
            prompt,
            working_directory,
            None,
            model,
            effort,
            run_id,
            ephemeral=True,
            additional_directories=additional_directories,
            **common,
        )
    del provider_session_id
    return ProviderExecutionResult(
        code,
        final_text,
        tuple(stderr_tail),
        transport,
    )

"""Lock-safe public projection for one compatibility workspace."""

from __future__ import annotations

from typing import Any, Callable


def build_workspace_snapshot(
    session: Any,
    *,
    preview_selection_public: Callable[[Any], dict[str, Any]],
    stable_watch_url: Callable[[int | None, str], str | None],
    watch_http_alive: Callable[[int | None], bool],
    include_watch_probe: bool = True,
) -> dict[str, Any]:
    with session.reference_lock:
        references = [
            attachment.public_metadata() for attachment in session.pending_references
        ]
        retained = [
            attachment.public_metadata()
            for attachment in session.retained_references.values()
        ]
    preview_selection = preview_selection_public(session)
    memory_summary = (
        {
            **(
                session.memory.summary()
                if session.memory is not None
                else {
                    "session_id": session.session_id,
                    "created_at": session.created_at_iso,
                    "retained_attachments": 0,
                    "retained_attachment_bytes": 0,
                }
            ),
            "retained_turns": session.transcript_count(),
            "durable": True,
        }
        if session.durable_conversation is not None
        else session.memory.summary()
        if session.memory is not None
        else {
            "session_id": session.session_id,
            "created_at": session.created_at_iso,
            "retained_turns": session.transcript_count(),
            "retained_attachments": 0,
            "retained_attachment_bytes": 0,
        }
    )
    with session.lock:
        active_doc = str(session.active_doc) if session.active_doc else None
        active_source = str(session.active_source) if session.active_source else None
        watch_port = session.watch_port
        watch_generation = session.watch_generation
        snapshot = {
            "session_id": session.session_id,
            "created_at": session.created_at_iso,
            "active_document": active_doc,
            "source_document": active_source,
            "watch_port": watch_port,
            "watch_generation": watch_generation or None,
            "watch_url": stable_watch_url(watch_port, watch_generation),
            "run_status": session.run_status,
            "last_run_outcome": session.last_run_outcome,
            "run_id": session.run_id,
            "run_contract": (
                session.run_contract.public()
                if session.run_contract is not None
                else None
            ),
            "run_plan": (
                session.run_plan.public() if session.run_plan is not None else None
            ),
            "run_steps": [dict(step) for step in session.run_steps],
            "assistant_stream": (
                dict(session.assistant_stream)
                if session.assistant_stream is not None
                else None
            ),
            "capability_receipt": (
                session.run_capability.receipt.public()
                if session.run_capability is not None
                else None
            ),
            "conversation_generation": session.conversation_generation,
            "transcript": (
                []
                if session.durable_conversation is not None
                else list(session.transcript)
            ),
            "transcript_paged": session.durable_conversation is not None,
            "transcript_total": session.transcript_count(),
            "transcript_page_url": (
                f"/api/workspaces/{session.session_id}/turns"
                if session.durable_conversation is not None
                else None
            ),
            "last_error": session.last_error,
            "preview_update_status": session.preview_update_status,
            "preview_update_message": session.preview_update_message,
            "preview_confirmation": (
                dict(session.preview_confirmation)
                if session.preview_confirmation is not None
                else None
            ),
            "codex_context": bool(session.codex_thread_id),
            "agent_contexts": {
                "codex": bool(session.codex_thread_id),
                "claude": bool(session.claude_session_id),
            },
            "sequence": session.sequence,
            "sse_clients": session.sse_clients,
            "orphan_since": session.orphan_since,
            "complex_layout": session.complex_layout,
            "complex_layout_detail": session.complex_layout_detail,
            "snapshot_in_progress": session.snapshot_in_progress,
            "snapshot_available": bool(
                session.snapshot_path and session.snapshot_path.is_file()
            ),
            "snapshot_cache_key": session.snapshot_cache_key,
            "snapshot_document_id": session.snapshot_document_id,
            "snapshot_document_revision": session.snapshot_document_revision,
            "snapshot_error": session.snapshot_error,
            "references": references,
            "retained_attachments": retained,
            "document_mode": session.document_mode,
            "document_id": session.document_id or None,
            "document_revision": session.document_revision,
            "document_index": (
                dict(session.document_index)
                if session.document_index is not None
                else None
            ),
            "source_document_index": (
                dict(session.source_document_index)
                if session.source_document_index is not None
                else None
            ),
            "recovery_backup": (
                session.recovery_backup.public_metadata()
                if session.recovery_backup
                else None
            ),
            "preview_selection": preview_selection,
            "session_memory": memory_summary,
            "last_timing": (dict(session.last_timing) if session.last_timing else None),
        }
        snapshot["preview_identity"] = (
            {
                "session_id": session.session_id,
                "document_id": session.document_id,
                "watch_port": watch_port,
                "watch_generation": watch_generation,
            }
            if active_doc
            and session.document_id
            and watch_port is not None
            and watch_generation
            else None
        )
    snapshot["watch_alive"] = (
        bool(active_doc) and watch_http_alive(watch_port)
        if include_watch_probe
        else False
    )
    return snapshot

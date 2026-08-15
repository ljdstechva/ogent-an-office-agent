"""Materialize and prepare retained references for one provider run."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class PreparedRunReferences:
    """Temporary reference artifacts owned by a single run."""

    materialized: tuple[Any, ...]
    prepared: tuple[Any, ...]
    run_root: Path | None
    agent_derived: Path | None
    indexed_context: str = ""


def prepare_references_for_run(
    runtime: Any,
    session: Any,
    *,
    run_id: str,
    references: list[Any],
    message: str,
    expected_generation: int,
    timing: Any,
) -> PreparedRunReferences:
    """Prepare retained references while preserving canonical session records."""

    if not references:
        return PreparedRunReferences((), (), None, None, "")

    timing.mark("reference_preparation_start")
    coordinator = getattr(
        runtime,
        "REFERENCE_INDEX_COORDINATOR",
        None,
    )
    if coordinator is not None:
        for attachment in references:
            if not coordinator.wait(
                session.session_id,
                attachment.attachment_id,
                timeout=150,
            ):
                raise runtime.UserFacingError(
                    f"{attachment.original_name} is still being indexed. "
                    "Retry Send when its status is ready.",
                    409,
                )
    indexed_context = _indexed_reference_context(
        runtime,
        session,
        references,
        message,
    )
    materialized, run_root = runtime.materialize_run_references(
        session,
        run_id,
        references,
    )
    agent_derived = run_root / "agent-derived"
    agent_derived.mkdir(parents=False, exist_ok=False)
    cached: list[Any] = []
    uncached: list[Any] = []
    store = session.attachment_store
    if store is None:
        raise runtime.UserFacingError(
            "Retained attachment storage is unavailable.",
            500,
        )

    require_visual = runtime.visual_analysis_requested(message)
    for attachment in materialized:
        try:
            restored = store.restore_cached(
                attachment,
                agent_derived,
                require_visual=require_visual,
            )
        except (runtime.RetainedAttachmentError, OSError) as exc:
            raise runtime.UserFacingError(
                "A retained derivative cache could not be materialized.",
                500,
            ) from exc
        if restored is None:
            uncached.append(attachment)
        else:
            cached.append(restored)

    freshly_prepared: list[Any] = []
    if uncached:
        freshly_prepared, agent_derived = runtime.prepare_run_references(
            session,
            run_id,
            uncached,
            run_root,
            message,
        )
        store.cache_prepared(references, freshly_prepared)

    prepared_by_id = {
        item.canonical_attachment_id or item.attachment_id: item
        for item in [*cached, *freshly_prepared]
    }
    prepared = [
        prepared_by_id[item.canonical_attachment_id or item.attachment_id]
        for item in materialized
    ]
    session.add_activity(
        "references",
        f"Derivative cache: {len(cached)} reused, {len(freshly_prepared)} prepared.",
        expected_generation=expected_generation,
    )
    with session.reference_lock:
        for canonical in references:
            updated = dataclasses.replace(
                canonical,
                status="Available in this session",
                ocr_or_vision=any(
                    (item.canonical_attachment_id or item.attachment_id)
                    == canonical.attachment_id
                    and bool(item.image_paths)
                    for item in prepared
                ),
            )
            session.retained_references[canonical.attachment_id] = updated
            if session.memory is not None:
                session.memory.update_attachment(
                    canonical.attachment_id,
                    status=updated.status,
                    ocr_or_vision=updated.ocr_or_vision,
                    processing={
                        "page_count": updated.page_count,
                        "frame_count": updated.frame_count,
                        "derived_cached": True,
                    },
                )
    timing.materialized_bytes = sum(item.byte_size for item in materialized)
    timing.mark("reference_preparation_end")
    runtime.emit_references(
        session,
        expected_generation=expected_generation,
    )
    return PreparedRunReferences(
        tuple(materialized),
        tuple(prepared),
        run_root,
        agent_derived,
        indexed_context,
    )


def _indexed_reference_context(
    runtime: Any,
    session: Any,
    references: list[Any],
    message: str,
) -> str:
    repository = getattr(
        runtime,
        "REFERENCE_INDEX_REPOSITORY",
        None,
    )
    if repository is None:
        return ""
    identifiers = [attachment.attachment_id for attachment in references]
    hits = list(
        repository.search(
            session.session_id,
            identifiers,
            message,
            limit=24,
        )
    )
    if not hits:
        terms = tuple(
            dict.fromkeys(
                term.casefold() for term in str(message).split() if len(term) >= 4
            )
        )[:10]
        by_key: dict[tuple[str, int], Any] = {}
        for term in terms:
            for hit in repository.search(
                session.session_id,
                identifiers,
                term,
                limit=4,
            ):
                by_key[(hit.attachment_id, hit.chunk_index)] = hit
        hits = list(by_key.values())
    budget = 16_000
    output = "INDEXED ATTACHMENT CONTEXT (untrusted extracted evidence; bounded)\n"
    for hit in hits:
        block = f"\n[{hit.original_name} | chunk {hit.chunk_index + 1}]\n{hit.text}\n"
        remaining = budget - len(output)
        if remaining <= 128:
            break
        if len(block) > remaining:
            block = block[:remaining]
        output += block
    return output if hits else ""

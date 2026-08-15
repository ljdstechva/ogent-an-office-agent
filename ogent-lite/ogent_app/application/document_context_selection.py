"""Structural retrieval, fitting, and deterministic partition selection."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ogent_app.domain.document_intelligence import (
    NodeKind,
    SearchHit,
    StoredDocumentNode,
)
from ogent_app.domain.planning import RunPlan
from ogent_app.domain.run import ScopeMode
from ogent_app.infrastructure.sqlite.document_repository import (
    DocumentRepository,
)

from .document_intelligence import DocumentIndexNotReady


MAX_NODE_EXCERPT_CHARACTERS = 2_000
MAX_PARTITION_NODE_COUNT = 40
MAX_PARTITION_VISUAL_NODE_COUNT = 4
PARTITION_HEADING_RESERVE_CHARACTERS = 1_800


def candidate_nodes(
    repository: DocumentRepository,
    revision_id: str,
    request: str,
    plan: RunPlan,
) -> tuple[StoredDocumentNode, ...]:
    selected = repository.descendant_nodes(
        revision_id,
        plan.target_node_ids,
        limit=500,
    )
    related = (
        repository.related_nodes(
            revision_id,
            (record.node_id for record in selected),
            limit=200,
        )
        if plan.scope in {ScopeMode.LOCAL_REGION, ScopeMode.SELECTED_ONLY}
        else ()
    )
    hits = search_hits(
        repository,
        revision_id,
        request,
        require_complete=plan.scope is ScopeMode.WHOLE_DOCUMENT,
    )
    records: list[StoredDocumentNode] = [*selected, *related]
    existing = {record.node_id for record in records}
    records.extend(
        repository.nodes_for_ids(
            revision_id,
            (hit.node_id for hit in hits if hit.node_id not in existing),
            limit=200,
        )
    )
    if not records and plan.scope is ScopeMode.WHOLE_DOCUMENT:
        records.extend(
            repository.coverage_nodes(
                revision_id,
                limit=200,
            )
        )
    return tuple({record.node_id: record for record in records}.values())


def search_hits(
    repository: DocumentRepository,
    revision_id: str,
    request: str,
    *,
    require_complete: bool,
) -> tuple[SearchHit, ...]:
    hits = list(
        repository.search(
            revision_id,
            request,
            limit=60,
            require_complete=require_complete,
        )
    )
    if hits:
        return tuple(hits)
    terms = tuple(
        dict.fromkeys(
            term.casefold()
            for term in re.findall(r"[\w.-]+", request, re.UNICODE)
            if len(term) >= 4
        )
    )[:12]
    by_node: dict[str, SearchHit] = {}
    for term in terms:
        for hit in repository.search(
            revision_id,
            term,
            limit=8,
            require_complete=require_complete,
        ):
            current = by_node.get(hit.node_id)
            if current is None or hit.rank < current.rank:
                by_node[hit.node_id] = hit
    return tuple(
        sorted(
            by_node.values(),
            key=lambda item: (item.rank, item.stable_path),
        )
    )


def fit_nodes(
    heading: str,
    candidates: Iterable[StoredDocumentNode],
    budget: int,
) -> tuple[str, tuple[StoredDocumentNode, ...], int]:
    materialized = tuple(candidates)
    output = heading[:budget]
    included: list[StoredDocumentNode] = []
    for record in materialized:
        node = record.node
        identity = (
            f"\n[{node.kind.value}] {node.stable_path}"
            + (f" | {node.title}" if node.title else "")
            + (f" | sheet={node.sheet_name}" if node.sheet_name else "")
            + (f" | slide={node.slide_number}" if node.slide_number is not None else "")
            + "\n"
        )
        body = node.text.strip()
        bounded_body = body
        bounded_suffix = ""
        if len(body) > MAX_NODE_EXCERPT_CHARACTERS:
            bounded_body = body[:MAX_NODE_EXCERPT_CHARACTERS]
            bounded_suffix = "\n[excerpt bounded at a Unicode character boundary]"
        block = identity + (body if body else "(structural node)") + "\n"
        if body:
            block = identity + bounded_body + bounded_suffix + "\n"
        remaining = budget - len(output)
        if remaining <= len(identity) + 64:
            break
        if len(block) > remaining:
            suffix = "\n[excerpt bounded at a Unicode character boundary]\n"
            excerpt_size = max(
                0,
                remaining - len(identity) - len(suffix),
            )
            block = identity + body[:excerpt_size] + suffix
        output += block
        included.append(record)
        if len(output) >= budget:
            break
    return (
        output,
        tuple(included),
        max(0, len(materialized) - len(included)),
    )


def coverage_partitions(
    repository: DocumentRepository,
    revision_id: str,
    nodes: Iterable[StoredDocumentNode],
    character_budget: int,
) -> tuple[tuple[str, ...], ...]:
    paths = tuple(record.node.stable_path for record in nodes)
    hydrated: list[StoredDocumentNode] = []
    for index in range(0, len(paths), 500):
        hydrated.extend(
            repository.nodes_for_paths(
                revision_id,
                paths[index : index + 500],
                include_text=True,
                limit=500,
            )
        )
    by_path = {record.node.stable_path: record for record in hydrated}
    target = max(
        512,
        character_budget - PARTITION_HEADING_RESERVE_CHARACTERS,
    )
    partitions: list[tuple[str, ...]] = []
    current: list[str] = []
    current_size = 0
    current_visuals = 0
    for path in paths:
        record = by_path.get(path)
        if record is None:
            raise DocumentIndexNotReady(
                "A structural path disappeared while partitioning."
            )
        estimate = node_projection_size(record)
        is_visual = record.node.kind in {
            NodeKind.FIGURE,
            NodeKind.CHART,
            NodeKind.PROCESS_FLOW,
        }
        if current and (
            len(current) >= MAX_PARTITION_NODE_COUNT
            or current_size + estimate > target
            or (is_visual and current_visuals >= MAX_PARTITION_VISUAL_NODE_COUNT)
        ):
            partitions.append(tuple(current))
            current = []
            current_size = 0
            current_visuals = 0
        current.append(path)
        current_size += estimate
        current_visuals += int(is_visual)
    if current:
        partitions.append(tuple(current))
    return tuple(partitions)


def node_projection_size(record: StoredDocumentNode) -> int:
    node = record.node
    body_size = min(
        len(node.text.strip()),
        MAX_NODE_EXCERPT_CHARACTERS,
    )
    return (
        len(node.stable_path)
        + len(node.title or "")
        + len(node.sheet_name or "")
        + body_size
        + 192
    )

"""Shared limits and structural categories for document persistence."""

from __future__ import annotations

from ogent_app.domain.document_intelligence import NodeKind


INDEX_VERSION = 1
MAX_SEARCH_RESULTS = 200
TEXT_CHUNK_CHARACTERS = 4_000
COVERAGE_NODE_KINDS = (
    NodeKind.SECTION,
    NodeKind.HEADING,
    NodeKind.TABLE,
    NodeKind.FIGURE,
    NodeKind.CHART,
    NodeKind.SHEET,
    NodeKind.SLIDE,
    NodeKind.PROCESS_FLOW,
)

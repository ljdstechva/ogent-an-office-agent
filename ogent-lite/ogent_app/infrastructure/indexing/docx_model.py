"""Small mutable collector and style records used by the DOCX indexer."""

from __future__ import annotations

import dataclasses
from typing import Any

from ogent_app.domain.document_intelligence import IndexedEdge, IndexedNode

from .common import DocumentIndexError


@dataclasses.dataclass(frozen=True, slots=True)
class StyleDefinition:
    name: str
    based_on: str | None = None
    outline_level: int | None = None


class DocxCollector:
    def __init__(self) -> None:
        self.nodes: list[IndexedNode] = []
        self.edges: list[IndexedEdge] = []
        self.paths: set[str] = set()
        self.counters: dict[str, int] = {}

    def add(self, node: IndexedNode) -> None:
        if node.stable_path in self.paths:
            raise DocumentIndexError(
                f"Duplicate Word index locator: {node.stable_path}"
            )
        self.paths.add(node.stable_path)
        self.nodes.append(node)
        if node.parent_path and node.parent_path not in self.paths:
            raise DocumentIndexError(
                "Word index node has a missing parent: "
                f"{node.parent_path} -> {node.stable_path}"
            )
        if node.parent_path:
            self.edges.append(
                IndexedEdge(
                    node.parent_path,
                    node.stable_path,
                    "contains",
                )
            )

    def edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if source in self.paths and target in self.paths:
            self.edges.append(IndexedEdge(source, target, edge_type, metadata or {}))
            return
        raise DocumentIndexError(
            f"Word index edge has a missing endpoint: {source} -> {target}"
        )

    def next(self, key: str) -> int:
        value = self.counters.get(key, 0) + 1
        self.counters[key] = value
        return value

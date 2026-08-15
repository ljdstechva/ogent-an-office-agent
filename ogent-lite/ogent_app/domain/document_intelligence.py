"""Document intelligence domain records and coverage invariants."""

from __future__ import annotations

import dataclasses
import enum
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


class DocumentFormat(str, enum.Enum):
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    PDF = "pdf"

    @classmethod
    def from_path(cls, path: Path) -> "DocumentFormat":
        extension = Path(path).suffix.casefold().lstrip(".")
        try:
            return cls(extension)
        except ValueError as exc:
            raise ValueError(
                f"Unsupported document intelligence format: .{extension}"
            ) from exc


class IndexStatus(str, enum.Enum):
    PENDING = "pending"
    QUICK_READY = "quick_ready"
    INDEXING = "indexing"
    COMPLETE = "complete"
    PARTIAL = "partial"
    STALE = "stale"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            IndexStatus.COMPLETE,
            IndexStatus.PARTIAL,
            IndexStatus.STALE,
            IndexStatus.FAILED,
        }


class LocatorStability(str, enum.Enum):
    NATIVE = "native"
    NAMED = "named"
    REVISION_SCOPED = "revision_scoped"
    SYNTHETIC = "synthetic"


class LocatorNamespace(str, enum.Enum):
    OFFICECLI = "officecli"
    INTERNAL = "internal"


class NodeKind(str, enum.Enum):
    DOCUMENT = "document"
    STYLE = "style"
    NUMBERING = "numbering"
    SECTION = "section"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    RUN = "run"
    TABLE = "table"
    TABLE_ROW = "table_row"
    TABLE_CELL = "table_cell"
    HEADER = "header"
    FOOTER = "footer"
    FOOTNOTE = "footnote"
    ENDNOTE = "endnote"
    COMMENT = "comment"
    REVISION = "revision"
    BOOKMARK = "bookmark"
    CROSS_REFERENCE = "cross_reference"
    FIELD = "field"
    TOC = "toc"
    FIGURE = "figure"
    IMAGE = "image"
    CHART = "chart"
    TEXT_BOX = "text_box"
    SHAPE = "shape"
    EQUATION = "equation"
    WORKBOOK = "workbook"
    SHEET = "sheet"
    RANGE = "range"
    CELL = "cell"
    FORMULA = "formula"
    NAMED_RANGE = "named_range"
    DATA_VALIDATION = "data_validation"
    CONDITIONAL_FORMAT = "conditional_format"
    PIVOT_TABLE = "pivot_table"
    SPARKLINE = "sparkline"
    SLIDE = "slide"
    MASTER = "master"
    LAYOUT = "layout"
    GROUP = "group"
    CONNECTOR = "connector"
    PROCESS_FLOW = "process_flow"
    SPEAKER_NOTE = "speaker_note"
    ANIMATION = "animation"
    TRANSITION = "transition"
    PDF_PAGE = "pdf_page"
    UNSUPPORTED = "unsupported"


@dataclasses.dataclass(frozen=True, slots=True)
class StructuralLocator:
    stable_path: str
    native_key: str | None = None
    stability: LocatorStability = LocatorStability.REVISION_SCOPED
    lineage_key: str | None = None
    source_paths: tuple[str, ...] = ()
    namespace: LocatorNamespace = LocatorNamespace.INTERNAL
    resolvable: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class IndexedNode:
    locator: StructuralLocator
    kind: NodeKind
    parent_path: str | None = None
    title: str | None = None
    text: str = ""
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)
    sheet_name: str | None = None
    slide_number: int | None = None
    page_number: int | None = None
    ordinal: int = 0
    content_sha256: str = ""

    @property
    def stable_path(self) -> str:
        return self.locator.stable_path

    @property
    def native_key(self) -> str | None:
        return self.locator.native_key

    @property
    def locator_stability(self) -> LocatorStability:
        return self.locator.stability

    @property
    def lineage_key(self) -> str | None:
        return self.locator.lineage_key

    @property
    def officecli_resolvable(self) -> bool:
        return (
            self.locator.namespace is LocatorNamespace.OFFICECLI
            and self.locator.resolvable
        )


@dataclasses.dataclass(frozen=True, slots=True)
class IndexedEdge:
    source_path: str
    target_path: str
    edge_type: str
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class StructuralManifest:
    document_format: DocumentFormat
    package_sha256: str
    counts: dict[str, int]
    stable_paths: tuple[str, ...]
    unsupported: tuple[str, ...] = ()
    quick: bool = True

    def count(self, kind: NodeKind | str) -> int:
        key = kind.value if isinstance(kind, NodeKind) else str(kind)
        return int(self.counts.get(key, 0))

    def public(self) -> dict[str, Any]:
        return {
            "format": self.document_format.value,
            "package_sha256": self.package_sha256,
            "counts": dict(self.counts),
            "stable_paths": list(self.stable_paths),
            "unsupported": list(self.unsupported),
            "quick": self.quick,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class DocumentIndex:
    document_format: DocumentFormat
    package_sha256: str
    nodes: tuple[IndexedNode, ...]
    edges: tuple[IndexedEdge, ...]
    unsupported: tuple[str, ...] = ()

    def manifest(self, *, quick: bool = False) -> StructuralManifest:
        counts = Counter(node.kind.value for node in self.nodes)
        locators = tuple(
            node.stable_path
            for node in self.nodes
            if node.kind
            in {
                NodeKind.SECTION,
                NodeKind.HEADING,
                NodeKind.TABLE,
                NodeKind.FIGURE,
                NodeKind.CHART,
                NodeKind.SHEET,
                NodeKind.SLIDE,
                NodeKind.PROCESS_FLOW,
            }
        )
        return StructuralManifest(
            self.document_format,
            self.package_sha256,
            dict(counts),
            locators,
            self.unsupported,
            quick=quick,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class IndexBatch:
    nodes: tuple[IndexedNode, ...]
    edges: tuple[IndexedEdge, ...] = ()
    progress: float = 0.0
    unsupported: tuple[str, ...] = ()
    complete: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class IndexSummary:
    document_format: DocumentFormat
    package_sha256: str
    node_count: int
    edge_count: int
    counts: dict[str, int]
    unsupported: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class IndexJob:
    revision_id: str
    attempt_id: str
    attempt_generation: int
    status: IndexStatus
    progress: float
    indexed_nodes: int
    total_estimate: int
    started_at: str | None
    updated_at: str
    error_code: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ObservedRevision:
    document: DocumentRecord
    revision: DocumentRevision
    attempt_id: str | None
    deduplicated: bool


@dataclasses.dataclass(frozen=True, slots=True)
class StoredDocumentNode:
    node_id: str
    revision_id: str
    node: IndexedNode


@dataclasses.dataclass(frozen=True, slots=True)
class DocumentRecord:
    document_id: str
    source_path: str | None
    active_path: str
    mode: str
    document_format: DocumentFormat
    canonical_path_key: str
    created_at: str
    backup_id: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class DocumentRevision:
    revision_id: str
    document_id: str
    revision_number: int
    package_sha256: str
    created_at: str
    index_status: IndexStatus
    index_version: int
    quick_manifest: dict[str, Any] = dataclasses.field(default_factory=dict)
    manifest: dict[str, Any] = dataclasses.field(default_factory=dict)
    indexed_at: str | None = None
    error_code: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class RevisionDelta:
    revision_id: str
    added_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    reused_paths: tuple[str, ...]
    removed_paths: tuple[str, ...]

    @property
    def invalidated_paths(self) -> tuple[str, ...]:
        return (*self.changed_paths, *self.removed_paths)


@dataclasses.dataclass(frozen=True, slots=True)
class SearchHit:
    node_id: str
    stable_path: str
    kind: NodeKind
    title: str | None
    text: str
    rank: float
    sheet_name: str | None = None
    slide_number: int | None = None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


COVERAGE_KIND_MAP: dict[str, frozenset[NodeKind]] = {
    "sections": frozenset({NodeKind.SECTION}),
    "headings": frozenset({NodeKind.HEADING}),
    "tables": frozenset({NodeKind.TABLE}),
    "figures": frozenset({NodeKind.FIGURE}),
    "charts": frozenset({NodeKind.CHART}),
    "sheets": frozenset({NodeKind.SHEET}),
    "slides": frozenset({NodeKind.SLIDE}),
    "process_flows": frozenset({NodeKind.PROCESS_FLOW}),
}


@dataclasses.dataclass(frozen=True, slots=True)
class CoverageLedger:
    revision_id: str
    required_paths_by_category: dict[str, tuple[str, ...]]
    reviewed_paths_by_category: dict[str, tuple[str, ...]] = dataclasses.field(
        default_factory=dict
    )
    required_visual_paths: tuple[str, ...] = ()
    unreadable_or_unsupported: tuple[str, ...] = ()
    visual_interpretation_used: tuple[str, ...] = ()

    @classmethod
    def from_nodes(
        cls,
        revision_id: str,
        nodes: Iterable[IndexedNode],
        *,
        unsupported: Iterable[str] = (),
    ) -> "CoverageLedger":
        materialized = tuple(nodes)
        required = {
            category: tuple(
                sorted(
                    {node.stable_path for node in materialized if node.kind in kinds}
                )
            )
            for category, kinds in COVERAGE_KIND_MAP.items()
        }
        return cls(
            revision_id,
            required,
            required_visual_paths=tuple(
                sorted(
                    {
                        node.stable_path
                        for node in materialized
                        if node.kind
                        in {
                            NodeKind.FIGURE,
                            NodeKind.CHART,
                            NodeKind.PROCESS_FLOW,
                        }
                    }
                )
            ),
            unreadable_or_unsupported=tuple(dict.fromkeys(unsupported)),
        )

    @property
    def totals(self) -> dict[str, int]:
        return {
            category: len(paths)
            for category, paths in self.required_paths_by_category.items()
        }

    @property
    def reviewed_counts(self) -> dict[str, int]:
        return {
            category: len(
                set(self.reviewed_paths_by_category.get(category, ())) & set(required)
            )
            for category, required in self.required_paths_by_category.items()
        }

    @property
    def reviewed_paths(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    path
                    for paths in self.reviewed_paths_by_category.values()
                    for path in paths
                }
            )
        )

    @property
    def structurally_complete(self) -> bool:
        return (
            all(
                set(required).issubset(
                    self.reviewed_paths_by_category.get(category, ())
                )
                for category, required in self.required_paths_by_category.items()
            )
            and not self.unreadable_or_unsupported
        )

    @property
    def complete(self) -> bool:
        return self.structurally_complete and set(self.required_visual_paths).issubset(
            self.visual_interpretation_used
        )

    def mark_reviewed(
        self,
        nodes: Iterable[IndexedNode],
        *,
        visual_paths: Iterable[str] = (),
    ) -> "CoverageLedger":
        materialized = tuple(nodes)
        reviewed_by_category = {
            category: set(paths)
            for category, paths in self.reviewed_paths_by_category.items()
        }
        for category, kinds in COVERAGE_KIND_MAP.items():
            category_paths = {
                node.stable_path for node in materialized if node.kind in kinds
            }
            allowed = set(self.required_paths_by_category.get(category, ()))
            reviewed_by_category.setdefault(category, set()).update(
                category_paths & allowed
            )
        visuals = tuple(
            dict.fromkeys((*self.visual_interpretation_used, *tuple(visual_paths)))
        )
        return dataclasses.replace(
            self,
            reviewed_paths_by_category={
                category: tuple(sorted(paths))
                for category, paths in reviewed_by_category.items()
            },
            visual_interpretation_used=visuals,
        )

    def public(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "complete": self.complete,
            "structurally_complete": self.structurally_complete,
            "categories": {
                category: {
                    "reviewed": int(self.reviewed_counts.get(category, 0)),
                    "total": int(total),
                }
                for category, total in self.totals.items()
            },
            "unreadable_or_unsupported": list(self.unreadable_or_unsupported),
            "visual_interpretation_used": list(self.visual_interpretation_used),
            "required_visual_paths": list(self.required_visual_paths),
        }

    def text(self) -> str:
        labels = (
            ("Sections", "sections"),
            ("Headings", "headings"),
            ("Tables", "tables"),
            ("Figures", "figures"),
            ("Charts", "charts"),
            ("Sheets", "sheets"),
            ("Slides", "slides"),
            ("Process flows", "process_flows"),
        )
        lines = ["Coverage:"]
        for label, key in labels:
            lines.append(
                f"- {label} reviewed: "
                f"{int(self.reviewed_counts.get(key, 0))}/"
                f"{int(self.totals.get(key, 0))}"
            )
        lines.extend(
            (
                "- Unreadable or unsupported objects: "
                f"{list(self.unreadable_or_unsupported)}",
                "- Visual interpretation used: "
                f"{list(self.visual_interpretation_used)}",
            )
        )
        return "\n".join(lines)


@dataclasses.dataclass(frozen=True, slots=True)
class VisualRegion:
    revision_id: str
    stable_path: str
    renderer_profile: str
    region_key: str
    media_type: str
    blob_id: str
    byte_size: int
    created_at: str

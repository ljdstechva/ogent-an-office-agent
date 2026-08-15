"""Streaming PPTX indexer for slides, visuals, and process-flow graphs."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from ogent_app.domain.document_intelligence import (
    DocumentFormat,
    DocumentIndex,
    IndexBatch,
    IndexedEdge,
    IndexedNode,
    LocatorNamespace,
    LocatorStability,
    NodeKind,
    StructuralManifest,
)

from .common import (
    OoxmlPackage,
    attribute_by_local_name,
    indexed_node,
    local_name,
    normalized_text,
    package_sha256,
    relationship_id,
)
from .pptx_schema import NS, R
from .pptx_visual_mixin import PptxVisualMixin


class PptxIndexer(PptxVisualMixin):
    format = DocumentFormat.PPTX

    def __init__(self, *, max_objects_per_slide: int = 20_000) -> None:
        self.max_objects_per_slide = max(1, int(max_objects_per_slide))

    def quick_inventory(self, path: Path) -> StructuralManifest:
        digest = package_sha256(path)
        with OoxmlPackage(path) as package:
            presentation = package.xml("ppt/presentation.xml")
            contexts = self._slide_contexts(
                presentation,
                package.relationships("ppt/presentation.xml"),
            )
            counts: Counter[str] = Counter()
            locators: list[str] = []
            for context in contexts:
                slide_path = context["path"]
                locators.append(slide_path)
                counts[NodeKind.SLIDE.value] += 1
                part = context["part"]
                if not part or not package.exists(part):
                    continue
                slide = package.xml(part)
                slide_counts, slide_locators = self._quick_slide_inventory(
                    slide, context
                )
                counts.update(slide_counts)
                locators.extend(slide_locators)
        return StructuralManifest(
            self.format,
            digest,
            dict(counts),
            tuple(locators),
            quick=True,
        )

    def index(self, path: Path) -> DocumentIndex:
        nodes: list[IndexedNode] = []
        edges: list[IndexedEdge] = []
        unsupported: list[str] = []
        for batch in self.iter_batches(path):
            nodes.extend(batch.nodes)
            edges.extend(batch.edges)
            unsupported.extend(batch.unsupported)
        return DocumentIndex(
            self.format,
            package_sha256(path),
            tuple(nodes),
            tuple(edges),
            tuple(dict.fromkeys(unsupported)),
        )

    def iter_batches(self, path: Path) -> Iterator[IndexBatch]:
        digest = package_sha256(path)
        with OoxmlPackage(path) as package:
            presentation = package.xml("ppt/presentation.xml")
            contexts = self._slide_contexts(
                presentation,
                package.relationships("ppt/presentation.xml"),
            )
            root_nodes = [
                indexed_node(
                    "/document",
                    NodeKind.DOCUMENT,
                    title=Path(path).name,
                    metadata={"format": self.format.value},
                    stability=LocatorStability.SYNTHETIC,
                    lineage_key=f"pptx:package:{digest}",
                )
            ]
            definition_nodes, definition_edges = self._definitions(package)
            root_nodes.extend(definition_nodes)
            yield IndexBatch(
                tuple(root_nodes),
                tuple(definition_edges),
                progress=0.02,
            )

            total = max(1, len(contexts))
            for index, context in enumerate(contexts, 1):
                nodes, edges, unsupported = self._index_slide(
                    package,
                    context,
                )
                yield IndexBatch(
                    tuple(nodes),
                    tuple(edges),
                    progress=(
                        1.0 if index == len(contexts) else min(0.99, index / total)
                    ),
                    unsupported=tuple(unsupported),
                    complete=index == len(contexts),
                )
            if not contexts:
                yield IndexBatch((), (), progress=1.0, complete=True)

    @staticmethod
    def _slide_contexts(
        presentation: ET.Element,
        relationships: dict[str, tuple[str, str]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        slides = presentation.findall("p:sldIdLst/p:sldId", NS)
        for index, slide in enumerate(slides, 1):
            slide_id = slide.attrib.get("id", str(index))
            rel_id = relationship_id(slide)
            part = relationships.get(rel_id or "", ("", ""))[0]
            result.append(
                {
                    "number": str(index),
                    "slide_id": slide_id,
                    "relationship_id": rel_id or "",
                    "part": part,
                    "path": f"/slide[{index}]",
                    "hidden": str(slide.attrib.get("show", "1")).casefold()
                    in {"0", "false", "off"},
                }
            )
        return result

    @classmethod
    def _quick_slide_inventory(
        cls,
        slide: ET.Element,
        context: dict[str, Any],
    ) -> tuple[Counter[str], list[str]]:
        counts: Counter[str] = Counter()
        locators: list[str] = []
        object_paths: dict[str, str] = {}
        connector_records: list[dict[str, Any]] = []
        tree = slide.find("p:cSld/p:spTree", NS)
        if tree is None:
            return counts, locators

        def visit(
            children: ET.Element,
            parent_path: str,
            *,
            group_depth: int,
        ) -> None:
            counters: Counter[str] = Counter()
            for item in children:
                name = local_name(item.tag)
                if name in {"nvGrpSpPr", "grpSpPr"}:
                    continue
                properties = next(
                    (
                        child
                        for child in item.iter()
                        if local_name(child.tag) == "cNvPr"
                    ),
                    None,
                )
                object_id = (
                    attribute_by_local_name(properties, "id")
                    if properties is not None
                    else None
                )
                indexed_path: str | None = None
                if name == "sp":
                    counters["shape"] += 1
                    counts[NodeKind.SHAPE.value] += 1
                    indexed_path = (
                        f"{parent_path}/shape[@id={object_id}]"
                        if object_id and group_depth == 0
                        else f"{parent_path}/shape[{counters['shape']}]"
                    )
                elif name == "pic":
                    counters["picture"] += 1
                    counts[NodeKind.FIGURE.value] += 1
                    indexed_path = f"{parent_path}/picture[{counters['picture']}]"
                    locators.append(indexed_path)
                elif name == "cxnSp":
                    counters["connector"] += 1
                    counts[NodeKind.CONNECTOR.value] += 1
                    indexed_path = f"{parent_path}/connector[{counters['connector']}]"
                    start, end = cls._connector_endpoints(item)
                    connector_records.append(
                        {
                            "path": indexed_path,
                            "start": start,
                            "end": end,
                        }
                    )
                elif name == "grpSp":
                    counters["group"] += 1
                    counts[NodeKind.GROUP.value] += 1
                    indexed_path = f"{parent_path}/group[{counters['group']}]"
                    visit(
                        item,
                        indexed_path,
                        group_depth=group_depth + 1,
                    )
                elif name == "graphicFrame":
                    names = {local_name(child.tag) for child in item.iter()}
                    if "tbl" in names:
                        counters["table"] += 1
                        counts[NodeKind.TABLE.value] += 1
                        indexed_path = (
                            f"{parent_path}/table[@id={object_id}]"
                            if object_id and group_depth == 0
                            else f"{parent_path}/table[{counters['table']}]"
                        )
                        locators.append(indexed_path)
                    elif "chart" in names:
                        counters["chart"] += 1
                        counts[NodeKind.CHART.value] += 1
                        indexed_path = (
                            f"{parent_path}/chart[@id={object_id}]"
                            if object_id and group_depth == 0
                            else f"{parent_path}/chart[{counters['chart']}]"
                        )
                        locators.append(indexed_path)
                    elif "relIds" in names:
                        counters["diagram"] += 1
                        counts[NodeKind.PROCESS_FLOW.value] += 1
                        indexed_path = (
                            f"/internal/pptx/slide["
                            f"{context['slide_id']}]"
                            f"/process-flow[smartart-"
                            f"{counters['diagram']}]"
                        )
                        locators.append(indexed_path)
                    else:
                        counts[NodeKind.SHAPE.value] += 1
                if object_id and indexed_path is not None:
                    object_paths[object_id] = indexed_path

        visit(tree, context["path"], group_depth=0)
        components, _ = cls._resolved_connector_components(
            object_paths,
            connector_records,
        )
        for index, _component in enumerate(components, 1):
            counts[NodeKind.PROCESS_FLOW.value] += 1
            locators.append(
                f"/internal/pptx/slide[{context['slide_id']}]"
                f"/process-flow[connectors-{index}]"
            )
        return counts, locators

    def _definitions(
        self,
        package: OoxmlPackage,
    ) -> tuple[list[IndexedNode], list[IndexedEdge]]:
        nodes: list[IndexedNode] = []
        edges: list[IndexedEdge] = []
        paths_by_part: dict[str, str] = {}
        for kind, prefix, label in (
            (NodeKind.MASTER, "ppt/slideMasters/slideMaster", "master"),
            (NodeKind.LAYOUT, "ppt/slideLayouts/slideLayout", "layout"),
        ):
            parts = sorted(
                name
                for name in package.names(prefix)
                if name.endswith(".xml") and "/_rels/" not in name
            )
            for index, part in enumerate(parts, 1):
                root = package.xml(part)
                common = root.find("p:cSld", NS)
                name = common.attrib.get("name") if common is not None else None
                path = f"/internal/pptx/{label}[{index}]"
                paths_by_part[part] = path
                nodes.append(
                    indexed_node(
                        path,
                        kind,
                        parent_path="/document",
                        title=name or f"{label.title()} {index}",
                        text=normalized_text(root),
                        metadata={"package_part": part},
                        native_key=part,
                        stability=LocatorStability.NATIVE,
                        lineage_key=f"pptx:{label}:{part}",
                        ordinal=index,
                    )
                )
                edges.append(IndexedEdge("/document", path, "contains"))
        for part, layout_path in paths_by_part.items():
            if "/slideLayouts/" not in part:
                continue
            master_part = next(
                (
                    target
                    for target, rel_type in package.relationships(part).values()
                    if rel_type.rsplit("/", 1)[-1] == "slideMaster"
                ),
                None,
            )
            master_path = paths_by_part.get(master_part or "")
            if master_path is not None:
                edges.append(
                    IndexedEdge(
                        layout_path,
                        master_path,
                        "uses_master",
                    )
                )
        return nodes, edges

    def _index_slide(
        self,
        package: OoxmlPackage,
        context: dict[str, Any],
    ) -> tuple[list[IndexedNode], list[IndexedEdge], list[str]]:
        slide_path = context["path"]
        part = context["part"]
        unsupported: list[str] = []
        if not part or not package.exists(part):
            return (
                [
                    indexed_node(
                        slide_path,
                        NodeKind.SLIDE,
                        parent_path="/document",
                        title=f"Slide {context['number']}",
                        metadata={
                            **context,
                            "missing_part": True,
                        },
                        slide_number=int(context["number"]),
                        native_key=context["slide_id"],
                        stability=LocatorStability.REVISION_SCOPED,
                        lineage_key=(f"pptx:slide:{context['slide_id']}"),
                        namespace=LocatorNamespace.OFFICECLI,
                        resolvable=True,
                    )
                ],
                [IndexedEdge("/document", slide_path, "contains")],
                [f"Missing slide part for slide {context['number']}"],
            )
        slide = package.xml(part)
        context = {
            **context,
            "hidden": context["hidden"]
            or str(slide.attrib.get("show", "1")).casefold() in {"0", "false", "off"},
        }
        relationships = package.relationships(part)
        layout_part = next(
            (
                target
                for target, rel_type in relationships.values()
                if rel_type.rsplit("/", 1)[-1] == "slideLayout"
            ),
            None,
        )
        layout_name: str | None = None
        if layout_part and package.exists(layout_part):
            layout = package.xml(layout_part)
            common = layout.find("p:cSld", NS)
            layout_name = common.attrib.get("name") if common is not None else None
        title = self._slide_title(slide) or f"Slide {context['number']}"
        nodes = [
            indexed_node(
                slide_path,
                NodeKind.SLIDE,
                parent_path="/document",
                title=title,
                text=self._slide_text(slide),
                metadata={
                    "slide_id": context["slide_id"],
                    "hidden": context["hidden"],
                    "layout": layout_name,
                    "layout_part": layout_part,
                    "package_part": part,
                },
                slide_number=int(context["number"]),
                native_key=context["slide_id"],
                stability=LocatorStability.REVISION_SCOPED,
                lineage_key=f"pptx:slide:{context['slide_id']}",
                namespace=LocatorNamespace.OFFICECLI,
                resolvable=True,
            )
        ]
        edges = [IndexedEdge("/document", slide_path, "contains")]
        if layout_part:
            layout_parts = sorted(
                name
                for name in package.names("ppt/slideLayouts/slideLayout")
                if name.endswith(".xml") and "/_rels/" not in name
            )
            if layout_part in layout_parts:
                edges.append(
                    IndexedEdge(
                        slide_path,
                        (
                            "/internal/pptx/layout["
                            f"{layout_parts.index(layout_part) + 1}]"
                        ),
                        "uses_layout",
                    )
                )
        tree = slide.find("p:cSld/p:spTree", NS)
        object_paths: dict[str, str] = {}
        connector_records: list[dict[str, Any]] = []
        counters: Counter[str] = Counter()
        object_budget = [0]
        if tree is not None:
            for item in tree:
                self._index_slide_object(
                    nodes,
                    edges,
                    package,
                    item,
                    context,
                    relationships,
                    counters,
                    object_paths,
                    connector_records,
                    unsupported,
                    parent_path=slide_path,
                    group_depth=0,
                    object_budget=object_budget,
                )
        self._connect_flow_graph(
            nodes,
            edges,
            context,
            object_paths,
            connector_records,
            unsupported,
        )
        self._index_notes(
            nodes,
            edges,
            package,
            context,
            relationships,
        )
        transition = slide.find("p:transition", NS)
        if transition is not None:
            path = f"/internal/pptx/slide[{context['slide_id']}]/transition"
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.TRANSITION,
                    parent_path=slide_path,
                    title=next(
                        (local_name(child.tag) for child in transition),
                        "transition",
                    ),
                    metadata=dict(transition.attrib),
                    slide_number=int(context["number"]),
                )
            )
            edges.append(IndexedEdge(slide_path, path, "contains"))
        timing = slide.find("p:timing", NS)
        if timing is not None:
            path = f"/internal/pptx/slide[{context['slide_id']}]/animation"
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.ANIMATION,
                    parent_path=slide_path,
                    text=normalized_text(timing),
                    metadata={
                        "timing_node_count": len(list(timing.iter())),
                    },
                    slide_number=int(context["number"]),
                )
            )
            edges.append(IndexedEdge(slide_path, path, "contains"))
        self._index_external_relationships(
            nodes,
            edges,
            package,
            context,
        )
        return nodes, edges, unsupported

    def _index_slide_object(
        self,
        nodes: list[IndexedNode],
        edges: list[IndexedEdge],
        package: OoxmlPackage,
        item: ET.Element,
        context: dict[str, Any],
        relationships: dict[str, tuple[str, str]],
        counters: Counter[str],
        object_paths: dict[str, str],
        connector_records: list[dict[str, Any]],
        unsupported: list[str],
        *,
        parent_path: str,
        group_depth: int,
        object_budget: list[int],
    ) -> None:
        name = local_name(item.tag)
        if name == "nvGrpSpPr" or name == "grpSpPr":
            return
        object_budget[0] += 1
        if object_budget[0] > self.max_objects_per_slide:
            message = (
                "Slide object inventory exceeded the configured "
                f"{self.max_objects_per_slide} object limit on "
                f"slide {context['number']}"
            )
            if message not in unsupported:
                unsupported.append(message)
            return
        properties = next(
            (child for child in item.iter() if local_name(child.tag) == "cNvPr"),
            None,
        )
        object_id = (
            attribute_by_local_name(properties, "id")
            if properties is not None
            else None
        )
        object_name = (
            attribute_by_local_name(properties, "name")
            if properties is not None
            else None
        )
        description = (
            attribute_by_local_name(properties, "descr")
            if properties is not None
            else None
        )
        slide_path = context["path"]
        slide_number = int(context["number"])
        if name == "sp":
            counters["shape"] += 1
            path = (
                f"{slide_path}/shape[@id={object_id}]"
                if object_id and group_depth == 0
                else f"{parent_path}/shape[{counters['shape']}]"
            )
            placeholder = next(
                (child for child in item.iter() if local_name(child.tag) == "ph"),
                None,
            )
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.SHAPE,
                    parent_path=parent_path,
                    title=object_name,
                    text=normalized_text(item),
                    metadata={
                        "description": description,
                        "placeholder_type": (
                            attribute_by_local_name(placeholder, "type")
                            if placeholder is not None
                            else None
                        ),
                        "geometry": self._geometry(item),
                        "bounds": self._bounds(item),
                    },
                    slide_number=slide_number,
                    native_key=object_id,
                    stability=(
                        LocatorStability.NATIVE
                        if object_id and group_depth == 0
                        else LocatorStability.REVISION_SCOPED
                    ),
                    lineage_key=(
                        f"pptx:shape:{context['slide_id']}:{object_id}"
                        if object_id
                        else None
                    ),
                    namespace=(
                        LocatorNamespace.OFFICECLI
                        if group_depth <= 1
                        else LocatorNamespace.INTERNAL
                    ),
                    resolvable=group_depth <= 1,
                    ordinal=counters["shape"],
                )
            )
            edges.append(IndexedEdge(parent_path, path, "contains"))
            if object_id:
                object_paths[object_id] = path
        elif name == "pic":
            counters["picture"] += 1
            path = f"{parent_path}/picture[{counters['picture']}]"
            blip = next(
                (child for child in item.iter() if local_name(child.tag) == "blip"),
                None,
            )
            rel_id = blip.attrib.get(f"{{{R}}}embed") if blip is not None else None
            target = relationships.get(rel_id or "", (None, ""))[0]
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.FIGURE,
                    parent_path=parent_path,
                    title=object_name,
                    text=description or "",
                    metadata={
                        "description": description,
                        "relationship_id": rel_id,
                        "package_part": target,
                        "bounds": self._bounds(item),
                    },
                    slide_number=slide_number,
                    native_key=object_id,
                    stability=LocatorStability.REVISION_SCOPED,
                    lineage_key=(
                        f"pptx:picture:{context['slide_id']}:{object_id}"
                        if object_id
                        else None
                    ),
                    namespace=(
                        LocatorNamespace.OFFICECLI
                        if group_depth == 0
                        else LocatorNamespace.INTERNAL
                    ),
                    resolvable=group_depth == 0,
                    ordinal=counters["picture"],
                )
            )
            edges.append(IndexedEdge(parent_path, path, "contains"))
            if object_id:
                object_paths[object_id] = path
        elif name == "cxnSp":
            counters["connector"] += 1
            path = f"{parent_path}/connector[{counters['connector']}]"
            start, end = self._connector_endpoints(item)
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.CONNECTOR,
                    parent_path=parent_path,
                    title=object_name,
                    text=normalized_text(item),
                    metadata={
                        "start": start,
                        "end": end,
                        "geometry": self._geometry(item),
                        "bounds": self._bounds(item),
                    },
                    slide_number=slide_number,
                    native_key=object_id,
                    stability=LocatorStability.REVISION_SCOPED,
                    lineage_key=(
                        f"pptx:connector:{context['slide_id']}:{object_id}"
                        if object_id
                        else None
                    ),
                    namespace=(
                        LocatorNamespace.OFFICECLI
                        if group_depth == 0
                        else LocatorNamespace.INTERNAL
                    ),
                    resolvable=group_depth == 0,
                    ordinal=counters["connector"],
                )
            )
            edges.append(IndexedEdge(parent_path, path, "contains"))
            connector_records.append({"path": path, "start": start, "end": end})
            if object_id:
                object_paths[object_id] = path
        elif name == "grpSp":
            counters["group"] += 1
            path = f"{parent_path}/group[{counters['group']}]"
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.GROUP,
                    parent_path=parent_path,
                    title=object_name,
                    text=normalized_text(item),
                    metadata={
                        "child_object_count": max(0, len(item) - 2),
                        "bounds": self._bounds(item),
                    },
                    slide_number=slide_number,
                    native_key=object_id,
                    stability=LocatorStability.REVISION_SCOPED,
                    lineage_key=(
                        f"pptx:group:{context['slide_id']}:{object_id}"
                        if object_id
                        else None
                    ),
                    namespace=(
                        LocatorNamespace.OFFICECLI
                        if group_depth == 0
                        else LocatorNamespace.INTERNAL
                    ),
                    resolvable=group_depth == 0,
                    ordinal=counters["group"],
                )
            )
            edges.append(IndexedEdge(parent_path, path, "contains"))
            if object_id:
                object_paths[object_id] = path
            child_counters: Counter[str] = Counter()
            for child in item:
                self._index_slide_object(
                    nodes,
                    edges,
                    package,
                    child,
                    context,
                    relationships,
                    child_counters,
                    object_paths,
                    connector_records,
                    unsupported,
                    parent_path=path,
                    group_depth=group_depth + 1,
                    object_budget=object_budget,
                )
        elif name == "graphicFrame":
            self._index_graphic_frame(
                nodes,
                edges,
                package,
                item,
                context,
                relationships,
                counters,
                object_id,
                object_name,
                object_paths,
                unsupported,
                parent_path=parent_path,
                group_depth=group_depth,
            )
        else:
            unsupported.append(
                f"Unsupported slide object {name} on slide {context['number']}"
            )

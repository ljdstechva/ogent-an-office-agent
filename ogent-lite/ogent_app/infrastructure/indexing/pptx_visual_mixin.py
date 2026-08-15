"""Graphics, SmartArt, connectors, charts, and notes for PPTX indexing."""

from __future__ import annotations

from collections import Counter
from typing import Any
from xml.etree import ElementTree as ET

from ogent_app.domain.document_intelligence import (
    IndexedEdge,
    IndexedNode,
    LocatorNamespace,
    LocatorStability,
    NodeKind,
)

from .common import (
    OoxmlPackage,
    attribute_by_local_name,
    indexed_node,
    local_name,
    normalized_text,
    relationship_id,
)
from .pptx_schema import NS, R


class PptxVisualMixin:
    def _index_graphic_frame(
        self,
        nodes: list[IndexedNode],
        edges: list[IndexedEdge],
        package: OoxmlPackage,
        frame: ET.Element,
        context: dict[str, str],
        relationships: dict[str, tuple[str, str]],
        counters: Counter[str],
        object_id: str | None,
        object_name: str | None,
        object_paths: dict[str, str],
        unsupported: list[str],
        *,
        parent_path: str,
        group_depth: int,
    ) -> None:
        names = {local_name(child.tag) for child in frame.iter()}
        slide_number = int(context["number"])
        indexed_path: str | None = None
        if "tbl" in names:
            counters["table"] += 1
            path = (
                f"{parent_path}/table[@id={object_id}]"
                if object_id and group_depth == 0
                else f"{parent_path}/table[{counters['table']}]"
            )
            table = next(
                child for child in frame.iter() if local_name(child.tag) == "tbl"
            )
            rows = [child for child in table if local_name(child.tag) == "tr"]
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.TABLE,
                    parent_path=parent_path,
                    title=object_name,
                    text=normalized_text(table),
                    metadata={
                        "row_count": len(rows),
                        "column_count": max(
                            (
                                len(
                                    [
                                        cell
                                        for cell in row
                                        if local_name(cell.tag) == "tc"
                                    ]
                                )
                                for row in rows
                            ),
                            default=0,
                        ),
                        "bounds": self._bounds(frame),
                    },
                    slide_number=slide_number,
                    native_key=object_id,
                    stability=(
                        LocatorStability.NATIVE
                        if object_id and group_depth == 0
                        else LocatorStability.REVISION_SCOPED
                    ),
                    lineage_key=(
                        f"pptx:table:{context['slide_id']}:{object_id}"
                        if object_id
                        else None
                    ),
                    namespace=(
                        LocatorNamespace.OFFICECLI
                        if group_depth == 0
                        else LocatorNamespace.INTERNAL
                    ),
                    resolvable=group_depth == 0,
                    ordinal=counters["table"],
                )
            )
            edges.append(IndexedEdge(parent_path, path, "contains"))
            indexed_path = path
        elif "chart" in names:
            counters["chart"] += 1
            path = (
                f"{parent_path}/chart[@id={object_id}]"
                if object_id and group_depth == 0
                else f"{parent_path}/chart[{counters['chart']}]"
            )
            chart = next(
                child for child in frame.iter() if local_name(child.tag) == "chart"
            )
            rel_id = relationship_id(chart)
            target = relationships.get(rel_id or "", (None, ""))[0]
            metadata: dict[str, Any] = {
                "relationship_id": rel_id,
                "package_part": target,
                "bounds": self._bounds(frame),
            }
            title: str | None = None
            if target and package.exists(target):
                title, chart_metadata = self._chart_metadata(package.xml(target))
                metadata.update(chart_metadata)
            elif target:
                unsupported.append(f"Missing chart part: {target}")
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.CHART,
                    parent_path=parent_path,
                    title=title or object_name,
                    text=title or "",
                    metadata=metadata,
                    slide_number=slide_number,
                    native_key=object_id,
                    stability=(
                        LocatorStability.NATIVE
                        if object_id and group_depth == 0
                        else LocatorStability.REVISION_SCOPED
                    ),
                    lineage_key=(
                        f"pptx:chart:{context['slide_id']}:{object_id}"
                        if object_id
                        else f"pptx:chart:{target or rel_id}"
                    ),
                    namespace=(
                        LocatorNamespace.OFFICECLI
                        if group_depth == 0
                        else LocatorNamespace.INTERNAL
                    ),
                    resolvable=group_depth == 0,
                    ordinal=counters["chart"],
                )
            )
            edges.append(IndexedEdge(parent_path, path, "contains"))
            indexed_path = path
        elif "relIds" in names:
            counters["diagram"] += 1
            frame_path = (
                f"/internal/pptx/slide[{context['slide_id']}]"
                f"/diagram-frame[{counters['diagram']}]"
            )
            self._index_smartart(
                nodes,
                edges,
                package,
                frame,
                context,
                frame_path,
                object_id,
                object_name,
                relationships,
                counters["diagram"],
                unsupported,
            )
            indexed_path = frame_path
        else:
            unsupported.append(
                "Unsupported graphic frame on "
                f"slide {context['number']}: {object_name or object_id}"
            )
        if object_id and indexed_path is not None:
            object_paths[object_id] = indexed_path

    def _index_smartart(
        self,
        nodes: list[IndexedNode],
        edges: list[IndexedEdge],
        package: OoxmlPackage,
        frame: ET.Element,
        context: dict[str, str],
        frame_path: str,
        object_id: str | None,
        object_name: str | None,
        relationships: dict[str, tuple[str, str]],
        ordinal: int,
        unsupported: list[str],
    ) -> None:
        rel_ids = next(
            (child for child in frame.iter() if local_name(child.tag) == "relIds"),
            None,
        )
        data_rel = rel_ids.attrib.get(f"{{{R}}}dm") if rel_ids is not None else None
        target = relationships.get(data_rel or "", (None, ""))[0]
        diagram_nodes: list[dict[str, Any]] = []
        diagram_edges: list[dict[str, Any]] = []
        if target and package.exists(target):
            data = package.xml(target)
            for point in data.findall(".//dgm:pt", NS):
                diagram_nodes.append(
                    {
                        "id": point.attrib.get("modelId"),
                        "type": point.attrib.get("type"),
                        "text": normalized_text(point),
                    }
                )
            for connection in data.findall(".//dgm:cxn", NS):
                diagram_edges.append(
                    {
                        "source": connection.attrib.get("srcId"),
                        "target": connection.attrib.get("destId"),
                        "type": connection.attrib.get("type"),
                    }
                )
        elif target:
            unsupported.append(f"Missing SmartArt data part: {target}")
        flow_path = (
            f"/internal/pptx/slide[{context['slide_id']}]"
            f"/process-flow[smartart-{ordinal}]"
        )
        nodes.append(
            indexed_node(
                flow_path,
                NodeKind.PROCESS_FLOW,
                parent_path=context["path"],
                title=object_name or "SmartArt",
                text=" ".join(
                    str(item.get("text", "")) for item in diagram_nodes
                ).strip(),
                metadata={
                    "source_frame_path": frame_path,
                    "package_part": target,
                    "nodes": diagram_nodes,
                    "edges": diagram_edges,
                    "smartart": True,
                },
                slide_number=int(context["number"]),
                native_key=object_id,
                stability=LocatorStability.SYNTHETIC,
                lineage_key=(f"pptx:smartart:{context['slide_id']}:{object_id}"),
                source_paths=(context["path"],),
                ordinal=ordinal,
            )
        )
        edges.append(IndexedEdge(context["path"], flow_path, "contains"))

    @classmethod
    def _connect_flow_graph(
        cls,
        nodes: list[IndexedNode],
        edges: list[IndexedEdge],
        context: dict[str, str],
        object_paths: dict[str, str],
        connector_records: list[dict[str, Any]],
        unsupported: list[str],
    ) -> None:
        if not connector_records:
            return
        components, unresolved = cls._resolved_connector_components(
            object_paths,
            connector_records,
        )
        for record in unresolved:
            start_id = record["start"].get("shape_id") or "missing"
            end_id = record["end"].get("shape_id") or "missing"
            unsupported.append(
                "Unresolved connector endpoints on slide "
                f"{context['number']}: {record['path']} "
                f"(start={start_id}, end={end_id})"
            )
        for component_index, component in enumerate(components, 1):
            flow_path = (
                f"/internal/pptx/slide[{context['slide_id']}]"
                f"/process-flow[connectors-{component_index}]"
            )
            source_paths = tuple(
                dict.fromkeys(
                    [
                        *(str(record["path"]) for record in component),
                        *(
                            path
                            for record in component
                            for endpoint in (
                                record["start"].get("shape_id"),
                                record["end"].get("shape_id"),
                            )
                            if endpoint
                            for path in [object_paths.get(endpoint)]
                            if path
                        ),
                    ]
                )
            )
            nodes.append(
                indexed_node(
                    flow_path,
                    NodeKind.PROCESS_FLOW,
                    parent_path=context["path"],
                    title=(
                        f"Process flow {component_index} on slide {context['number']}"
                    ),
                    metadata={
                        "connector_count": len(component),
                        "source_object_paths": list(source_paths),
                    },
                    slide_number=int(context["number"]),
                    stability=LocatorStability.SYNTHETIC,
                    lineage_key=(
                        f"pptx:connector-flow:{context['slide_id']}:{component_index}"
                    ),
                    source_paths=source_paths,
                    ordinal=component_index,
                )
            )
            edges.append(IndexedEdge(context["path"], flow_path, "contains"))
            for record in component:
                connector_path = record["path"]
                start_path = object_paths[record["start"]["shape_id"]]
                end_path = object_paths[record["end"]["shape_id"]]
                edges.append(
                    IndexedEdge(
                        flow_path,
                        connector_path,
                        "uses_connector",
                    )
                )
                edges.append(
                    IndexedEdge(
                        connector_path,
                        start_path,
                        "connector_start",
                        {"connection_index": record["start"].get("index")},
                    )
                )
                edges.append(
                    IndexedEdge(
                        connector_path,
                        end_path,
                        "connector_end",
                        {"connection_index": record["end"].get("index")},
                    )
                )
                edges.append(
                    IndexedEdge(
                        start_path,
                        end_path,
                        "process_flow",
                        {"connector_path": connector_path},
                    )
                )

    @staticmethod
    def _resolved_connector_components(
        object_paths: dict[str, str],
        connector_records: list[dict[str, Any]],
    ) -> tuple[
        list[list[dict[str, Any]]],
        list[dict[str, Any]],
    ]:
        resolved: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for record in connector_records:
            start_id = str(record["start"].get("shape_id") or "")
            end_id = str(record["end"].get("shape_id") or "")
            if (
                not start_id
                or not end_id
                or start_id not in object_paths
                or end_id not in object_paths
            ):
                unresolved.append(record)
            else:
                resolved.append(record)

        components: list[list[dict[str, Any]]] = []
        remaining = list(resolved)
        while remaining:
            component = [remaining.pop(0)]
            endpoint_ids = {
                str(component[0]["start"]["shape_id"]),
                str(component[0]["end"]["shape_id"]),
            }
            changed = True
            while changed:
                changed = False
                for record in tuple(remaining):
                    record_ids = {
                        str(record["start"]["shape_id"]),
                        str(record["end"]["shape_id"]),
                    }
                    if endpoint_ids.isdisjoint(record_ids):
                        continue
                    remaining.remove(record)
                    component.append(record)
                    endpoint_ids.update(record_ids)
                    changed = True
            components.append(component)
        return components, unresolved

    @staticmethod
    def _connector_endpoints(
        connector: ET.Element,
    ) -> tuple[dict[str, str], dict[str, str]]:
        properties = next(
            (
                child
                for child in connector.iter()
                if local_name(child.tag) == "cNvCxnSpPr"
            ),
            None,
        )
        result: list[dict[str, str]] = []
        for name in ("stCxn", "endCxn"):
            element = (
                next(
                    (child for child in properties if local_name(child.tag) == name),
                    None,
                )
                if properties is not None
                else None
            )
            result.append(
                {
                    "shape_id": (attribute_by_local_name(element, "id", "") or "")
                    if element is not None
                    else "",
                    "index": (attribute_by_local_name(element, "idx", "") or "")
                    if element is not None
                    else "",
                }
            )
        return result[0], result[1]

    @staticmethod
    def _geometry(element: ET.Element) -> str | None:
        geometry = next(
            (child for child in element.iter() if local_name(child.tag) == "prstGeom"),
            None,
        )
        return (
            attribute_by_local_name(geometry, "prst") if geometry is not None else None
        )

    @staticmethod
    def _bounds(element: ET.Element) -> dict[str, str]:
        transform = next(
            (child for child in element.iter() if local_name(child.tag) == "xfrm"),
            None,
        )
        if transform is None:
            return {}
        result: dict[str, str] = {}
        for child in transform:
            name = local_name(child.tag)
            if name in {"off", "ext"}:
                result[name] = ",".join(
                    f"{local_name(key)}={value}" for key, value in child.attrib.items()
                )
        return result

    @staticmethod
    def _slide_text(slide: ET.Element) -> str:
        return " ".join(
            item.text.strip()
            for item in slide.findall(".//a:t", NS)
            if item.text and item.text.strip()
        )

    @staticmethod
    def _slide_title(slide: ET.Element) -> str | None:
        for shape in slide.findall(".//p:sp", NS):
            placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
            placeholder_type = (
                placeholder.attrib.get("type") if placeholder is not None else None
            )
            if placeholder_type in {"title", "ctrTitle"}:
                text = normalized_text(shape)
                if text:
                    return text
        return None

    @staticmethod
    def _chart_metadata(root: ET.Element) -> tuple[str | None, dict[str, Any]]:
        title_element = root.find(".//c:chart/c:title", NS)
        title = normalized_text(title_element) if title_element is not None else None
        series: list[dict[str, Any]] = []
        for item in root.findall(".//c:ser", NS):
            series_title = item.find("c:tx", NS)
            category_formula = item.find("c:cat//c:f", NS)
            value_formula = item.find("c:val//c:f", NS)
            series.append(
                {
                    "title": (
                        normalized_text(series_title)
                        if series_title is not None
                        else ""
                    ),
                    "categories": [
                        value.text
                        for value in item.findall(".//c:cat//c:v", NS)
                        if value.text is not None
                    ],
                    "values": [
                        value.text
                        for value in item.findall(".//c:val//c:v", NS)
                        if value.text is not None
                    ],
                    "category_formula": (
                        normalized_text(category_formula)
                        if category_formula is not None
                        else ""
                    ),
                    "value_formula": (
                        normalized_text(value_formula)
                        if value_formula is not None
                        else ""
                    ),
                }
            )
        return title, {
            "chart_types": sorted(
                {
                    local_name(item.tag)
                    for item in root.iter()
                    if local_name(item.tag).endswith("Chart")
                }
            ),
            "series": series,
        }

    @staticmethod
    def _index_notes(
        nodes: list[IndexedNode],
        edges: list[IndexedEdge],
        package: OoxmlPackage,
        context: dict[str, str],
        relationships: dict[str, tuple[str, str]],
    ) -> None:
        notes_part = next(
            (
                target
                for target, rel_type in relationships.values()
                if rel_type.rsplit("/", 1)[-1] == "notesSlide"
            ),
            None,
        )
        if not notes_part or not package.exists(notes_part):
            return
        notes = package.xml(notes_part)
        path = f"/internal/pptx/slide[{context['slide_id']}]/speaker-notes"
        nodes.append(
            indexed_node(
                path,
                NodeKind.SPEAKER_NOTE,
                parent_path=context["path"],
                title=f"Notes for slide {context['number']}",
                text=normalized_text(notes),
                metadata={"package_part": notes_part},
                slide_number=int(context["number"]),
                native_key=notes_part,
                stability=LocatorStability.NATIVE,
                lineage_key=f"pptx:notes:{context['slide_id']}",
            )
        )
        edges.append(IndexedEdge(context["path"], path, "contains"))

    @staticmethod
    def _index_external_relationships(
        nodes: list[IndexedNode],
        edges: list[IndexedEdge],
        package: OoxmlPackage,
        context: dict[str, str],
    ) -> None:
        for index, relationship in enumerate(
            package.external_relationships(context["part"]),
            1,
        ):
            path = (
                f"/internal/pptx/slide[{context['slide_id']}]"
                f"/external-relationship[{index}]"
            )
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.CROSS_REFERENCE,
                    parent_path=context["path"],
                    title=relationship["type"].rsplit("/", 1)[-1],
                    text="",
                    metadata={
                        **relationship,
                        "external": True,
                        "fetched": False,
                    },
                    slide_number=int(context["number"]),
                    native_key=relationship["id"],
                    stability=LocatorStability.REVISION_SCOPED,
                    lineage_key=(
                        f"pptx:external:{context['slide_id']}:{relationship['id']}"
                    ),
                    ordinal=index,
                )
            )
            edges.append(IndexedEdge(context["path"], path, "references"))

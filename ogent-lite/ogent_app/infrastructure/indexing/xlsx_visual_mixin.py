"""Relationships, drawings, charts, and formulas for XLSX indexing."""

from __future__ import annotations

import re
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
from .xlsx_schema import CELL_REFERENCE, NS


class XlsxVisualMixin:
    def _index_related_parts(
        self,
        nodes: list[IndexedNode],
        edges: list[IndexedEdge],
        package: OoxmlPackage,
        root: ET.Element,
        relationships: dict[str, tuple[str, str]],
        context: dict[str, str],
        unsupported: list[str],
    ) -> None:
        sheet_path = context["path"]
        sheet_name = context["name"]
        table_index = 0
        pivot_index = 0
        comment_index = 0
        table_relationship_ids = [
            rel_id
            for item in root.findall("x:tableParts/x:tablePart", NS)
            for rel_id in [relationship_id(item)]
            if rel_id and rel_id in relationships
        ]
        ordered_relationship_ids = [
            *table_relationship_ids,
            *(
                rel_id
                for rel_id in relationships
                if rel_id not in table_relationship_ids
            ),
        ]
        for rel_id in ordered_relationship_ids:
            target, rel_type = relationships[rel_id]
            rel_name = rel_type.rsplit("/", 1)[-1].casefold()
            if rel_name == "table":
                table_index += 1
                if not package.exists(target):
                    unsupported.append(f"Missing table part: {target}")
                    continue
                table = package.xml(target)
                name = table.attrib.get(
                    "displayName",
                    table.attrib.get("name"),
                )
                path = f"{sheet_path}/table[{table_index}]"
                nodes.append(
                    indexed_node(
                        path,
                        NodeKind.TABLE,
                        parent_path=sheet_path,
                        title=name,
                        text=name or "",
                        metadata={
                            "name": name,
                            "reference": table.attrib.get("ref"),
                            "column_count": len(
                                table.findall(
                                    "x:tableColumns/x:tableColumn",
                                    NS,
                                )
                            ),
                            "package_part": target,
                            "relationship_id": rel_id,
                        },
                        sheet_name=sheet_name,
                        native_key=table.attrib.get("id") or name,
                        stability=LocatorStability.REVISION_SCOPED,
                        lineage_key=(
                            f"xlsx:table:{context['sheet_id']}:"
                            f"{table.attrib.get('id') or name or target}"
                        ),
                        ordinal=table_index,
                        namespace=LocatorNamespace.OFFICECLI,
                        resolvable=True,
                    )
                )
                edges.append(IndexedEdge(sheet_path, path, "contains"))
            elif rel_name == "pivottable":
                pivot_index += 1
                path = f"{sheet_path}/pivottable[{pivot_index}]"
                pivot = package.xml(target) if package.exists(target) else None
                nodes.append(
                    indexed_node(
                        path,
                        NodeKind.PIVOT_TABLE,
                        parent_path=sheet_path,
                        title=(pivot.attrib.get("name") if pivot is not None else None),
                        metadata={
                            "package_part": target,
                            "relationship_id": rel_id,
                        },
                        sheet_name=sheet_name,
                        native_key=(
                            pivot.attrib.get("name") if pivot is not None else rel_id
                        ),
                        stability=LocatorStability.REVISION_SCOPED,
                        lineage_key=f"xlsx:pivot:{target}",
                        ordinal=pivot_index,
                        namespace=LocatorNamespace.OFFICECLI,
                        resolvable=True,
                    )
                )
                edges.append(IndexedEdge(sheet_path, path, "contains"))
            elif rel_name == "comments":
                if not package.exists(target):
                    unsupported.append(f"Missing comments part: {target}")
                    continue
                comments = package.xml(target)
                authors = [
                    item.text or ""
                    for item in comments.findall("x:authors/x:author", NS)
                ]
                for comment in comments.findall("x:commentList/x:comment", NS):
                    comment_index += 1
                    reference = comment.attrib.get("ref", "")
                    cell_path = f"{sheet_path}/{reference}"
                    if not any(node.stable_path == cell_path for node in nodes):
                        nodes.append(
                            indexed_node(
                                cell_path,
                                NodeKind.CELL,
                                parent_path=sheet_path,
                                title=reference,
                                metadata={
                                    "reference": reference,
                                    "empty": True,
                                    "comment_anchor": True,
                                },
                                sheet_name=sheet_name,
                                native_key=(f"{context['sheet_id']}!{reference}"),
                                stability=LocatorStability.NATIVE,
                                lineage_key=(
                                    f"xlsx:cell:{context['sheet_id']}!{reference}"
                                ),
                                namespace=LocatorNamespace.OFFICECLI,
                                resolvable=True,
                            )
                        )
                        edges.append(IndexedEdge(sheet_path, cell_path, "contains"))
                    path = f"{cell_path}/comment"
                    author_id = comment.attrib.get("authorId", "")
                    try:
                        author = authors[int(author_id)]
                    except (ValueError, IndexError):
                        author = None
                    nodes.append(
                        indexed_node(
                            path,
                            NodeKind.COMMENT,
                            parent_path=cell_path,
                            title=author,
                            text=normalized_text(comment),
                            metadata={
                                "reference": reference,
                                "author": author,
                            },
                            sheet_name=sheet_name,
                            native_key=f"{context['sheet_id']}!{reference}",
                            stability=LocatorStability.NATIVE,
                            lineage_key=(
                                f"xlsx:comment:{context['sheet_id']}!{reference}"
                            ),
                            ordinal=comment_index,
                            namespace=LocatorNamespace.OFFICECLI,
                            resolvable=True,
                        )
                    )
                    edges.append(
                        IndexedEdge(
                            cell_path,
                            path,
                            "contains",
                        )
                    )
        drawing = root.find("x:drawing", NS)
        drawing_id = relationship_id(drawing) if drawing is not None else None
        drawing_part = relationships.get(drawing_id or "", ("", ""))[0]
        if drawing_part:
            self._index_drawing(
                nodes,
                edges,
                package,
                drawing_part,
                context,
                unsupported,
            )

    def _index_drawing(
        self,
        nodes: list[IndexedNode],
        edges: list[IndexedEdge],
        package: OoxmlPackage,
        drawing_part: str,
        context: dict[str, str],
        unsupported: list[str],
    ) -> None:
        if not package.exists(drawing_part):
            unsupported.append(f"Missing drawing part: {drawing_part}")
            return
        root = package.xml(drawing_part)
        relationships = package.relationships(drawing_part)
        counters: Counter[str] = Counter()
        for anchor in root:
            anchor_range = self._drawing_anchor(anchor)
            anchor_properties = next(
                (child for child in anchor.iter() if local_name(child.tag) == "cNvPr"),
                None,
            )
            anchor_id = (
                attribute_by_local_name(anchor_properties, "id")
                if anchor_properties is not None
                else None
            )
            for item in anchor.iter():
                name = local_name(item.tag)
                if name == "chart":
                    counters["chart"] += 1
                    rel_id = relationship_id(item)
                    target = relationships.get(rel_id or "", ("", ""))[0]
                    metadata = {
                        "relationship_id": rel_id,
                        "package_part": target,
                        "anchor": anchor_range,
                        "drawing_object_id": anchor_id,
                    }
                    title: str | None = None
                    if target and package.exists(target):
                        title, chart_metadata = self._chart_metadata(
                            package.xml(target)
                        )
                        metadata.update(chart_metadata)
                    elif target:
                        unsupported.append(f"Missing chart part: {target}")
                    path = f"{context['path']}/chart[{counters['chart']}]"
                    nodes.append(
                        indexed_node(
                            path,
                            NodeKind.CHART,
                            parent_path=context["path"],
                            title=title,
                            text=title or "",
                            metadata=metadata,
                            sheet_name=context["name"],
                            native_key=anchor_id,
                            stability=LocatorStability.REVISION_SCOPED,
                            lineage_key=(
                                f"xlsx:chart:{context['sheet_id']}:"
                                f"{anchor_id or target or rel_id}"
                            ),
                            ordinal=counters["chart"],
                            namespace=LocatorNamespace.OFFICECLI,
                            resolvable=True,
                        )
                    )
                    edges.append(IndexedEdge(context["path"], path, "contains"))
                elif name == "pic":
                    counters["picture"] += 1
                    properties = next(
                        (
                            child
                            for child in item.iter()
                            if local_name(child.tag) == "cNvPr"
                        ),
                        None,
                    )
                    identifier = (
                        attribute_by_local_name(properties, "id")
                        if properties is not None
                        else None
                    )
                    path = f"{context['path']}/picture[{counters['picture']}]"
                    nodes.append(
                        indexed_node(
                            path,
                            NodeKind.FIGURE,
                            parent_path=context["path"],
                            title=(
                                attribute_by_local_name(properties, "name")
                                if properties is not None
                                else None
                            ),
                            text=(
                                attribute_by_local_name(properties, "descr", "")
                                if properties is not None
                                else ""
                            )
                            or "",
                            metadata={
                                "anchor": anchor_range,
                                "description": (
                                    attribute_by_local_name(
                                        properties,
                                        "descr",
                                    )
                                    if properties is not None
                                    else None
                                ),
                            },
                            sheet_name=context["name"],
                            native_key=identifier,
                            stability=LocatorStability.REVISION_SCOPED,
                            lineage_key=(
                                f"xlsx:picture:{context['sheet_id']}:{identifier}"
                            ),
                            ordinal=counters["picture"],
                            namespace=LocatorNamespace.OFFICECLI,
                            resolvable=True,
                        )
                    )
                    edges.append(IndexedEdge(context["path"], path, "contains"))
                elif name == "sp":
                    counters["shape"] += 1
                    properties = next(
                        (
                            child
                            for child in item.iter()
                            if local_name(child.tag) == "cNvPr"
                        ),
                        None,
                    )
                    identifier = (
                        attribute_by_local_name(properties, "id")
                        if properties is not None
                        else None
                    )
                    path = f"{context['path']}/shape[{counters['shape']}]"
                    nodes.append(
                        indexed_node(
                            path,
                            NodeKind.SHAPE,
                            parent_path=context["path"],
                            title=(
                                attribute_by_local_name(properties, "name")
                                if properties is not None
                                else None
                            ),
                            text=normalized_text(item),
                            metadata={"anchor": anchor_range},
                            sheet_name=context["name"],
                            native_key=identifier,
                            stability=LocatorStability.REVISION_SCOPED,
                            lineage_key=(
                                f"xlsx:shape:{context['sheet_id']}:{identifier}"
                            ),
                            ordinal=counters["shape"],
                            namespace=LocatorNamespace.OFFICECLI,
                            resolvable=True,
                        )
                    )
                    edges.append(IndexedEdge(context["path"], path, "contains"))

    @staticmethod
    def _drawing_anchor(anchor: ET.Element) -> dict[str, Any]:
        result: dict[str, Any] = {"type": local_name(anchor.tag)}
        for name in ("from", "to"):
            element = next(
                (child for child in anchor if local_name(child.tag) == name),
                None,
            )
            if element is not None:
                result[name] = {local_name(child.tag): child.text for child in element}
        return result

    @staticmethod
    def _chart_metadata(root: ET.Element) -> tuple[str | None, dict[str, Any]]:
        title_element = root.find(".//c:chart/c:title", NS)
        title = normalized_text(title_element) if title_element is not None else None
        series: list[dict[str, Any]] = []
        for item in root.findall(".//c:ser", NS):
            title_node = item.find("c:tx", NS)
            category_formula = item.find("c:cat//c:f", NS)
            value_formula = item.find("c:val//c:f", NS)
            series.append(
                {
                    "title": (
                        normalized_text(title_node) if title_node is not None else ""
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
    def _formula_dependencies(
        formulas: list[tuple[str, str, str, bool]],
        sheets: list[dict[str, str]],
        known_paths: set[str],
        named_ranges: list[tuple[str, str | None, str]],
    ) -> tuple[list[IndexedNode], list[IndexedEdge], list[str]]:
        sheet_paths = {sheet["name"]: sheet["path"] for sheet in sheets}
        sheet_ids = {sheet["name"]: sheet["sheet_id"] for sheet in sheets}
        nodes: list[IndexedNode] = []
        edges: list[IndexedEdge] = []
        unsupported: list[str] = []
        seen: set[tuple[str, str]] = set()
        created_paths: set[str] = set()
        for source, formula, current_sheet, shared_template in formulas:
            resolved_any = False
            if shared_template:
                unsupported.append(
                    "Shared-formula follower dependencies use the master "
                    f"template at {source}"
                )
                continue
            if "[" in formula or "]" in formula:
                unsupported.append(
                    f"Structured formula reference requires expansion at {source}"
                )
            names = {
                name.casefold(): name for name, _local_sheet, _path in named_ranges
            }
            for canonical_name in names.values():
                candidates = [
                    (local_sheet, named_path)
                    for name, local_sheet, named_path in named_ranges
                    if name.casefold() == canonical_name.casefold()
                ]
                qualified_matches = [
                    named_path
                    for local_sheet, named_path in candidates
                    if local_sheet
                    and re.search(
                        (
                            rf"(?:'{re.escape(local_sheet)}'|"
                            rf"{re.escape(local_sheet)})!"
                            rf"{re.escape(canonical_name)}"
                            r"(?![A-Za-z0-9_.])"
                        ),
                        formula,
                        re.IGNORECASE,
                    )
                ]
                selected_paths = qualified_matches
                if not selected_paths and re.search(
                    rf"(?<![A-Za-z0-9_.!]){re.escape(canonical_name)}"
                    r"(?![A-Za-z0-9_.])",
                    formula,
                    re.IGNORECASE,
                ):
                    local = [
                        path
                        for local_sheet, path in candidates
                        if local_sheet
                        and local_sheet.casefold() == current_sheet.casefold()
                    ]
                    global_paths = [
                        path for local_sheet, path in candidates if local_sheet is None
                    ]
                    selected_paths = local or global_paths[:1]
                for named_path in dict.fromkeys(selected_paths):
                    key = (source, named_path)
                    if key in seen:
                        continue
                    seen.add(key)
                    resolved_any = True
                    edges.append(
                        IndexedEdge(
                            source,
                            named_path,
                            "formula_depends_on_named_range",
                            {"formula": formula},
                        )
                    )
            for match in CELL_REFERENCE.finditer(formula):
                sheet_name = match.group(1) or match.group(2) or current_sheet
                canonical_name = next(
                    (
                        name
                        for name in sheet_paths
                        if name.casefold() == sheet_name.casefold()
                    ),
                    sheet_name,
                )
                sheet_path = sheet_paths.get(canonical_name)
                if not sheet_path:
                    continue
                first = match.group(3).replace("$", "").upper()
                last = (
                    match.group(4).replace("$", "").upper() if match.group(4) else None
                )
                reference = f"{first}:{last}" if last else first
                target = f"{sheet_path}/{reference}"
                if target not in known_paths and target not in created_paths:
                    nodes.append(
                        indexed_node(
                            target,
                            NodeKind.RANGE,
                            parent_path=sheet_path,
                            title=reference,
                            metadata={
                                "reference": reference,
                                "formula_dependency_target": True,
                            },
                            sheet_name=canonical_name,
                            native_key=(
                                f"{sheet_ids.get(canonical_name, canonical_name)}"
                                f"!{reference}"
                            ),
                            stability=LocatorStability.NATIVE,
                            lineage_key=(
                                "xlsx:range:"
                                f"{sheet_ids.get(canonical_name, canonical_name)}"
                                f"!{reference}"
                            ),
                            namespace=LocatorNamespace.OFFICECLI,
                            resolvable=True,
                        )
                    )
                    created_paths.add(target)
                key = (source, target)
                if key not in seen:
                    seen.add(key)
                    resolved_any = True
                    edges.append(
                        IndexedEdge(
                            source,
                            target,
                            "formula_depends_on",
                            {"formula": formula},
                        )
                    )
            if not resolved_any and re.search(r"[A-Z]{1,3}[0-9]", formula, re.I):
                unsupported.append(
                    f"Formula references could not be resolved at {source}"
                )
        return nodes, edges, list(dict.fromkeys(unsupported))

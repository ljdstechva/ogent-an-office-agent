"""Paragraph content, visuals, tables, and story-part indexing for DOCX."""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from ogent_app.domain.document_intelligence import (
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
from .docx_model import DocxCollector as _Collector
from .docx_model import StyleDefinition as _StyleDefinition
from .docx_schema import NS, R, W


class DocxContentMixin:
    @staticmethod
    def _word_text(element: ET.Element) -> str:
        raise NotImplementedError

    @staticmethod
    def _paragraph_locator(paragraph: ET.Element, ordinal: int) -> str:
        raise NotImplementedError

    def _paragraph_node(
        self,
        paragraph: ET.Element,
        path: str,
        parent_path: str,
        styles: dict[str, _StyleDefinition],
        ordinal: int,
    ) -> tuple[IndexedNode, int | None]:
        raise NotImplementedError

    def _index_paragraph_children(
        self,
        collector: _Collector,
        package: OoxmlPackage,
        paragraph: ET.Element,
        parent_path: str,
        relationships: dict[str, tuple[str, str]],
        bookmarks: dict[str, str],
        pending_cross_references: list[tuple[str, str]],
        unsupported: list[str],
        external_relationships: dict[str, dict[str, str]] | None = None,
    ) -> None:
        visual_index = 0
        for index, run in enumerate(paragraph.findall(".//w:r", NS), 1):
            text = self._word_text(run)
            run_path = f"{parent_path}/r[{index}]"
            run_visuals = [
                item
                for item in run.iter()
                if local_name(item.tag) in {"drawing", "pict", "object"}
            ]
            collector.add(
                indexed_node(
                    run_path,
                    NodeKind.RUN,
                    parent_path=parent_path,
                    text=text,
                    metadata={"visual_count": len(run_visuals)},
                    ordinal=index,
                    namespace=LocatorNamespace.OFFICECLI,
                    resolvable=True,
                )
            )
            for visual in run_visuals:
                visual_index += 1
                self._index_visual(
                    collector,
                    package,
                    visual,
                    run_path,
                    parent_path,
                    relationships,
                    visual_index,
                    unsupported,
                )
        for index, bookmark in enumerate(
            paragraph.findall(".//w:bookmarkStart", NS),
            1,
        ):
            name = bookmark.attrib.get(f"{{{W}}}name", "")
            path = f"{parent_path}/bookmark[{index}]"
            collector.add(
                indexed_node(
                    path,
                    NodeKind.BOOKMARK,
                    parent_path=parent_path,
                    title=name or None,
                    metadata={
                        "name": name,
                        "bookmark_id": bookmark.attrib.get(f"{{{W}}}id"),
                    },
                    ordinal=index,
                )
            )
            if name:
                bookmarks[name] = path
        fields = [
            item
            for item in paragraph.iter()
            if local_name(item.tag) in {"fldSimple", "instrText"}
        ]
        for index, field in enumerate(fields, 1):
            instruction = (
                field.attrib.get(f"{{{W}}}instr") or (field.text or "")
            ).strip()
            upper = instruction.upper()
            kind = (
                NodeKind.TOC
                if upper.startswith("TOC")
                else NodeKind.CROSS_REFERENCE
                if upper.startswith(("REF ", "PAGEREF "))
                else NodeKind.FIELD
            )
            path = f"{parent_path}/field[{index}]"
            collector.add(
                indexed_node(
                    path,
                    kind,
                    parent_path=parent_path,
                    title=instruction or None,
                    text=instruction,
                    metadata={"instruction": instruction},
                    ordinal=index,
                )
            )
            match = re.match(r"(?:REF|PAGEREF)\s+([^\s\\]+)", instruction, re.I)
            if match:
                pending_cross_references.append((path, match.group(1)))
        external_relationships = external_relationships or {}
        for index, hyperlink in enumerate(
            paragraph.findall(".//w:hyperlink", NS),
            1,
        ):
            rel_id = relationship_id(hyperlink)
            anchor = hyperlink.attrib.get(f"{{{W}}}anchor")
            internal_target = relationships.get(rel_id or "", (None, ""))[0]
            external = external_relationships.get(rel_id or "")
            path = f"{parent_path}/hyperlink[{index}]"
            target = external.get("target") if external is not None else internal_target
            collector.add(
                indexed_node(
                    path,
                    NodeKind.CROSS_REFERENCE,
                    parent_path=parent_path,
                    title=self._word_text(hyperlink) or None,
                    text=self._word_text(hyperlink),
                    metadata={
                        "relationship_id": rel_id,
                        "anchor": anchor,
                        "target": target,
                        "external": external is not None,
                        "fetched": False,
                    },
                    ordinal=index,
                    native_key=rel_id or anchor,
                    stability=LocatorStability.REVISION_SCOPED,
                    lineage_key=f"docx:hyperlink:{rel_id or anchor}",
                )
            )
            if anchor:
                pending_cross_references.append((path, anchor))
        for index, revision in enumerate(
            [
                item
                for item in paragraph.iter()
                if local_name(item.tag) in {"ins", "del", "moveFrom", "moveTo"}
            ],
            1,
        ):
            collector.add(
                indexed_node(
                    f"{parent_path}/revision[{index}]",
                    NodeKind.REVISION,
                    parent_path=parent_path,
                    text=self._word_text(revision),
                    metadata={
                        "revision_type": local_name(revision.tag),
                        "author": attribute_by_local_name(revision, "author"),
                        "date": attribute_by_local_name(revision, "date"),
                    },
                    ordinal=index,
                )
            )
        for index, textbox in enumerate(
            [
                item
                for item in paragraph.iter()
                if local_name(item.tag) == "txbxContent"
            ],
            1,
        ):
            collector.add(
                indexed_node(
                    f"{parent_path}/textbox[{index}]",
                    NodeKind.TEXT_BOX,
                    parent_path=parent_path,
                    text=self._word_text(textbox),
                    ordinal=index,
                )
            )
        for index, shape in enumerate(
            [
                item
                for item in paragraph.iter()
                if local_name(item.tag) in {"shape", "wsp"}
            ],
            1,
        ):
            collector.add(
                indexed_node(
                    f"{parent_path}/shape[{index}]",
                    NodeKind.SHAPE,
                    parent_path=parent_path,
                    title=attribute_by_local_name(shape, "name"),
                    text=self._word_text(shape),
                    metadata={
                        "shape_id": attribute_by_local_name(shape, "id"),
                        "style": attribute_by_local_name(shape, "style"),
                    },
                    ordinal=index,
                )
            )
        equations = [
            item
            for item in paragraph.iter()
            if local_name(item.tag) in {"oMath", "oMathPara"}
        ]
        for index, equation in enumerate(equations, 1):
            collector.add(
                indexed_node(
                    f"{parent_path}/equation[{index}]",
                    NodeKind.EQUATION,
                    parent_path=parent_path,
                    text=normalized_text(equation),
                    ordinal=index,
                )
            )

    def _index_visual(
        self,
        collector: _Collector,
        package: OoxmlPackage,
        visual: ET.Element,
        source_run_path: str,
        paragraph_path: str,
        relationships: dict[str, tuple[str, str]],
        ordinal: int,
        unsupported: list[str],
    ) -> None:
        properties = next(
            (
                item
                for item in visual.iter()
                if local_name(item.tag) in {"docPr", "cNvPr"}
            ),
            None,
        )
        metadata = {
            "name": attribute_by_local_name(properties, "name")
            if properties is not None
            else None,
            "description": attribute_by_local_name(properties, "descr")
            if properties is not None
            else None,
            "title": attribute_by_local_name(properties, "title")
            if properties is not None
            else None,
        }
        drawing_id = (
            attribute_by_local_name(properties, "id")
            if properties is not None
            else None
        )
        charts = [item for item in visual.iter() if local_name(item.tag) == "chart"]
        for chart in charts:
            chart_index = collector.next(f"chart:{paragraph_path}")
            rel_id = relationship_id(chart)
            target = relationships.get(rel_id or "", (None, ""))[0]
            chart_metadata: dict[str, Any] = {
                "relationship_id": rel_id,
                "package_part": target,
                "drawing_id": drawing_id,
                "source_run_path": source_run_path,
            }
            title: str | None = None
            if target and package.exists(target):
                title, parsed = self._chart_metadata(package.xml(target))
                chart_metadata.update(parsed)
            elif target:
                unsupported.append(f"Missing chart part: {target}")
            collector.add(
                indexed_node(
                    f"{paragraph_path}/chart[{chart_index}]",
                    NodeKind.CHART,
                    parent_path=paragraph_path,
                    title=title,
                    text=title or "",
                    metadata=chart_metadata,
                    ordinal=chart_index,
                    native_key=rel_id,
                    stability=(
                        LocatorStability.NATIVE
                        if rel_id
                        else LocatorStability.REVISION_SCOPED
                    ),
                    lineage_key=(
                        f"docx:chart:{target or rel_id}" if target or rel_id else None
                    ),
                    source_paths=(source_run_path,),
                    namespace=LocatorNamespace.OFFICECLI,
                    resolvable=True,
                )
            )
            collector.edge(
                source_run_path,
                f"{paragraph_path}/chart[{chart_index}]",
                "hosts",
            )
        if charts:
            return

        figure_index = collector.next("figure")
        figure_path = f"/internal/word/figure[{figure_index}]"
        collector.add(
            indexed_node(
                figure_path,
                NodeKind.FIGURE,
                parent_path=paragraph_path,
                title=metadata["title"] or metadata["name"],
                text=metadata["description"] or "",
                metadata={
                    **metadata,
                    "source_run_path": source_run_path,
                },
                ordinal=ordinal,
                native_key=drawing_id,
                stability=(
                    LocatorStability.NATIVE
                    if drawing_id
                    else LocatorStability.REVISION_SCOPED
                ),
                lineage_key=(f"docx:drawing:{drawing_id}" if drawing_id else None),
                source_paths=(source_run_path,),
            )
        )
        collector.edge(source_run_path, figure_path, "hosts")
        for image_index, blip in enumerate(
            [item for item in visual.iter() if local_name(item.tag) == "blip"],
            1,
        ):
            embed = blip.attrib.get(f"{{{R}}}embed")
            target = relationships.get(embed or "", (None, ""))[0]
            collector.add(
                indexed_node(
                    f"{figure_path}/image[{image_index}]",
                    NodeKind.IMAGE,
                    parent_path=figure_path,
                    title=metadata["title"] or metadata["name"],
                    text=metadata["description"] or "",
                    metadata={
                        **metadata,
                        "relationship_id": embed,
                        "package_part": target,
                    },
                    ordinal=image_index,
                    native_key=embed,
                    stability=LocatorStability.REVISION_SCOPED,
                    lineage_key=f"docx:image:{target or embed}",
                    source_paths=(source_run_path,),
                )
            )

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
                        point.text
                        for point in item.findall(".//c:cat//c:v", NS)
                        if point.text is not None
                    ],
                    "values": [
                        point.text
                        for point in item.findall(".//c:val//c:v", NS)
                        if point.text is not None
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
        axes = [
            normalized_text(title_element)
            for axis in root.findall(".//c:catAx", NS) + root.findall(".//c:valAx", NS)
            for title_element in axis.findall("c:title", NS)
        ]
        return title, {
            "chart_types": sorted(
                {
                    local_name(item.tag)
                    for item in root.iter()
                    if local_name(item.tag).endswith("Chart")
                }
            ),
            "series": series,
            "axes": axes,
        }

    def _index_table(
        self,
        collector: _Collector,
        package: OoxmlPackage,
        table: ET.Element,
        path: str,
        parent_path: str,
        styles: dict[str, _StyleDefinition],
        relationships: dict[str, tuple[str, str]],
        external_relationships: dict[str, dict[str, str]],
        unsupported: list[str],
    ) -> None:
        rows = table.findall("w:tr", NS)
        caption = table.find("w:tblPr/w:tblCaption", NS)
        table_style = table.find("w:tblPr/w:tblStyle", NS)
        table_name = caption.attrib.get(f"{{{W}}}val") if caption is not None else None
        collector.add(
            indexed_node(
                path,
                NodeKind.TABLE,
                parent_path=parent_path,
                title=table_name,
                text=self._word_text(table),
                metadata={
                    "row_count": len(rows),
                    "column_count": max(
                        (len(row.findall("w:tc", NS)) for row in rows),
                        default=0,
                    ),
                    "style": (
                        table_style.attrib.get(f"{{{W}}}val")
                        if table_style is not None
                        else None
                    ),
                },
                native_key=None,
                stability=LocatorStability.REVISION_SCOPED,
                lineage_key=None,
                namespace=LocatorNamespace.OFFICECLI,
                resolvable=True,
            )
        )
        for row_index, row in enumerate(rows, 1):
            row_path = f"{path}/tr[{row_index}]"
            cells = row.findall("w:tc", NS)
            collector.add(
                indexed_node(
                    row_path,
                    NodeKind.TABLE_ROW,
                    parent_path=path,
                    text=self._word_text(row),
                    metadata={"cell_count": len(cells)},
                    ordinal=row_index,
                )
            )
            for cell_index, cell in enumerate(cells, 1):
                cell_path = f"{row_path}/tc[{cell_index}]"
                grid_span = cell.find("w:tcPr/w:gridSpan", NS)
                vertical_merge = cell.find("w:tcPr/w:vMerge", NS)
                collector.add(
                    indexed_node(
                        cell_path,
                        NodeKind.TABLE_CELL,
                        parent_path=row_path,
                        text=self._word_text(cell),
                        metadata={
                            "grid_span": (
                                grid_span.attrib.get(f"{{{W}}}val")
                                if grid_span is not None
                                else None
                            ),
                            "vertical_merge": (
                                vertical_merge.attrib.get(
                                    f"{{{W}}}val",
                                    "continue",
                                )
                                if vertical_merge is not None
                                else None
                            ),
                        },
                        ordinal=cell_index,
                    )
                )
                for paragraph_index, paragraph in enumerate(
                    cell.findall("w:p", NS),
                    1,
                ):
                    paragraph_path = f"{cell_path}/p[{paragraph_index}]"
                    node, _level = self._paragraph_node(
                        paragraph,
                        paragraph_path,
                        cell_path,
                        styles,
                        paragraph_index,
                    )
                    collector.add(node)
                    self._index_paragraph_children(
                        collector,
                        package,
                        paragraph,
                        paragraph_path,
                        relationships,
                        {},
                        [],
                        unsupported,
                        external_relationships,
                    )

    @staticmethod
    def _add_section(
        collector: _Collector,
        section: ET.Element,
        index: int,
        parent_path: str,
    ) -> None:
        page_size = section.find("w:pgSz", NS)
        margins = section.find("w:pgMar", NS)
        columns = section.find("w:cols", NS)
        collector.add(
            indexed_node(
                f"/body/sectPr[{index}]",
                NodeKind.SECTION,
                parent_path=parent_path,
                title=f"Section {index}",
                metadata={
                    "page_size": dict(page_size.attrib)
                    if page_size is not None
                    else {},
                    "margins": dict(margins.attrib) if margins is not None else {},
                    "columns": (dict(columns.attrib) if columns is not None else {}),
                },
                ordinal=index,
            )
        )

    def _index_story_parts(
        self,
        collector: _Collector,
        package: OoxmlPackage,
        prefix: str,
        kind: NodeKind,
        styles: dict[str, _StyleDefinition],
        unsupported: list[str],
    ) -> None:
        parts = sorted(
            name
            for name in package.names(prefix)
            if name.endswith(".xml") and "/_rels/" not in name
        )
        for part_index, part in enumerate(parts, 1):
            root = package.xml(part)
            path = f"/{kind.value}[{part_index}]"
            collector.add(
                indexed_node(
                    path,
                    kind,
                    parent_path="/document",
                    title=f"{kind.value.replace('_', ' ').title()} {part_index}",
                    text=self._word_text(root),
                    metadata={"package_part": part},
                    ordinal=part_index,
                )
            )
            relationships = package.relationships(part)
            external_relationships = {
                item["id"]: item for item in package.external_relationships(part)
            }
            for paragraph_index, paragraph in enumerate(
                root.findall(".//w:p", NS),
                1,
            ):
                paragraph_path = f"{path}/p[{paragraph_index}]"
                node, _level = self._paragraph_node(
                    paragraph,
                    paragraph_path,
                    path,
                    styles,
                    paragraph_index,
                )
                collector.add(node)
                self._index_paragraph_children(
                    collector,
                    package,
                    paragraph,
                    paragraph_path,
                    relationships,
                    {},
                    [],
                    unsupported,
                    external_relationships,
                )

    def _index_notes_comments(
        self,
        collector: _Collector,
        package: OoxmlPackage,
        part: str,
        element_name: str,
        kind: NodeKind,
    ) -> None:
        if not package.exists(part):
            return
        root = package.xml(part)
        items = [item for item in root if local_name(item.tag) == element_name]
        for index, item in enumerate(items, 1):
            identifier = attribute_by_local_name(item, "id", str(index))
            if element_name == "footnote":
                path = f"/footnote[@footnoteId={identifier}]"
                resolvable = True
            elif element_name == "comment":
                path = f"/comments/comment[@commentId={identifier}]"
                resolvable = True
            else:
                path = f"/internal/word/{element_name}[{identifier}]"
                resolvable = False
            collector.add(
                indexed_node(
                    path,
                    kind,
                    parent_path="/document",
                    title=attribute_by_local_name(item, "author"),
                    text=self._word_text(item),
                    metadata={
                        "id": identifier,
                        "author": attribute_by_local_name(item, "author"),
                        "date": attribute_by_local_name(item, "date"),
                    },
                    ordinal=index,
                    native_key=identifier,
                    stability=LocatorStability.NATIVE,
                    lineage_key=f"docx:{element_name}:{identifier}",
                    namespace=(
                        LocatorNamespace.OFFICECLI
                        if resolvable
                        else LocatorNamespace.INTERNAL
                    ),
                    resolvable=resolvable,
                )
            )

    def _index_comment_anchors(
        self,
        collector: _Collector,
        document_root: ET.Element,
    ) -> None:
        body = document_root.find("w:body", NS)
        if body is None:
            return
        paragraph_index = 0
        for paragraph in body:
            if paragraph.tag != f"{{{W}}}p":
                continue
            paragraph_index += 1
            paragraph_path = self._paragraph_locator(
                paragraph,
                paragraph_index,
            )
            for anchor in paragraph.findall(".//w:commentRangeStart", NS):
                identifier = anchor.attrib.get(f"{{{W}}}id")
                comment_path = f"/comments/comment[@commentId={identifier}]"
                if comment_path in collector.paths:
                    collector.edge(
                        paragraph_path,
                        comment_path,
                        "comment_anchor",
                    )

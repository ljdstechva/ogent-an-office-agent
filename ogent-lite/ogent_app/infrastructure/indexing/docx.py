"""Structural DOCX indexer with OfficeCLI-compatible stable locators."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ogent_app.domain.document_intelligence import (
    DocumentFormat,
    DocumentIndex,
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
    package_sha256,
)
from .docx_model import DocxCollector as _Collector
from .docx_model import StyleDefinition as _StyleDefinition
from .docx_schema import NS, W, W14
from .docx_content_mixin import DocxContentMixin


class DocxIndexer(DocxContentMixin):
    format = DocumentFormat.DOCX

    def quick_inventory(self, path: Path) -> StructuralManifest:
        digest = package_sha256(path)
        with OoxmlPackage(path) as package:
            root = package.xml("word/document.xml")
            styles = self._styles(package)
            body = root.find("w:body", NS)
            if body is None:
                counts: dict[str, int] = {}
                locators: tuple[str, ...] = ()
            else:
                quick_counts, quick_locators = self._quick_body_inventory(
                    package,
                    body,
                    styles,
                )
                counts = dict(quick_counts)
                locators = tuple(quick_locators)
        return StructuralManifest(
            self.format,
            digest,
            counts,
            locators,
            quick=True,
        )

    def _quick_body_inventory(  # noqa: C901
        self,
        package: OoxmlPackage,
        body: ET.Element,
        styles: dict[str, _StyleDefinition],
    ) -> tuple[dict[str, int], list[str]]:
        """Traverse heterogeneous Word body nodes; see ARCHITECTURE.md."""
        counts: dict[str, int] = {}
        locators: list[str] = []
        figure_index = 0

        def increment(kind: NodeKind, amount: int = 1) -> None:
            counts[kind.value] = counts.get(kind.value, 0) + amount

        def scan_paragraph(
            paragraph: ET.Element,
            paragraph_path: str,
        ) -> None:
            nonlocal figure_index
            paragraph_kind = self._paragraph_kind(paragraph, styles)[0]
            increment(paragraph_kind)
            if paragraph_kind is NodeKind.HEADING:
                locators.append(paragraph_path)
            chart_index = 0
            for run in paragraph.findall(".//w:r", NS):
                visuals = [
                    item
                    for item in run.iter()
                    if local_name(item.tag) in {"drawing", "pict", "object"}
                ]
                for visual in visuals:
                    charts = [
                        item
                        for item in visual.iter()
                        if local_name(item.tag) == "chart"
                    ]
                    if charts:
                        for _chart in charts:
                            chart_index += 1
                            increment(NodeKind.CHART)
                            locators.append(f"{paragraph_path}/chart[{chart_index}]")
                    else:
                        figure_index += 1
                        increment(NodeKind.FIGURE)
                        locators.append(f"/internal/word/figure[{figure_index}]")

        def scan_table(table: ET.Element, table_path: str) -> None:
            increment(NodeKind.TABLE)
            locators.append(table_path)
            for row_index, row in enumerate(
                table.findall("w:tr", NS),
                1,
            ):
                for cell_index, cell in enumerate(
                    row.findall("w:tc", NS),
                    1,
                ):
                    cell_path = f"{table_path}/tr[{row_index}]/tc[{cell_index}]"
                    for paragraph_index, paragraph in enumerate(
                        cell.findall("w:p", NS),
                        1,
                    ):
                        scan_paragraph(
                            paragraph,
                            f"{cell_path}/p[{paragraph_index}]",
                        )

        paragraph_index = 0
        table_index = 0
        section_index = 0
        for child in body:
            if child.tag == f"{{{W}}}p":
                paragraph_index += 1
                paragraph_path = self._paragraph_locator(
                    child,
                    paragraph_index,
                )
                scan_paragraph(child, paragraph_path)
                if child.find("w:pPr/w:sectPr", NS) is not None:
                    section_index += 1
                    increment(NodeKind.SECTION)
                    locators.append(f"/body/sectPr[{section_index}]")
            elif child.tag == f"{{{W}}}tbl":
                table_index += 1
                scan_table(child, f"/body/tbl[{table_index}]")
            elif child.tag == f"{{{W}}}sectPr":
                section_index += 1
                increment(NodeKind.SECTION)
                locators.append(f"/body/sectPr[{section_index}]")

        for prefix, kind in (
            ("word/header", NodeKind.HEADER),
            ("word/footer", NodeKind.FOOTER),
        ):
            parts = sorted(
                name
                for name in package.names(prefix)
                if name.endswith(".xml") and "/_rels/" not in name
            )
            for part_index, part in enumerate(parts, 1):
                story = package.xml(part)
                story_path = f"/{kind.value}[{part_index}]"
                for story_paragraph_index, paragraph in enumerate(
                    story.findall(".//w:p", NS),
                    1,
                ):
                    scan_paragraph(
                        paragraph,
                        f"{story_path}/p[{story_paragraph_index}]",
                    )
        return counts, locators

    def index(self, path: Path) -> DocumentIndex:
        digest = package_sha256(path)
        collector = _Collector()
        unsupported: list[str] = []
        with OoxmlPackage(path) as package:
            styles = self._styles(package)
            numbering_count = self._numbering_count(package)
            collector.add(
                indexed_node(
                    "/document",
                    NodeKind.DOCUMENT,
                    title=Path(path).name,
                    metadata={
                        "format": self.format.value,
                        "style_count": len(styles),
                        "numbering_definition_count": numbering_count,
                    },
                )
            )
            self._index_definitions(collector, package)
            root = package.xml("word/document.xml")
            relationships = package.relationships("word/document.xml")
            external_relationships = {
                item["id"]: item
                for item in package.external_relationships("word/document.xml")
            }
            body = root.find("w:body", NS)
            if body is None:
                unsupported.append("word/document.xml has no document body")
            else:
                self._index_body(
                    collector,
                    package,
                    body,
                    styles,
                    relationships,
                    external_relationships,
                    unsupported,
                )
            self._record_unsupported_features(root, unsupported)
            self._index_story_parts(
                collector,
                package,
                "word/header",
                NodeKind.HEADER,
                styles,
                unsupported,
            )
            self._index_story_parts(
                collector,
                package,
                "word/footer",
                NodeKind.FOOTER,
                styles,
                unsupported,
            )
            self._index_notes_comments(
                collector,
                package,
                "word/footnotes.xml",
                "footnote",
                NodeKind.FOOTNOTE,
            )
            self._index_notes_comments(
                collector,
                package,
                "word/endnotes.xml",
                "endnote",
                NodeKind.ENDNOTE,
            )
            self._index_notes_comments(
                collector,
                package,
                "word/comments.xml",
                "comment",
                NodeKind.COMMENT,
            )
            self._index_comment_anchors(collector, root)
        return DocumentIndex(
            self.format,
            digest,
            tuple(collector.nodes),
            tuple(collector.edges),
            tuple(dict.fromkeys(unsupported)),
        )

    @staticmethod
    def _record_unsupported_features(
        root: ET.Element,
        unsupported: list[str],
    ) -> None:
        descriptions = {
            "altChunk": "altChunk content was not expanded",
            "customXml": "custom XML content was not semantically decoded",
            "control": "embedded ActiveX control was not decoded",
            "oleObject": "embedded OLE object was not decoded",
            "sdt": "content-control semantics are only partially indexed",
        }
        names = {local_name(item.tag) for item in root.iter()}
        unsupported.extend(
            description for name, description in descriptions.items() if name in names
        )

    @staticmethod
    def _paragraph_locator(paragraph: ET.Element, ordinal: int) -> str:
        paragraph_id = paragraph.attrib.get(
            f"{{{W14}}}paraId"
        ) or attribute_by_local_name(paragraph, "paraId")
        return (
            f"/body/p[@paraId={paragraph_id}]"
            if paragraph_id
            else f"/body/p[{ordinal}]"
        )

    @staticmethod
    def _styles(
        package: OoxmlPackage,
    ) -> dict[str, _StyleDefinition]:
        if not package.exists("word/styles.xml"):
            return {}
        root = package.xml("word/styles.xml")
        result: dict[str, _StyleDefinition] = {}
        for style in root.findall("w:style", NS):
            identifier = style.attrib.get(f"{{{W}}}styleId")
            name = style.find("w:name", NS)
            if identifier:
                outline = style.find("w:pPr/w:outlineLvl", NS)
                try:
                    outline_level = (
                        int(outline.attrib.get(f"{{{W}}}val", "0")) + 1
                        if outline is not None
                        else None
                    )
                except ValueError:
                    outline_level = None
                based_on = style.find("w:basedOn", NS)
                result[identifier] = _StyleDefinition(
                    (
                        name.attrib.get(f"{{{W}}}val", identifier)
                        if name is not None
                        else identifier
                    ),
                    (
                        based_on.attrib.get(f"{{{W}}}val")
                        if based_on is not None
                        else None
                    ),
                    outline_level,
                )
        return result

    @staticmethod
    def _numbering_count(package: OoxmlPackage) -> int:
        if not package.exists("word/numbering.xml"):
            return 0
        root = package.xml("word/numbering.xml")
        return len(root.findall("w:num", NS))

    def _index_definitions(
        self,
        collector: _Collector,
        package: OoxmlPackage,
    ) -> None:
        if package.exists("word/styles.xml"):
            root = package.xml("word/styles.xml")
            for index, style in enumerate(root.findall("w:style", NS), 1):
                identifier = style.attrib.get(f"{{{W}}}styleId")
                name = style.find("w:name", NS)
                based_on = style.find("w:basedOn", NS)
                display = (
                    name.attrib.get(f"{{{W}}}val") if name is not None else identifier
                )
                collector.add(
                    indexed_node(
                        f"/styles/style[{index}]",
                        NodeKind.STYLE,
                        parent_path="/document",
                        title=display,
                        metadata={
                            "style_id": identifier,
                            "style_type": style.attrib.get(f"{{{W}}}type"),
                            "based_on": (
                                based_on.attrib.get(f"{{{W}}}val")
                                if based_on is not None
                                else None
                            ),
                        },
                        native_key=identifier,
                        stability=LocatorStability.NATIVE,
                        lineage_key=f"docx:style:{identifier}",
                        ordinal=index,
                    )
                )
        if package.exists("word/numbering.xml"):
            root = package.xml("word/numbering.xml")
            for index, numbering in enumerate(root.findall("w:num", NS), 1):
                identifier = numbering.attrib.get(f"{{{W}}}numId")
                abstract_numbering = numbering.find(
                    "w:abstractNumId",
                    NS,
                )
                collector.add(
                    indexed_node(
                        f"/numbering/num[{index}]",
                        NodeKind.NUMBERING,
                        parent_path="/document",
                        title=f"Numbering {identifier}",
                        metadata={
                            "num_id": identifier,
                            "abstract_num_id": (
                                abstract_numbering.attrib.get(f"{{{W}}}val")
                                if abstract_numbering is not None
                                else None
                            ),
                        },
                        native_key=identifier,
                        stability=LocatorStability.NATIVE,
                        lineage_key=f"docx:numbering:{identifier}",
                        ordinal=index,
                    )
                )

    def _index_body(  # noqa: C901
        self,
        collector: _Collector,
        package: OoxmlPackage,
        body: ET.Element,
        styles: dict[str, _StyleDefinition],
        relationships: dict[str, tuple[str, str]],
        external_relationships: dict[str, dict[str, str]],
        unsupported: list[str],
    ) -> None:
        """Index ordered Word body semantics; see ARCHITECTURE.md."""
        paragraph_index = 0
        table_index = 0
        section_index = 0
        heading_stack: dict[int, str] = {}
        bookmarks: dict[str, str] = {}
        pending_cross_references: list[tuple[str, str]] = []
        pending_caption: tuple[str, str | None] | None = None
        last_caption_target: tuple[str, NodeKind, int] | None = None
        for body_position, child in enumerate(body, 1):
            if child.tag == f"{{{W}}}p":
                paragraph_index += 1
                path = self._paragraph_locator(child, paragraph_index)
                node, heading_level = self._paragraph_node(
                    child,
                    path,
                    "/document",
                    styles,
                    paragraph_index,
                )
                collector.add(node)
                if heading_level is not None:
                    for level in list(heading_stack):
                        if level >= heading_level:
                            heading_stack.pop(level, None)
                    if heading_stack:
                        parent_level = max(
                            level for level in heading_stack if level < heading_level
                        )
                        collector.edge(
                            heading_stack[parent_level],
                            path,
                            "heading_child",
                        )
                    heading_stack[heading_level] = path
                before_children = len(collector.nodes)
                self._index_paragraph_children(
                    collector,
                    package,
                    child,
                    path,
                    relationships,
                    bookmarks,
                    pending_cross_references,
                    unsupported,
                    external_relationships,
                )
                new_targets = [
                    (item.stable_path, item.kind)
                    for item in collector.nodes[before_children:]
                    if item.kind
                    in {
                        NodeKind.FIGURE,
                        NodeKind.CHART,
                    }
                ]
                if node.metadata.get("is_caption"):
                    caption_kind = node.metadata.get("caption_kind")
                    if (
                        last_caption_target is not None
                        and last_caption_target[2] == body_position - 1
                        and self._caption_matches_target(
                            caption_kind,
                            last_caption_target[1],
                        )
                    ):
                        collector.edge(
                            path,
                            last_caption_target[0],
                            "caption_for",
                        )
                    else:
                        pending_caption = (path, caption_kind)
                elif new_targets:
                    first_target_path, first_target_kind = new_targets[0]
                    if pending_caption is not None and self._caption_matches_target(
                        pending_caption[1],
                        first_target_kind,
                    ):
                        collector.edge(
                            pending_caption[0],
                            first_target_path,
                            "caption_for",
                        )
                        pending_caption = None
                    last_target_path, last_target_kind = new_targets[-1]
                    last_caption_target = (
                        last_target_path,
                        last_target_kind,
                        body_position,
                    )
                paragraph_section = child.find("w:pPr/w:sectPr", NS)
                if paragraph_section is not None:
                    section_index += 1
                    self._add_section(
                        collector,
                        paragraph_section,
                        section_index,
                        path,
                    )
            elif child.tag == f"{{{W}}}tbl":
                table_index += 1
                table_path = f"/body/tbl[{table_index}]"
                self._index_table(
                    collector,
                    package,
                    child,
                    table_path,
                    "/document",
                    styles,
                    relationships,
                    external_relationships,
                    unsupported,
                )
                if pending_caption is not None and self._caption_matches_target(
                    pending_caption[1],
                    NodeKind.TABLE,
                ):
                    collector.edge(
                        pending_caption[0],
                        table_path,
                        "caption_for",
                    )
                    pending_caption = None
                last_caption_target = (
                    table_path,
                    NodeKind.TABLE,
                    body_position,
                )
            elif child.tag == f"{{{W}}}sectPr":
                section_index += 1
                self._add_section(
                    collector,
                    child,
                    section_index,
                    "/document",
                )
            else:
                unsupported.append(
                    f"Unsupported top-level Word object: {local_name(child.tag)}"
                )
        for source, bookmark_name in pending_cross_references:
            target = bookmarks.get(bookmark_name)
            if target:
                collector.edge(
                    source,
                    target,
                    "cross_reference",
                    {"bookmark": bookmark_name},
                )

    def _paragraph_node(
        self,
        paragraph: ET.Element,
        path: str,
        parent_path: str,
        styles: dict[str, _StyleDefinition],
        ordinal: int,
    ) -> tuple[IndexedNode, int | None]:
        kind, heading_level, style_id, style_name = self._paragraph_kind(
            paragraph,
            styles,
        )
        text = self._word_text(paragraph)
        caption_kind = self._caption_kind(
            paragraph,
            text,
            style_id,
            style_name,
        )
        numbering: dict[str, str] = {}
        for key in ("numId", "ilvl"):
            value = paragraph.find(f"w:pPr/w:numPr/w:{key}", NS)
            if value is not None:
                numbering[key] = value.attrib.get(f"{{{W}}}val", "")
        metadata: dict[str, Any] = {
            "style_id": style_id,
            "style_name": style_name,
            "heading_level": heading_level,
            "run_count": len(paragraph.findall(".//w:r", NS)),
            "numbering": numbering,
            "tracked_revision_count": len(
                [
                    item
                    for item in paragraph.iter()
                    if local_name(item.tag) in {"ins", "del", "moveFrom", "moveTo"}
                ]
            ),
            "is_caption": caption_kind is not None,
            "caption_kind": caption_kind,
        }
        title = text if kind is NodeKind.HEADING else None
        paragraph_id = paragraph.attrib.get(
            f"{{{W14}}}paraId"
        ) or attribute_by_local_name(paragraph, "paraId")
        return (
            indexed_node(
                path,
                kind,
                parent_path=parent_path,
                title=title,
                text=text,
                metadata=metadata,
                ordinal=ordinal,
                native_key=paragraph_id,
                stability=(
                    LocatorStability.NATIVE
                    if paragraph_id
                    else LocatorStability.REVISION_SCOPED
                ),
                lineage_key=(
                    f"docx:paragraph:{paragraph_id}" if paragraph_id else None
                ),
                namespace=LocatorNamespace.OFFICECLI,
                resolvable=True,
            ),
            heading_level,
        )

    @staticmethod
    def _caption_kind(
        paragraph: ET.Element,
        text: str,
        style_id: str | None,
        style_name: str | None,
    ) -> str | None:
        style_is_caption = bool(
            re.search(
                r"\bcaption\b",
                f"{style_id or ''} {style_name or ''}",
                re.IGNORECASE,
            )
        )
        field_text = " ".join(
            (item.attrib.get(f"{{{W}}}instr", "") or item.text or "")
            for item in paragraph.iter()
            if local_name(item.tag) in {"fldSimple", "instrText"}
        )
        if re.search(r"\bSEQ\s+TABLE\b", field_text, re.IGNORECASE):
            return "table"
        if re.search(
            r"\bSEQ\s+(?:FIGURE|FIG\.?|CHART|IMAGE)\b",
            field_text,
            re.IGNORECASE,
        ):
            return "figure"
        if not style_is_caption:
            return None
        if re.match(r"\s*TABLE\b", text, re.IGNORECASE):
            return "table"
        if re.match(
            r"\s*(?:FIGURE|FIG\.?|CHART|IMAGE)\b",
            text,
            re.IGNORECASE,
        ):
            return "figure"
        return "generic"

    @staticmethod
    def _caption_matches_target(
        caption_kind: object,
        target_kind: NodeKind,
    ) -> bool:
        if caption_kind == "table":
            return target_kind is NodeKind.TABLE
        if caption_kind == "figure":
            return target_kind in {NodeKind.FIGURE, NodeKind.CHART}
        return caption_kind in {None, "generic"}

    @staticmethod
    def _paragraph_kind(
        paragraph: ET.Element,
        styles: dict[str, _StyleDefinition],
    ) -> tuple[NodeKind, int | None, str | None, str | None]:
        style = paragraph.find("w:pPr/w:pStyle", NS)
        style_id = style.attrib.get(f"{{{W}}}val") if style is not None else None
        definition = styles.get(style_id or "")
        style_name = definition.name if definition is not None else style_id
        outline = paragraph.find("w:pPr/w:outlineLvl", NS)
        level: int | None = None
        if outline is not None:
            try:
                level = int(outline.attrib.get(f"{{{W}}}val", "0")) + 1
            except ValueError:
                level = None
        current_style = style_id
        visited: set[str] = set()
        style_chain: list[tuple[str, _StyleDefinition]] = []
        while current_style and current_style not in visited:
            visited.add(current_style)
            current = styles.get(current_style)
            if current is None:
                break
            style_chain.append((current_style, current))
            if level is None and current.outline_level is not None:
                level = current.outline_level
            current_style = current.based_on
        for current_style, current in style_chain:
            heading_match = re.search(
                r"(?:heading|title)\s*([1-9])?",
                f"{current_style} {current.name}",
                re.IGNORECASE,
            )
            if heading_match:
                explicit_level = heading_match.group(1)
                if explicit_level:
                    level = int(explicit_level)
                    break
                if level is None:
                    level = 1
        return (
            NodeKind.HEADING if level is not None else NodeKind.PARAGRAPH,
            level,
            style_id,
            style_name,
        )

    @staticmethod
    def _word_text(element: ET.Element) -> str:
        values: list[str] = []
        for item in element.iter():
            name = local_name(item.tag)
            if name in {"t", "delText", "instrText"} and item.text:
                values.append(item.text)
            elif name in {"tab"}:
                values.append("\t")
            elif name in {"br", "cr"}:
                values.append("\n")
        return re.sub(r"[ \t]+", " ", "".join(values)).strip()

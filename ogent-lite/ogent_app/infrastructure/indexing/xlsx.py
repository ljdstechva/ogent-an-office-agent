"""Streaming structural XLSX indexer with exact OfficeCLI cell locators."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
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
    indexed_node,
    local_name,
    normalized_text,
    package_sha256,
    relationship_id,
)
from .xlsx_schema import NS
from .xlsx_visual_mixin import XlsxVisualMixin


class XlsxIndexer(XlsxVisualMixin):
    format = DocumentFormat.XLSX

    def __init__(self, *, max_cells_per_sheet: int = 500_000) -> None:
        self.max_cells_per_sheet = max(1, int(max_cells_per_sheet))

    def quick_inventory(self, path: Path) -> StructuralManifest:
        digest = package_sha256(path)
        with OoxmlPackage(path) as package:
            workbook = package.xml("xl/workbook.xml")
            sheets = workbook.findall("x:sheets/x:sheet", NS)
            contexts = self._sheet_contexts(
                sheets,
                package.relationships("xl/workbook.xml"),
            )
            table_count = 0
            chart_count = 0
            figure_count = 0
            locators: list[str] = []
            for context in contexts:
                locators.append(context["path"])
                part = context["part"]
                if not part or not package.exists(part):
                    continue
                relationships = package.relationships(part)
                tables = [
                    target
                    for target, rel_type in relationships.values()
                    if rel_type.rsplit("/", 1)[-1].casefold() == "table"
                ]
                table_count += len(tables)
                locators.extend(
                    f"{context['path']}/table[{index}]"
                    for index in range(1, len(tables) + 1)
                )
                drawing_part = next(
                    (
                        target
                        for target, rel_type in relationships.values()
                        if rel_type.rsplit("/", 1)[-1].casefold() == "drawing"
                    ),
                    "",
                )
                if drawing_part and package.exists(drawing_part):
                    drawing_root = package.xml(drawing_part)
                    attached = len(
                        [
                            item
                            for item in drawing_root.iter()
                            if local_name(item.tag) == "chart"
                        ]
                    )
                    chart_count += attached
                    locators.extend(
                        f"{context['path']}/chart[{index}]"
                        for index in range(1, attached + 1)
                    )
                    pictures = len(
                        [
                            item
                            for item in drawing_root.iter()
                            if local_name(item.tag) == "pic"
                        ]
                    )
                    figure_count += pictures
                    locators.extend(
                        f"{context['path']}/picture[{index}]"
                        for index in range(1, pictures + 1)
                    )
            counts = {
                NodeKind.SHEET.value: len(sheets),
                NodeKind.TABLE.value: table_count,
                NodeKind.CHART.value: chart_count,
                NodeKind.FIGURE.value: figure_count,
            }
        return StructuralManifest(
            self.format,
            digest,
            counts,
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

    def iter_batches(
        self,
        path: Path,
        *,
        batch_size: int = 500,
    ) -> Iterator[IndexBatch]:
        digest = package_sha256(path)
        with OoxmlPackage(path) as package:
            workbook = package.xml("xl/workbook.xml")
            workbook_rels = package.relationships("xl/workbook.xml")
            shared_strings = self._shared_strings(package)
            sheets = workbook.findall("x:sheets/x:sheet", NS)
            sheet_contexts = self._sheet_contexts(
                sheets,
                workbook_rels,
            )
            root_nodes = [
                indexed_node(
                    "/document",
                    NodeKind.DOCUMENT,
                    title=Path(path).name,
                    metadata={"format": self.format.value},
                    stability=LocatorStability.SYNTHETIC,
                    lineage_key=f"xlsx:package:{digest}",
                ),
                indexed_node(
                    "/",
                    NodeKind.WORKBOOK,
                    parent_path="/document",
                    title=Path(path).name,
                    metadata={
                        "sheet_count": len(sheets),
                        "date1904": self._workbook_flag(
                            workbook,
                            "date1904",
                        ),
                    },
                    stability=LocatorStability.REVISION_SCOPED,
                    native_key="xl/workbook.xml",
                    lineage_key="xlsx:workbook",
                    namespace=LocatorNamespace.OFFICECLI,
                    resolvable=True,
                ),
            ]
            root_edges = [IndexedEdge("/document", "/", "contains")]
            named_nodes, named_edges = self._named_ranges(
                workbook,
                sheet_contexts,
            )
            named_ranges = [
                (
                    str(node.metadata.get("name")),
                    (
                        str(node.metadata.get("local_sheet"))
                        if node.metadata.get("local_sheet")
                        else None
                    ),
                    node.stable_path,
                )
                for node in named_nodes
                if node.metadata.get("name")
            ]
            root_nodes.extend(named_nodes)
            root_edges.extend(named_edges)
            yield IndexBatch(
                tuple(root_nodes),
                tuple(root_edges),
                progress=0.01,
            )

            known_paths = {node.stable_path for node in root_nodes}
            formulas: list[tuple[str, str, str, bool]] = []
            total_sheets = max(1, len(sheet_contexts))
            for sheet_index, context in enumerate(sheet_contexts, 1):
                nodes, edges, sheet_formulas, unsupported = self._index_sheet(
                    package,
                    context,
                    shared_strings,
                )
                formulas.extend(sheet_formulas)
                known_paths.update(node.stable_path for node in nodes)
                chunks = [
                    nodes[index : index + max(1, batch_size)]
                    for index in range(0, len(nodes), max(1, batch_size))
                ] or [[]]
                for chunk_index, chunk in enumerate(chunks, 1):
                    fraction = chunk_index / len(chunks)
                    progress = min(
                        0.9,
                        0.9 * (sheet_index - 1 + fraction) / total_sheets,
                    )
                    yield IndexBatch(
                        tuple(chunk),
                        (),
                        progress=progress,
                    )
                yield IndexBatch(
                    (),
                    tuple(edges),
                    progress=min(
                        0.95,
                        0.9 + (0.05 * sheet_index / total_sheets),
                    ),
                    unsupported=tuple(unsupported),
                )

            dependency_nodes, dependency_edges, dependency_unsupported = (
                self._formula_dependencies(
                    formulas,
                    sheet_contexts,
                    known_paths,
                    named_ranges,
                )
            )
            yield IndexBatch(
                tuple(dependency_nodes),
                tuple(dependency_edges),
                progress=1.0,
                unsupported=tuple(dependency_unsupported),
                complete=True,
            )

    @staticmethod
    def _workbook_flag(root: ET.Element, name: str) -> bool:
        properties = root.find("x:workbookPr", NS)
        if properties is None:
            return False
        return properties.attrib.get(name, "0") in {"1", "true"}

    @staticmethod
    def _sheet_contexts(
        sheets: list[ET.Element],
        relationships: dict[str, tuple[str, str]],
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for index, sheet in enumerate(sheets, 1):
            name = sheet.attrib.get("name", f"Sheet{index}")
            sheet_id = sheet.attrib.get("sheetId", str(index))
            rel_id = relationship_id(sheet)
            part = relationships.get(rel_id or "", ("", ""))[0]
            result.append(
                {
                    "name": name,
                    "sheet_id": sheet_id,
                    "relationship_id": rel_id or "",
                    "part": part,
                    "state": sheet.attrib.get("state", "visible"),
                    "path": f"/{name}",
                }
            )
        return result

    @staticmethod
    def _shared_strings(package: OoxmlPackage) -> tuple[str, ...]:
        if not package.exists("xl/sharedStrings.xml"):
            return ()
        root = package.xml("xl/sharedStrings.xml")
        return tuple(normalized_text(item) for item in root.findall("x:si", NS))

    def _named_ranges(
        self,
        workbook: ET.Element,
        sheets: list[dict[str, str]],
    ) -> tuple[list[IndexedNode], list[IndexedEdge]]:
        nodes: list[IndexedNode] = []
        edges: list[IndexedEdge] = []
        sheet_by_index = {str(index): sheet for index, sheet in enumerate(sheets)}
        for index, item in enumerate(
            workbook.findall("x:definedNames/x:definedName", NS),
            1,
        ):
            name = item.attrib.get("name", f"Name{index}")
            text = (item.text or "").strip()
            path = f"/namedrange[{index}]"
            local_sheet = sheet_by_index.get(item.attrib.get("localSheetId", ""))
            scope_key = (
                str(local_sheet["sheet_id"]) if local_sheet is not None else "workbook"
            )
            native_key = (
                f"{local_sheet['name']}!{name}" if local_sheet is not None else name
            )
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.NAMED_RANGE,
                    parent_path="/",
                    title=name,
                    text=text,
                    metadata={
                        "name": name,
                        "formula": text,
                        "hidden": item.attrib.get("hidden") in {"1", "true"},
                        "local_sheet": (local_sheet["name"] if local_sheet else None),
                    },
                    native_key=native_key,
                    stability=LocatorStability.NAMED,
                    lineage_key=f"xlsx:named-range:{scope_key}:{name}",
                    ordinal=index,
                    namespace=LocatorNamespace.OFFICECLI,
                    resolvable=True,
                )
            )
            edges.append(IndexedEdge("/", path, "contains"))
        return nodes, edges

    def _index_sheet(
        self,
        package: OoxmlPackage,
        context: dict[str, str],
        shared_strings: tuple[str, ...],
    ) -> tuple[
        list[IndexedNode],
        list[IndexedEdge],
        list[tuple[str, str, str, bool]],
        list[str],
    ]:
        path = context["path"]
        part = context["part"]
        nodes: list[IndexedNode] = [
            indexed_node(
                path,
                NodeKind.SHEET,
                parent_path="/",
                title=context["name"],
                metadata={
                    "sheet_id": context["sheet_id"],
                    "state": context["state"],
                    "hidden": context["state"] in {"hidden", "veryHidden"},
                    "very_hidden": context["state"] == "veryHidden",
                    "package_part": part,
                },
                sheet_name=context["name"],
                native_key=context["sheet_id"],
                stability=LocatorStability.REVISION_SCOPED,
                lineage_key=f"xlsx:sheet:{context['sheet_id']}",
                namespace=LocatorNamespace.OFFICECLI,
                resolvable=True,
            )
        ]
        edges: list[IndexedEdge] = [IndexedEdge("/", path, "contains")]
        formulas: list[tuple[str, str, str, bool]] = []
        shared_formulas: dict[str, str] = {}
        unsupported: list[str] = []
        if not part or not package.exists(part):
            unsupported.append(f"Missing worksheet part for sheet {context['name']}")
            return nodes, edges, formulas, unsupported
        root = package.xml(part)
        relationships = package.relationships(part)
        dimension = root.find("x:dimension", NS)
        used_range = dimension.attrib.get("ref") if dimension is not None else None
        if used_range:
            range_path = f"{path}/{used_range}"
            nodes.append(
                indexed_node(
                    range_path,
                    NodeKind.RANGE,
                    parent_path=path,
                    title="Used range",
                    metadata={"reference": used_range, "used_range": True},
                    sheet_name=context["name"],
                    stability=LocatorStability.REVISION_SCOPED,
                    lineage_key=(f"xlsx:used-range:{context['sheet_id']}"),
                    namespace=LocatorNamespace.OFFICECLI,
                    resolvable=True,
                )
            )
            edges.append(IndexedEdge(path, range_path, "contains"))
        for ordinal, cell in enumerate(
            root.iterfind(".//x:sheetData/x:row/x:c", NS),
            1,
        ):
            if ordinal > self.max_cells_per_sheet:
                unsupported.append(
                    "Worksheet cell inventory exceeded the configured "
                    f"{self.max_cells_per_sheet} cell limit on "
                    f"{context['name']}"
                )
                break
            reference = cell.attrib.get("r")
            if not reference:
                continue
            cell_path = f"{path}/{reference}"
            formula_element = cell.find("x:f", NS)
            formula = (
                (formula_element.text or "").strip()
                if formula_element is not None
                else ""
            )
            shared_formula = False
            if (
                formula_element is not None
                and formula_element.attrib.get("t") == "shared"
            ):
                shared_index = formula_element.attrib.get("si", "")
                if formula:
                    shared_formulas[shared_index] = formula
                elif shared_index in shared_formulas:
                    formula = shared_formulas[shared_index]
                    shared_formula = True
            value_element = cell.find("x:v", NS)
            raw_value = value_element.text if value_element is not None else ""
            value = self._cell_value(
                cell,
                raw_value or "",
                shared_strings,
            )
            kind = NodeKind.FORMULA if formula else NodeKind.CELL
            nodes.append(
                indexed_node(
                    cell_path,
                    kind,
                    parent_path=path,
                    title=reference,
                    text=value,
                    metadata={
                        "reference": reference,
                        "formula": formula or None,
                        "cached_value": raw_value,
                        "cell_type": cell.attrib.get("t"),
                        "style_index": cell.attrib.get("s"),
                        "shared_formula_template": shared_formula,
                    },
                    sheet_name=context["name"],
                    native_key=(f"{context['sheet_id']}!{reference.upper()}"),
                    stability=LocatorStability.NATIVE,
                    lineage_key=(
                        f"xlsx:cell:{context['sheet_id']}!{reference.upper()}"
                    ),
                    ordinal=ordinal,
                    namespace=LocatorNamespace.OFFICECLI,
                    resolvable=True,
                )
            )
            edges.append(IndexedEdge(path, cell_path, "contains"))
            if formula:
                formulas.append(
                    (
                        cell_path,
                        formula,
                        context["name"],
                        shared_formula,
                    )
                )
        self._index_rules(nodes, edges, root, context)
        self._index_related_parts(
            nodes,
            edges,
            package,
            root,
            relationships,
            context,
            unsupported,
        )
        return nodes, edges, formulas, unsupported

    @staticmethod
    def _cell_value(
        cell: ET.Element,
        raw: str,
        shared_strings: tuple[str, ...],
    ) -> str:
        cell_type = cell.attrib.get("t")
        if cell_type == "s":
            try:
                return shared_strings[int(raw)]
            except (ValueError, IndexError):
                return raw
        if cell_type == "inlineStr":
            inline = cell.find("x:is", NS)
            return normalized_text(inline) if inline is not None else ""
        if cell_type == "b":
            return "TRUE" if raw == "1" else "FALSE"
        return raw

    @staticmethod
    def _index_rules(
        nodes: list[IndexedNode],
        edges: list[IndexedEdge],
        root: ET.Element,
        context: dict[str, str],
    ) -> None:
        sheet_path = context["path"]
        sheet_name = context["name"]
        for kind, query, segment in (
            (
                NodeKind.DATA_VALIDATION,
                ".//x:dataValidations/x:dataValidation",
                "validation",
            ),
            (
                NodeKind.CONDITIONAL_FORMAT,
                ".//x:conditionalFormatting",
                "cf",
            ),
        ):
            for index, item in enumerate(root.findall(query, NS), 1):
                path = f"{sheet_path}/{segment}[{index}]"
                nodes.append(
                    indexed_node(
                        path,
                        kind,
                        parent_path=sheet_path,
                        text=normalized_text(item),
                        metadata={
                            "ranges": item.attrib.get(
                                "sqref",
                                item.attrib.get("ref"),
                            ),
                            "type": item.attrib.get("type"),
                        },
                        sheet_name=sheet_name,
                        ordinal=index,
                        namespace=LocatorNamespace.OFFICECLI,
                        resolvable=True,
                    )
                )
                edges.append(IndexedEdge(sheet_path, path, "contains"))
        sparkline_groups = [
            item for item in root.iter() if local_name(item.tag) == "sparklineGroup"
        ]
        for index, item in enumerate(sparkline_groups, 1):
            path = f"{sheet_path}/sparkline[{index}]"
            nodes.append(
                indexed_node(
                    path,
                    NodeKind.SPARKLINE,
                    parent_path=sheet_path,
                    text=normalized_text(item),
                    metadata={
                        "sparkline_count": len(
                            [
                                child
                                for child in item.iter()
                                if local_name(child.tag) == "sparkline"
                            ]
                        )
                    },
                    sheet_name=sheet_name,
                    ordinal=index,
                    namespace=LocatorNamespace.OFFICECLI,
                    resolvable=True,
                )
            )
            edges.append(IndexedEdge(sheet_path, path, "contains"))

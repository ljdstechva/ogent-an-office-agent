from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ogent_app.domain.document_intelligence import NodeKind
from ogent_app.infrastructure.indexing import (
    DocxIndexer,
    PdfIndexer,
    PptxIndexer,
    XlsxIndexer,
)


def _package(path: Path, parts: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(
                name,
                payload.encode("utf-8") if isinstance(payload, str) else payload,
            )
    return path


class RichDocxIndexerTests(unittest.TestCase):
    def test_inherited_headings_captions_and_visual_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = _package(
                Path(temporary) / "rich.docx",
                self._parts(),
            )
            indexer = DocxIndexer()
            quick = indexer.quick_inventory(document)
            indexed = indexer.index(document)
            by_path = {node.stable_path: node for node in indexed.nodes}

            heading = by_path["/body/p[@paraId=10000001]"]
            self.assertEqual(heading.kind, NodeKind.HEADING)
            self.assertEqual(heading.metadata["heading_level"], 2)
            self.assertEqual(quick.count(NodeKind.HEADING), 1)
            self.assertEqual(quick.count(NodeKind.FIGURE), 1)
            self.assertEqual(quick.count(NodeKind.CHART), 1)
            self.assertEqual(
                quick.count(NodeKind.FIGURE),
                sum(node.kind is NodeKind.FIGURE for node in indexed.nodes),
            )
            self.assertEqual(
                quick.count(NodeKind.CHART),
                sum(node.kind is NodeKind.CHART for node in indexed.nodes),
            )
            core_paths = {
                node.stable_path
                for node in indexed.nodes
                if node.kind
                in {
                    NodeKind.HEADING,
                    NodeKind.TABLE,
                    NodeKind.FIGURE,
                    NodeKind.CHART,
                    NodeKind.SECTION,
                }
            }
            self.assertEqual(set(quick.stable_paths), core_paths)

            caption_edges = {
                (edge.source_path, edge.target_path)
                for edge in indexed.edges
                if edge.edge_type == "caption_for"
            }
            self.assertIn(
                (
                    "/body/p[@paraId=10000003]",
                    "/internal/word/figure[1]",
                ),
                caption_edges,
            )
            self.assertIn(
                (
                    "/body/p[@paraId=10000005]",
                    "/body/tbl[1]",
                ),
                caption_edges,
            )
            tables = [node for node in indexed.nodes if node.kind is NodeKind.TABLE]
            self.assertEqual(len(tables), 2)
            self.assertEqual(
                [node.title for node in tables],
                ["Repeated caption", "Repeated caption"],
            )
            self.assertTrue(all(node.lineage_key is None for node in tables))

    @staticmethod
    def _parts() -> dict[str, str]:
        document = """\
<w:document
 xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
 <w:body>
  <w:p w14:paraId="10000001">
   <w:pPr><w:pStyle w:val="CustomHeading"/></w:pPr>
   <w:r><w:t>Inherited heading</w:t></w:r>
  </w:p>
  <w:p w14:paraId="10000002">
   <w:r><w:drawing><wp:inline><wp:docPr id="7" name="Figure 1"
    descr="A monitored outfall"/><a:graphic><a:graphicData>
    <a:blip r:embed="rIdImage"/>
   </a:graphicData></a:graphic></wp:inline></w:drawing></w:r>
  </w:p>
  <w:p w14:paraId="10000003">
   <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
   <w:r><w:t>Figure 1. Monitored outfall</w:t></w:r>
  </w:p>
  <w:p w14:paraId="10000004">
   <w:r><w:drawing><wp:inline><wp:docPr id="8" name="Chart 1"/>
    <a:graphic><a:graphicData><c:chart r:id="rIdChart"/>
    </a:graphicData></a:graphic>
   </wp:inline></w:drawing></w:r>
  </w:p>
  <w:p w14:paraId="10000005">
   <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
   <w:r><w:t>Table 1. Results</w:t></w:r>
  </w:p>
  <w:tbl>
   <w:tblPr><w:tblCaption w:val="Repeated caption"/></w:tblPr>
   <w:tr><w:tc><w:p><w:r><w:t>First</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:tbl>
   <w:tblPr><w:tblCaption w:val="Repeated caption"/></w:tblPr>
   <w:tr><w:tc><w:p><w:r><w:t>Second</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>
 </w:body>
</w:document>"""
        styles = """\
<w:styles
 xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:style w:type="paragraph" w:styleId="BaseHeading">
  <w:name w:val="Section Level"/>
  <w:pPr><w:outlineLvl w:val="1"/></w:pPr>
 </w:style>
 <w:style w:type="paragraph" w:styleId="CustomHeading">
  <w:name w:val="Custom section"/>
  <w:basedOn w:val="BaseHeading"/>
 </w:style>
 <w:style w:type="paragraph" w:styleId="Caption">
  <w:name w:val="Caption"/>
 </w:style>
</w:styles>"""
        relationships = """\
<Relationships
 xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rIdImage"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
  Target="media/image1.png"/>
 <Relationship Id="rIdChart"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
  Target="charts/chart1.xml"/>
</Relationships>"""
        chart = """\
<c:chartSpace
 xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
 <c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>Flow</a:t>
 </a:r></a:p></c:rich></c:tx></c:title>
 <c:plotArea><c:barChart><c:ser><c:tx><c:v>Series A</c:v></c:tx>
 <c:cat><c:strRef><c:f>Sheet1!$A$2:$A$3</c:f>
 <c:strCache><c:pt><c:v>Jan</c:v></c:pt></c:strCache></c:strRef></c:cat>
 <c:val><c:numRef><c:f>Sheet1!$B$2:$B$3</c:f>
 <c:numCache><c:pt><c:v>1</c:v></c:pt></c:numCache></c:numRef></c:val>
 </c:ser></c:barChart></c:plotArea></c:chart>
</c:chartSpace>"""
        return {
            "word/document.xml": document,
            "word/styles.xml": styles,
            "word/_rels/document.xml.rels": relationships,
            "word/charts/chart1.xml": chart,
            "word/media/image1.png": b"\x89PNG\r\n\x1a\n",
        }


class RichXlsxIndexerTests(unittest.TestCase):
    def test_scoped_names_shared_formulas_and_table_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = _package(
                Path(temporary) / "rich.xlsx",
                self._parts(),
            )
            indexer = XlsxIndexer()
            quick = indexer.quick_inventory(document)
            indexed = indexer.index(document)
            by_path = {node.stable_path: node for node in indexed.nodes}

            self.assertEqual(quick.count(NodeKind.SHEET), 2)
            self.assertEqual(quick.count(NodeKind.TABLE), 2)
            self.assertEqual(
                {path for path in quick.stable_paths if "/table[" in path},
                {"/Sheet1/table[1]", "/Sheet1/table[2]"},
            )
            self.assertEqual(
                by_path["/Sheet1/table[1]"].title,
                "SecondTable",
            )
            self.assertEqual(
                by_path["/Sheet1/table[2]"].title,
                "FirstTable",
            )

            named = [
                node for node in indexed.nodes if node.kind is NodeKind.NAMED_RANGE
            ]
            self.assertEqual(
                [node.native_key for node in named],
                ["Budget", "Sheet1!Budget"],
            )
            self.assertEqual(
                len({node.lineage_key for node in named}),
                2,
            )
            named_edges = {
                (edge.source_path, edge.target_path)
                for edge in indexed.edges
                if edge.edge_type == "formula_depends_on_named_range"
            }
            self.assertIn(("/Sheet1/A1", "/namedrange[2]"), named_edges)
            self.assertNotIn(("/Sheet1/A1", "/namedrange[1]"), named_edges)
            self.assertIn(("/Sheet2/A1", "/namedrange[1]"), named_edges)

            follower_edges = [
                edge
                for edge in indexed.edges
                if edge.source_path == "/Sheet1/A3"
                and edge.edge_type == "formula_depends_on"
            ]
            self.assertEqual(follower_edges, [])
            self.assertTrue(
                any("Shared-formula follower" in item for item in indexed.unsupported)
            )

    @staticmethod
    def _parts() -> dict[str, str]:
        workbook = """\
<workbook
 xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets>
  <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
  <sheet name="Sheet2" sheetId="2" r:id="rId2"/>
 </sheets>
 <definedNames>
  <definedName name="Budget">Sheet2!$B$1</definedName>
  <definedName name="Budget" localSheetId="0">Sheet1!$B$1</definedName>
 </definedNames>
</workbook>"""
        workbook_relationships = """\
<Relationships
 xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
  Target="worksheets/sheet1.xml"/>
 <Relationship Id="rId2"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
  Target="worksheets/sheet2.xml"/>
</Relationships>"""
        sheet1 = """\
<worksheet
 xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <dimension ref="A1:B3"/>
 <sheetData>
  <row r="1"><c r="A1"><f>Budget+1</f><v>3</v></c>
   <c r="B1"><v>2</v></c></row>
  <row r="2"><c r="A2"><f t="shared" si="0" ref="A2:A3">B1+1</f>
   <v>3</v></c></row>
  <row r="3"><c r="A3"><f t="shared" si="0"/><v>4</v></c></row>
 </sheetData>
 <tableParts count="2">
  <tablePart r:id="rIdT2"/>
  <tablePart r:id="rIdT1"/>
 </tableParts>
</worksheet>"""
        sheet1_relationships = """\
<Relationships
 xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rIdT1"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table"
  Target="../tables/table1.xml"/>
 <Relationship Id="rIdT2"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table"
  Target="../tables/table2.xml"/>
</Relationships>"""
        sheet2 = """\
<worksheet
 xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <dimension ref="A1:B1"/>
 <sheetData><row r="1"><c r="A1"><f>Budget+1</f><v>9</v></c>
 <c r="B1"><v>8</v></c></row></sheetData>
</worksheet>"""
        table1 = """\
<table
 xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 id="1" name="FirstTable" displayName="FirstTable" ref="A1:A2">
 <tableColumns count="1"><tableColumn id="1" name="First"/></tableColumns>
</table>"""
        table2 = """\
<table
 xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 id="2" name="SecondTable" displayName="SecondTable" ref="B1:B2">
 <tableColumns count="1"><tableColumn id="1" name="Second"/></tableColumns>
</table>"""
        return {
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": workbook_relationships,
            "xl/worksheets/sheet1.xml": sheet1,
            "xl/worksheets/_rels/sheet1.xml.rels": sheet1_relationships,
            "xl/worksheets/sheet2.xml": sheet2,
            "xl/tables/table1.xml": table1,
            "xl/tables/table2.xml": table2,
        }


class RichPptxIndexerTests(unittest.TestCase):
    def test_groups_hidden_slides_and_connector_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = _package(
                Path(temporary) / "rich.pptx",
                self._parts(),
            )
            indexer = PptxIndexer()
            quick = indexer.quick_inventory(document)
            batches = tuple(indexer.iter_batches(document))
            indexed = indexer.index(document)
            by_path = {node.stable_path: node for node in indexed.nodes}

            self.assertTrue(batches[-1].complete)
            self.assertEqual(batches[-1].progress, 1.0)
            self.assertTrue(by_path["/slide[1]"].metadata["hidden"])
            child = by_path["/slide[1]/group[1]/shape[1]"]
            self.assertEqual(child.parent_path, "/slide[1]/group[1]")
            self.assertTrue(child.officecli_resolvable)
            self.assertEqual(child.native_key, "10")

            flows = [
                node for node in indexed.nodes if node.kind is NodeKind.PROCESS_FLOW
            ]
            self.assertEqual(len(flows), 2)
            self.assertEqual(
                [node.metadata["connector_count"] for node in flows],
                [1, 1],
            )
            self.assertTrue(
                any(
                    "Unresolved connector endpoints" in item and "connector[3]" in item
                    for item in indexed.unsupported
                )
            )
            core_kinds = {
                NodeKind.SLIDE,
                NodeKind.TABLE,
                NodeKind.FIGURE,
                NodeKind.CHART,
                NodeKind.PROCESS_FLOW,
            }
            self.assertEqual(
                set(quick.stable_paths),
                {node.stable_path for node in indexed.nodes if node.kind in core_kinds},
            )
            self.assertEqual(quick.count(NodeKind.PROCESS_FLOW), 2)
            edge_types = {
                (
                    edge.source_path,
                    edge.target_path,
                    edge.edge_type,
                )
                for edge in indexed.edges
            }
            self.assertIn(
                (
                    "/slide[1]",
                    "/internal/pptx/layout[1]",
                    "uses_layout",
                ),
                edge_types,
            )
            self.assertIn(
                (
                    "/internal/pptx/layout[1]",
                    "/internal/pptx/master[1]",
                    "uses_master",
                ),
                edge_types,
            )

    @staticmethod
    def _parts() -> dict[str, str]:
        presentation = """\
<p:presentation
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <p:sldIdLst><p:sldId id="256" r:id="rId1" show="0"/></p:sldIdLst>
</p:presentation>"""
        presentation_relationships = """\
<Relationships
 xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
  Target="slides/slide1.xml"/>
</Relationships>"""
        shape = """\
<p:sp><p:nvSpPr><p:cNvPr id="{identifier}" name="{name}"/>
 <p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>
 <a:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/></a:xfrm>
 <a:prstGeom prst="rect"/></p:spPr>
 <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{name}</a:t>
 </a:r></a:p></p:txBody></p:sp>"""
        connector = """\
<p:cxnSp><p:nvCxnSpPr><p:cNvPr id="{identifier}" name="Connector"/>
 <p:cNvCxnSpPr><a:stCxn id="{start}" idx="0"/>
 <a:endCxn id="{end}" idx="0"/></p:cNvCxnSpPr><p:nvPr/>
 </p:nvCxnSpPr><p:spPr><a:xfrm><a:off x="0" y="0"/>
 <a:ext cx="100" cy="100"/></a:xfrm>
 <a:prstGeom prst="straightConnector1"/></p:spPr></p:cxnSp>"""
        group = f"""\
<p:grpSp><p:nvGrpSpPr><p:cNvPr id="9" name="Nested group"/>
 <p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
 <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/>
 <a:chOff x="0" y="0"/><a:chExt cx="100" cy="100"/></a:xfrm></p:grpSpPr>
 {shape.format(identifier="10", name="Grouped shape")}
</p:grpSp>"""
        picture = """\
<p:pic><p:nvPicPr><p:cNvPr id="11" name="Outfall" descr="Outfall photo"/>
 <p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill>
 <a:blip r:embed="rIdImage"/></p:blipFill><p:spPr>
 <a:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/></a:xfrm>
 </p:spPr></p:pic>"""
        table = """\
<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="12" name="Results"/>
 <p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
 <p:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/></p:xfrm>
 <a:graphic><a:graphicData><a:tbl><a:tr h="1"><a:tc>
 <a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Result</a:t>
 </a:r></a:p></a:txBody><a:tcPr/></a:tc></a:tr></a:tbl>
 </a:graphicData></a:graphic></p:graphicFrame>"""
        chart_frame = """\
<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="13" name="Trend"/>
 <p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
 <p:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/></p:xfrm>
 <a:graphic><a:graphicData><c:chart r:id="rIdChart"/>
 </a:graphicData></a:graphic></p:graphicFrame>"""
        slide = f"""\
<p:sld
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <p:cSld name="Process"><p:spTree>
  <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>
  </p:nvGrpSpPr><p:grpSpPr/>
  {shape.format(identifier="2", name="Start A")}
  {shape.format(identifier="3", name="End A")}
  {connector.format(identifier="4", start="2", end="3")}
  {shape.format(identifier="5", name="Start B")}
  {shape.format(identifier="6", name="End B")}
  {connector.format(identifier="7", start="5", end="6")}
  {connector.format(identifier="8", start="2", end="999")}
  {group}
  {picture}
  {table}
  {chart_frame}
 </p:spTree></p:cSld>
</p:sld>"""
        slide_relationships = """\
<Relationships
 xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rIdLayout"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
  Target="../slideLayouts/slideLayout1.xml"/>
 <Relationship Id="rIdChart"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
  Target="../charts/chart1.xml"/>
 <Relationship Id="rIdImage"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
  Target="../media/image1.png"/>
</Relationships>"""
        layout = """\
<p:sldLayout
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:cSld name="Process layout"><p:spTree/></p:cSld>
</p:sldLayout>"""
        layout_relationships = """\
<Relationships
 xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rIdMaster"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
  Target="../slideMasters/slideMaster1.xml"/>
</Relationships>"""
        master = """\
<p:sldMaster
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:cSld name="Primary master"><p:spTree/></p:cSld>
</p:sldMaster>"""
        chart = """\
<c:chartSpace
 xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
 <c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>Trend</a:t>
 </a:r></a:p></c:rich></c:tx></c:title>
 <c:plotArea><c:lineChart><c:ser><c:tx><c:v>Flow</c:v></c:tx>
 <c:cat><c:strLit><c:pt><c:v>Jan</c:v></c:pt></c:strLit></c:cat>
 <c:val><c:numLit><c:pt><c:v>4</c:v></c:pt></c:numLit></c:val>
 </c:ser></c:lineChart></c:plotArea></c:chart>
</c:chartSpace>"""
        return {
            "ppt/presentation.xml": presentation,
            "ppt/_rels/presentation.xml.rels": presentation_relationships,
            "ppt/slides/slide1.xml": slide,
            "ppt/slides/_rels/slide1.xml.rels": slide_relationships,
            "ppt/slideLayouts/slideLayout1.xml": layout,
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": (layout_relationships),
            "ppt/slideMasters/slideMaster1.xml": master,
            "ppt/charts/chart1.xml": chart,
            "ppt/media/image1.png": b"\x89PNG\r\n\x1a\n",
        }


@unittest.skipUnless(shutil.which("officecli"), "OfficeCLI is not installed")
class OfficeCliLocatorContractTests(unittest.TestCase):
    def test_every_advertised_real_locator_resolves(self) -> None:
        environment = {
            **os.environ,
            "OFFICECLI_NO_AUTO_RESIDENT": "1",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = {
                root / "locator.docx": DocxIndexer(),
                root / "locator.xlsx": XlsxIndexer(),
                root / "locator.pptx": PptxIndexer(),
            }
            for document in documents:
                self._run(environment, "create", str(document))
            self._run(
                environment,
                "add",
                str(root / "locator.docx"),
                "/body",
                "--type",
                "paragraph",
                "--prop",
                "text=Locator heading",
                "--prop",
                "style=Heading1",
            )
            self._run(
                environment,
                "set",
                str(root / "locator.xlsx"),
                "/Sheet1/A1",
                "--prop",
                "value=Locator cell",
            )
            self._run(
                environment,
                "add",
                str(root / "locator.pptx"),
                "/",
                "--type",
                "slide",
                "--prop",
                "layout=Blank",
            )
            self._run(
                environment,
                "add",
                str(root / "locator.pptx"),
                "/slide[1]",
                "--type",
                "shape",
                "--prop",
                "text=Locator shape",
                "--prop",
                "name=LocatorShape",
                "--prop",
                "x=1cm",
                "--prop",
                "y=1cm",
                "--prop",
                "width=4cm",
                "--prop",
                "height=2cm",
            )

            resolved = 0
            matched_native_ids = 0
            for document, indexer in documents.items():
                indexed = indexer.index(document)
                for node in indexed.nodes:
                    if not node.officecli_resolvable:
                        continue
                    result = self._run(
                        environment,
                        "get",
                        str(document),
                        node.stable_path,
                        "--json",
                    )
                    payload = json.loads(result.stdout)
                    self.assertTrue(payload["success"], node.stable_path)
                    matches = payload["data"]["results"]
                    self.assertGreaterEqual(len(matches), 1)
                    self.assertEqual(
                        matches[0]["path"],
                        node.stable_path,
                    )
                    returned_id = matches[0].get("format", {}).get("id")
                    if returned_id is not None and node.native_key:
                        self.assertEqual(
                            str(returned_id),
                            str(node.native_key),
                        )
                        matched_native_ids += 1
                    resolved += 1
            self.assertGreaterEqual(resolved, 7)
            self.assertGreaterEqual(matched_native_ids, 1)

    def _run(
        self,
        environment: dict[str, str],
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["officecli", *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=45,
        )


class SearchablePdfIndexerTests(unittest.TestCase):
    def test_indexes_every_searchable_page_beyond_first_batch(self) -> None:
        class TextPage:
            def __init__(self, text: str) -> None:
                self.text = text

            def get_text_range(self) -> str:
                return self.text

            def close(self) -> None:
                return None

        class Page:
            def __init__(self, text: str) -> None:
                self.text = text

            def get_textpage(self) -> TextPage:
                return TextPage(self.text)

            def close(self) -> None:
                return None

        class Document:
            def __init__(self, _path: str) -> None:
                self.pages = [
                    Page(
                        (
                            "page with nul \x00 and searchable content"
                            if index == 26
                            else f"searchable page {index}"
                        )
                    )
                    for index in range(1, 31)
                ]

            def __len__(self) -> int:
                return len(self.pages)

            def __getitem__(self, index: int) -> Page:
                return self.pages[index]

            def close(self) -> None:
                return None

        fake_pdfium = type("FakePdfium", (), {"PdfDocument": Document})
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "searchable.pdf"
            document.write_bytes(b"%PDF-1.7\nfixture")
            indexer = PdfIndexer(batch_pages=25)
            with mock.patch.object(
                indexer,
                "_pdfium",
                return_value=fake_pdfium,
            ):
                quick = indexer.quick_inventory(document)
                batches = tuple(indexer.iter_batches(document))
            pages = [
                node
                for batch in batches
                for node in batch.nodes
                if node.kind is NodeKind.PDF_PAGE
            ]
            self.assertEqual(quick.count(NodeKind.PDF_PAGE), 30)
            self.assertEqual(len(pages), 30)
            self.assertEqual(pages[-1].stable_path, "/pdf/page[30]")
            self.assertIn("\x00", pages[25].text)
            self.assertEqual(
                [
                    len(
                        [node for node in batch.nodes if node.kind is NodeKind.PDF_PAGE]
                    )
                    for batch in batches
                    if any(node.kind is NodeKind.PDF_PAGE for node in batch.nodes)
                ],
                [25, 5],
            )
            self.assertTrue(batches[-1].complete)
            self.assertEqual(batches[-1].progress, 1.0)

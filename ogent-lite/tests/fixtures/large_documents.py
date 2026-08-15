from __future__ import annotations

import zipfile
from pathlib import Path


def _write_package(path: Path, parts: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, payload in parts.items():
            package.writestr(
                name,
                payload.encode("utf-8") if isinstance(payload, str) else payload,
            )
    return path


def large_docx(path: Path, *, pages: int = 300) -> Path:
    body: list[str] = []
    for page in range(1, pages + 1):
        body.append(
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            f"<w:r><w:t>Page {page} compliance heading</w:t></w:r></w:p>"
        )
        body.append(
            f"<w:p><w:r><w:t>Searchable evidence for page {page}.</w:t></w:r></w:p>"
        )
        if page % 25 == 0:
            body.append(
                "<w:tbl><w:tr><w:tc><w:p><w:r>"
                f"<w:t>Table result {page}</w:t>"
                "</w:r></w:p></w:tc></w:tr></w:tbl>"
            )
        if page != pages:
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        + "".join(body)
        + "<w:sectPr/></w:body></w:document>"
    )
    styles = """\
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:style w:type="paragraph" w:styleId="Heading1">
  <w:name w:val="heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr>
 </w:style>
</w:styles>"""
    return _write_package(
        path,
        {
            "word/document.xml": document,
            "word/styles.xml": styles,
        },
    )


def _column_name(number: int) -> str:
    value = number
    output = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        output = chr(65 + remainder) + output
    return output


def large_xlsx(
    path: Path,
    *,
    sheets: int = 100,
    rows: int = 50,
    columns: int = 50,
) -> Path:
    sheet_items = "".join(
        f'<sheet name="Sheet{index}" sheetId="{index}" r:id="rId{index}"/>'
        for index in range(1, sheets + 1)
    )
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships"><sheets>{sheet_items}</sheets>'
        "</workbook>"
    )
    relationships = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheets + 1)
    )
    parts: dict[str, str | bytes] = {
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            f'package/2006/relationships">{relationships}</Relationships>'
        ),
    }
    for sheet in range(1, sheets + 1):
        row_items: list[str] = []
        for row in range(1, rows + 1):
            cells = "".join(
                f'<c r="{_column_name(column)}{row}"><v>'
                f"{sheet * 1_000_000 + row * 1000 + column}"
                "</v></c>"
                for column in range(1, columns + 1)
            )
            row_items.append(f'<row r="{row}">{cells}</row>')
        parts[f"xl/worksheets/sheet{sheet}.xml"] = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData>'
            + "".join(row_items)
            + "</sheetData></worksheet>"
        )
    return _write_package(path, parts)


def _shape(identifier: int, name: str, x: int) -> str:
    return f"""\
<p:sp><p:nvSpPr><p:cNvPr id="{identifier}" name="{name}"/>
 <p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>
 <a:xfrm><a:off x="{x}" y="0"/><a:ext cx="100" cy="100"/></a:xfrm>
 <a:prstGeom prst="rect"/></p:spPr>
 <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{name}</a:t>
 </a:r></a:p></p:txBody></p:sp>"""


def _mixed_slide(number: int) -> str:
    extras = ""
    if number % 30 == 0:
        extras = """\
<p:pic><p:nvPicPr><p:cNvPr id="5" name="Evidence photo"/>
 <p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill>
 <a:blip r:embed="rIdImage"/></p:blipFill><p:spPr/></p:pic>
<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="6" name="Trend chart"/>
 <p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>
 <a:graphic><a:graphicData><c:chart r:id="rIdChart"/>
 </a:graphicData></a:graphic></p:graphicFrame>
<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="7" name="Results table"/>
 <p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>
 <a:graphic><a:graphicData><a:tbl><a:tr h="1"><a:tc>
 <a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Result</a:t>
 </a:r></a:p></a:txBody><a:tcPr/></a:tc></a:tr></a:tbl>
 </a:graphicData></a:graphic></p:graphicFrame>"""
    return f"""\
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <p:cSld name="Process {number}"><p:spTree>
 <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>
 </p:nvGrpSpPr><p:grpSpPr/>
 {_shape(2, f"Start {number}", 0)}
 {_shape(3, f"Finish {number}", 200)}
 <p:cxnSp><p:nvCxnSpPr><p:cNvPr id="4" name="Flow"/>
 <p:cNvCxnSpPr><a:stCxn id="2" idx="0"/><a:endCxn id="3" idx="0"/>
 </p:cNvCxnSpPr><p:nvPr/></p:nvCxnSpPr><p:spPr>
 <a:prstGeom prst="straightConnector1"/></p:spPr></p:cxnSp>
 {extras}
 </p:spTree></p:cSld></p:sld>"""


def large_pptx(path: Path, *, slides: int = 300) -> Path:
    identifiers = "".join(
        f'<p:sldId id="{255 + index}" r:id="rId{index}"/>'
        for index in range(1, slides + 1)
    )
    relationships = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/slide" Target="slides/slide{index}.xml"/>'
        for index in range(1, slides + 1)
    )
    parts: dict[str, str | bytes] = {
        "ppt/presentation.xml": (
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/'
            'presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.'
            f'org/officeDocument/2006/relationships"><p:sldIdLst>{identifiers}'
            "</p:sldIdLst></p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            f'2006/relationships">{relationships}</Relationships>'
        ),
        "ppt/charts/chart1.xml": """\
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
 <c:chart><c:plotArea><c:lineChart><c:ser><c:tx><c:v>Flow</c:v></c:tx>
 <c:val><c:numLit><c:pt><c:v>4</c:v></c:pt></c:numLit></c:val>
 </c:ser></c:lineChart></c:plotArea></c:chart></c:chartSpace>""",
        "ppt/media/image1.png": b"\x89PNG\r\n\x1a\n",
    }
    for slide in range(1, slides + 1):
        parts[f"ppt/slides/slide{slide}.xml"] = _mixed_slide(slide)
        if slide % 30 == 0:
            parts[f"ppt/slides/_rels/slide{slide}.xml.rels"] = """\
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rIdImage"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
  Target="../media/image1.png"/>
 <Relationship Id="rIdChart"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
  Target="../charts/chart1.xml"/>
</Relationships>"""
    return _write_package(path, parts)

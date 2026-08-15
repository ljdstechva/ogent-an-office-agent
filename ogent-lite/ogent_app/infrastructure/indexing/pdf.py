"""Searchable PDF text indexer with page-stable locators."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ogent_app.domain.document_intelligence import (
    DocumentFormat,
    DocumentIndex,
    IndexBatch,
    IndexedEdge,
    LocatorStability,
    NodeKind,
    StructuralManifest,
)

from .common import DocumentIndexError, indexed_node, package_sha256


class PdfIndexer:
    format = DocumentFormat.PDF

    def __init__(
        self,
        *,
        max_pages: int = 5_000,
        max_characters_per_page: int = 2_000_000,
        batch_pages: int = 25,
    ) -> None:
        self.max_pages = max(1, int(max_pages))
        self.max_characters_per_page = max(
            1,
            int(max_characters_per_page),
        )
        self.batch_pages = max(1, int(batch_pages))

    @staticmethod
    def _pdfium():
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise DocumentIndexError(
                "Searchable PDF indexing requires pypdfium2."
            ) from exc
        return pdfium

    def quick_inventory(self, path: Path) -> StructuralManifest:
        digest = package_sha256(path)
        pdfium = self._pdfium()
        try:
            document = pdfium.PdfDocument(str(Path(path)))
        except Exception as exc:
            raise DocumentIndexError("The PDF could not be opened.") from exc
        try:
            page_count = len(document)
        finally:
            document.close()
        unsupported = (
            (f"PDF exceeds the configured {self.max_pages} page indexing limit",)
            if page_count > self.max_pages
            else ()
        )
        return StructuralManifest(
            self.format,
            digest,
            {NodeKind.PDF_PAGE.value: page_count},
            tuple(f"/pdf/page[{index}]" for index in range(1, page_count + 1)),
            unsupported,
            quick=True,
        )

    def index(self, path: Path) -> DocumentIndex:
        nodes = []
        edges = []
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
        source = Path(path).expanduser().resolve(strict=True)
        digest = package_sha256(source)
        pdfium = self._pdfium()
        try:
            document = pdfium.PdfDocument(str(source))
        except Exception as exc:
            raise DocumentIndexError("The PDF could not be opened.") from exc
        try:
            page_count = len(document)
            indexed_count = min(page_count, self.max_pages)
            root = indexed_node(
                "/document",
                NodeKind.DOCUMENT,
                title=source.name,
                metadata={
                    "format": self.format.value,
                    "page_count": page_count,
                },
                stability=LocatorStability.SYNTHETIC,
                lineage_key=f"pdf:package:{digest}",
            )
            yield IndexBatch((root,), (), progress=0.01)
            nodes = []
            edges = []
            unsupported: list[str] = []
            for page_index in range(indexed_count):
                page_number = page_index + 1
                page = document[page_index]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = text_page.get_text_range()
                    finally:
                        text_page.close()
                finally:
                    page.close()
                if len(text) > self.max_characters_per_page:
                    text = text[: self.max_characters_per_page]
                    unsupported.append(
                        f"PDF page {page_number} text exceeded the "
                        "configured character limit"
                    )
                if not text.strip():
                    unsupported.append(
                        f"PDF page {page_number} has no searchable text; needs OCR"
                    )
                page_path = f"/pdf/page[{page_number}]"
                nodes.append(
                    indexed_node(
                        page_path,
                        NodeKind.PDF_PAGE,
                        parent_path="/document",
                        title=f"Page {page_number}",
                        text=text,
                        metadata={
                            "searchable": bool(text.strip()),
                            "character_count": len(text),
                        },
                        page_number=page_number,
                        native_key=str(page_number),
                        stability=LocatorStability.NATIVE,
                        lineage_key=f"pdf:page:{page_number}",
                        ordinal=page_number,
                    )
                )
                edges.append(IndexedEdge("/document", page_path, "contains"))
                if len(nodes) >= self.batch_pages or page_number == indexed_count:
                    yield IndexBatch(
                        tuple(nodes),
                        tuple(edges),
                        progress=(page_number / max(1, indexed_count)),
                        unsupported=tuple(unsupported),
                        complete=(
                            page_number == indexed_count
                            and page_count <= self.max_pages
                        ),
                    )
                    nodes = []
                    edges = []
                    unsupported = []
            if page_count > self.max_pages:
                yield IndexBatch(
                    (),
                    (),
                    progress=1.0,
                    unsupported=(
                        f"PDF exceeds the configured {self.max_pages} "
                        "page indexing limit",
                    ),
                    complete=True,
                )
            elif indexed_count == 0:
                yield IndexBatch((), (), progress=1.0, complete=True)
        finally:
            document.close()

"""Format routing and revision-guarded document indexing."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Iterator

from ogent_app.domain.document_intelligence import (
    DocumentFormat,
    DocumentIndex,
    IndexBatch,
    StructuralManifest,
)

from .common import DocumentIndexError, package_sha256
from .docx import DocxIndexer
from .pdf import PdfIndexer
from .pptx import PptxIndexer
from .xlsx import XlsxIndexer


class DocumentRevisionChanged(DocumentIndexError):
    """The package changed while an index was being constructed."""


class DocumentIndexer:
    def __init__(
        self,
        *,
        docx: DocxIndexer | None = None,
        xlsx: XlsxIndexer | None = None,
        pptx: PptxIndexer | None = None,
        pdf: PdfIndexer | None = None,
    ) -> None:
        self.indexers = {
            DocumentFormat.DOCX: docx or DocxIndexer(),
            DocumentFormat.XLSX: xlsx or XlsxIndexer(),
            DocumentFormat.PPTX: pptx or PptxIndexer(),
            DocumentFormat.PDF: pdf or PdfIndexer(),
        }

    def quick_inventory(
        self,
        path: Path,
        *,
        expected_package_sha256: str | None = None,
    ) -> StructuralManifest:
        source = Path(path).expanduser().resolve(strict=True)
        expected = expected_package_sha256 or package_sha256(source)
        self._assert_revision(source, expected)
        manifest = self.indexers[DocumentFormat.from_path(source)].quick_inventory(
            source
        )
        self._assert_revision(source, expected)
        if manifest.package_sha256 != expected:
            raise DocumentRevisionChanged(
                "The document changed during quick inventory."
            )
        return manifest

    def iter_batches(
        self,
        path: Path,
        *,
        expected_package_sha256: str,
    ) -> Iterator[IndexBatch]:
        source = Path(path).expanduser().resolve(strict=True)
        self._assert_revision(source, expected_package_sha256)
        expected_stat = self._stat_token(source)
        indexer = self.indexers[DocumentFormat.from_path(source)]
        if hasattr(indexer, "iter_batches"):
            iterator = indexer.iter_batches(source)
        else:
            index = indexer.index(source)
            iterator = iter(
                (
                    IndexBatch(
                        index.nodes,
                        index.edges,
                        progress=1.0,
                        unsupported=index.unsupported,
                        complete=True,
                    ),
                )
            )
        for batch in iterator:
            if self._stat_token(source) != expected_stat:
                raise DocumentRevisionChanged(
                    "The document changed while its structure was being indexed."
                )
            yield dataclasses.replace(batch, complete=False)
        self._assert_revision(source, expected_package_sha256)
        yield IndexBatch((), (), progress=1.0, complete=True)

    def index(
        self,
        path: Path,
        *,
        expected_package_sha256: str | None = None,
    ) -> DocumentIndex:
        source = Path(path).expanduser().resolve(strict=True)
        expected = expected_package_sha256 or package_sha256(source)
        nodes = []
        edges = []
        unsupported: list[str] = []
        for batch in self.iter_batches(
            source,
            expected_package_sha256=expected,
        ):
            nodes.extend(batch.nodes)
            edges.extend(batch.edges)
            unsupported.extend(batch.unsupported)
        return DocumentIndex(
            DocumentFormat.from_path(source),
            expected,
            tuple(nodes),
            tuple(edges),
            tuple(dict.fromkeys(unsupported)),
        )

    @staticmethod
    def _assert_revision(path: Path, expected: str) -> None:
        if package_sha256(path) != expected:
            raise DocumentRevisionChanged(
                "The document changed while its structure was being indexed."
            )

    @staticmethod
    def _stat_token(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return int(stat.st_size), int(stat.st_mtime_ns)

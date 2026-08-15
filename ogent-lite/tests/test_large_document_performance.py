from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from ogent_app.domain.document_intelligence import NodeKind
from ogent_app.infrastructure.indexing import (
    DocxIndexer,
    PptxIndexer,
    XlsxIndexer,
)
from tests.fixtures.large_documents import (
    large_docx,
    large_pptx,
    large_xlsx,
)


class LargeDocumentPerformanceTests(unittest.TestCase):
    def test_300_page_docx_indexes_with_structural_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = large_docx(Path(temporary) / "large.docx")
            started = time.perf_counter()
            result = DocxIndexer().index(document)
            elapsed = time.perf_counter() - started
        self.assertEqual(
            sum(node.kind is NodeKind.HEADING for node in result.nodes),
            300,
        )
        self.assertEqual(
            sum(node.kind is NodeKind.TABLE for node in result.nodes),
            12,
        )
        self.assertLess(elapsed, 30)

    def test_100_sheet_250000_cell_xlsx_streams_in_bounded_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = large_xlsx(Path(temporary) / "large.xlsx")
            started = time.perf_counter()
            cell_count = 0
            sheet_count = 0
            largest_batch = 0
            for batch in XlsxIndexer().iter_batches(document):
                largest_batch = max(largest_batch, len(batch.nodes))
                cell_count += sum(node.kind is NodeKind.CELL for node in batch.nodes)
                sheet_count += sum(node.kind is NodeKind.SHEET for node in batch.nodes)
            elapsed = time.perf_counter() - started
        self.assertEqual(sheet_count, 100)
        self.assertEqual(cell_count, 250_000)
        self.assertLess(largest_batch, 3_000)
        self.assertLess(elapsed, 60)

    def test_300_slide_mixed_pptx_streams_charts_figures_and_flows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = large_pptx(Path(temporary) / "large.pptx")
            started = time.perf_counter()
            counts = {kind: 0 for kind in NodeKind}
            largest_batch = 0
            for batch in PptxIndexer().iter_batches(document):
                largest_batch = max(largest_batch, len(batch.nodes))
                for node in batch.nodes:
                    counts[node.kind] += 1
            elapsed = time.perf_counter() - started
        self.assertEqual(counts[NodeKind.SLIDE], 300)
        self.assertEqual(counts[NodeKind.CHART], 10)
        self.assertEqual(counts[NodeKind.FIGURE], 10)
        self.assertGreaterEqual(counts[NodeKind.TABLE], 10)
        self.assertGreaterEqual(counts[NodeKind.PROCESS_FLOW], 300)
        self.assertLess(largest_batch, 30)
        self.assertLess(elapsed, 30)


if __name__ == "__main__":
    unittest.main()

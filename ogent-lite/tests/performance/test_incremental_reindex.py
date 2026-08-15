from __future__ import annotations

import math
import os
import statistics
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from ogent_app.application.document_intelligence import (
    DocumentIntelligenceCoordinator,
)
from ogent_app.domain.document_intelligence import DocumentFormat
from ogent_app.infrastructure.indexing import DocumentIndexer
from ogent_app.infrastructure.indexing.common import package_sha256
from ogent_app.infrastructure.sqlite import (
    ContentAddressedBlobStore,
    DocumentRepository,
    SqliteDatabase,
    WorkspaceRepository,
)


def _revision_docx(path: Path, *, changed_text: str) -> Path:
    paragraphs = []
    for index in range(1, 121):
        text = changed_text if index == 17 else f"Stable paragraph {index}"
        paragraphs.append(
            f'<w:p w14:paraId="{index:08X}"><w:r><w:t>{text}</w:t></w:r></w:p>'
        )
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
        "<w:body>" + "".join(paragraphs) + "<w:sectPr/></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("word/document.xml", document.encode("utf-8"))
    return path


class IncrementalReindexPerformanceTests(unittest.TestCase):
    def test_normal_docx_edits_materialize_only_changed_nodes_under_one_second(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SqliteDatabase(root / "ogent.db")
            blobs = ContentAddressedBlobStore(root / "blobs")
            WorkspaceRepository(database).create("workspace-performance")
            documents = DocumentRepository(database, blobs)
            indexer = DocumentIndexer()
            coordinator = DocumentIntelligenceCoordinator(documents, indexer)
            document = root / "normal-edit.docx"
            latencies: list[float] = []
            try:
                self._index_revision(
                    coordinator,
                    document,
                    changed_text="Initial changed paragraph",
                )
                for revision in range(1, 21):
                    started = time.perf_counter()
                    observed = self._index_revision(
                        coordinator,
                        document,
                        changed_text=f"Edited paragraph revision {revision}",
                    )
                    latencies.append(time.perf_counter() - started)
                    with database.reader() as connection:
                        materialized_paths = {
                            str(row["stable_path"])
                            for row in connection.execute(
                                "SELECT DISTINCT node.stable_path "
                                "FROM document_chunks AS chunk "
                                "JOIN document_nodes AS node "
                                "ON node.id = chunk.node_id "
                                "WHERE chunk.revision_id = ?",
                                (observed.revision.revision_id,),
                            )
                        }
                    delta = documents.delta(observed.revision.revision_id)
                    self.assertEqual(
                        materialized_paths,
                        {
                            "/body/p[@paraId=00000011]",
                            "/body/p[@paraId=00000011]/r[1]",
                        },
                    )
                    self.assertTrue(materialized_paths.isdisjoint(delta.reused_paths))
            finally:
                coordinator.stop()

        percentile_index = max(0, math.ceil(0.95 * len(latencies)) - 1)
        p95 = sorted(latencies)[percentile_index]
        budget_seconds = float(
            os.environ.get("OGENT_REINDEX_P95_BUDGET_SECONDS", "1.0")
        )
        self.assertLess(
            p95,
            budget_seconds,
            f"small-revision p95 was {p95:.3f}s; "
            f"median was {statistics.median(latencies):.3f}s",
        )

    @staticmethod
    def _index_revision(
        coordinator: DocumentIntelligenceCoordinator,
        document: Path,
        *,
        changed_text: str,
    ):
        _revision_docx(document, changed_text=changed_text)
        digest = package_sha256(document)
        quick = coordinator.indexer.quick_inventory(
            document,
            expected_package_sha256=digest,
        )
        observed = coordinator.repository.observe(
            workspace_id="workspace-performance",
            source_path=document,
            active_path=document,
            mode="local_direct",
            document_format=DocumentFormat.DOCX,
            package_sha256=digest,
            quick_manifest=quick,
        )
        assert observed.attempt_id is not None
        coordinator._index_revision(  # noqa: SLF001 - deterministic perf seam
            "workspace-performance",
            document,
            observed.revision.revision_id,
            digest,
            observed.attempt_id,
            quick,
        )
        return observed


if __name__ == "__main__":
    unittest.main()

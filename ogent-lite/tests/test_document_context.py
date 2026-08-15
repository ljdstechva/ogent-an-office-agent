from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from ogent_app.application.document_context import (
    DocumentContextService,
    ProviderContextBudget,
)
from ogent_app.application.document_intelligence import DocumentIndexNotReady
from ogent_app.application.run_planner import RunPlanner
from ogent_app.domain.document_intelligence import (
    DocumentFormat,
    IndexBatch,
    IndexedEdge,
    LocatorNamespace,
    LocatorStability,
    NodeKind,
    StructuralManifest,
)
from ogent_app.domain.run import RunContract, RunMode, ScopeMode
from ogent_app.infrastructure.indexing.common import indexed_node
from ogent_app.infrastructure.sqlite import (
    ContentAddressedBlobStore,
    CoverageRepository,
    DocumentRepository,
    RunRepository,
    SqliteDatabase,
    TurnRepository,
    WorkspaceRepository,
)


class DocumentContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = SqliteDatabase(self.root / "ogent.db")
        self.blobs = ContentAddressedBlobStore(self.root / "blobs")
        self.workspaces = WorkspaceRepository(self.database)
        self.workspaces.create("workspace-context")
        self.turns = TurnRepository(self.database, self.blobs)
        self.runs = RunRepository(self.database)
        self.documents = DocumentRepository(self.database, self.blobs)
        self.coverage = CoverageRepository(self.database)
        self.service = DocumentContextService(
            self.documents,
            self.coverage,
        )
        self.planner = RunPlanner()
        self.document = self.root / "active.docx"
        self.document.write_bytes(b"context-revision")
        digest = hashlib.sha256(self.document.read_bytes()).hexdigest()
        self.observed = self.documents.observe(
            workspace_id="workspace-context",
            source_path=self.document,
            active_path=self.document,
            mode="local_direct",
            document_format=DocumentFormat.DOCX,
            package_sha256=digest,
            quick_manifest=StructuralManifest(
                DocumentFormat.DOCX,
                digest,
                {"heading": 1, "table": 1, "figure": 1},
                (
                    "/body/p[1]",
                    "/body/tbl[1]",
                    "/body/figure[1]",
                ),
                quick=True,
            ),
        )
        assert self.observed.attempt_id is not None
        self.nodes = (
            indexed_node(
                "/document",
                NodeKind.DOCUMENT,
                title="active.docx",
                stability=LocatorStability.SYNTHETIC,
                lineage_key="document",
            ),
            indexed_node(
                "/body/p[1]",
                NodeKind.HEADING,
                parent_path="/document",
                title="Emissions",
                text="😀 " + ("emissions evidence " * 2_000),
                namespace=LocatorNamespace.OFFICECLI,
                resolvable=True,
            ),
            indexed_node(
                "/body/tbl[1]",
                NodeKind.TABLE,
                parent_path="/document",
                title="Monitoring results",
                text="Parameter Result Unit\nBOD 12 mg/L",
                namespace=LocatorNamespace.OFFICECLI,
                resolvable=True,
            ),
            indexed_node(
                "/body/figure[1]",
                NodeKind.FIGURE,
                parent_path="/document",
                title="Process diagram",
                text="Wastewater treatment process",
                namespace=LocatorNamespace.OFFICECLI,
                resolvable=True,
            ),
        )
        self.documents.append_batch(
            self.observed.revision.revision_id,
            self.observed.attempt_id,
            IndexBatch(
                self.nodes,
                (
                    IndexedEdge("/document", "/body/p[1]", "contains"),
                    IndexedEdge("/document", "/body/tbl[1]", "contains"),
                    IndexedEdge(
                        "/body/p[1]",
                        "/body/tbl[1]",
                        "references",
                    ),
                ),
                progress=0.8,
            ),
        )
        self.budget = ProviderContextBudget.conservative(
            "codex",
            "fixture",
            partial_text_deltas=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run_id(self, plan) -> str:
        turn = self.turns.append(
            "workspace-context",
            "user",
            plan.goal,
        )
        return self.runs.create(
            "workspace-context",
            turn.turn_id,
            mode=plan.mode,
            scope=plan.scope,
            plan=plan,
        ).run_id

    def _finish_index(self) -> None:
        self.documents.finish(
            self.observed.revision.revision_id,
            self.observed.attempt_id,
            manifest=StructuralManifest(
                DocumentFormat.DOCX,
                self.observed.revision.package_sha256,
                {
                    "document": 1,
                    "heading": 1,
                    "table": 1,
                    "figure": 1,
                },
                (
                    "/body/p[1]",
                    "/body/tbl[1]",
                    "/body/figure[1]",
                ),
                quick=False,
            ),
        )

    def test_selected_scope_uses_partial_index_and_related_nodes(self) -> None:
        node_ids = self.documents.resolve_node_ids(
            self.observed.revision.revision_id,
            ("/body/p[1]",),
        )
        contract = RunContract(
            RunMode.ANALYZE,
            ScopeMode.SELECTED_ONLY,
            ("/body/p[1]",),
        )
        plan = self.planner.build(
            "Analyze the selected emissions section.",
            contract,
            target_node_ids=node_ids,
            has_document=True,
        )
        projection = self.service.retrieve(
            revision_id=self.observed.revision.revision_id,
            request=plan.goal,
            plan=plan,
            budget=self.budget,
            run_id=self._run_id(plan),
            fixed_prompt_characters=200_000,
        )

        self.assertLessEqual(
            projection.character_count,
            projection.character_budget,
        )
        self.assertEqual(projection.character_budget, 4_000)
        self.assertIn("/body/p[1]", projection.included_paths)
        self.assertIn("/body/tbl[1]", projection.included_paths)
        self.assertIn("Unicode character boundary", projection.text)
        self.assertNotIn("\ufffd", projection.text)

    def test_whole_document_requires_complete_current_index(self) -> None:
        contract = RunContract(
            RunMode.REVIEW,
            ScopeMode.WHOLE_DOCUMENT,
        )
        plan = self.planner.build(
            "Review the whole document.",
            contract,
            has_document=True,
        )
        run_id = self._run_id(plan)
        with self.assertRaisesRegex(
            DocumentIndexNotReady,
            "requires.*finish",
        ):
            self.service.retrieve(
                revision_id=self.observed.revision.revision_id,
                request=plan.goal,
                plan=plan,
                budget=self.budget,
                run_id=run_id,
            )

        self._finish_index()
        projection = self.service.retrieve(
            revision_id=self.observed.revision.revision_id,
            request=plan.goal,
            plan=plan,
            budget=self.budget,
            run_id=run_id,
        )
        ledger = self.coverage.get(run_id)

        self.assertEqual(projection.index_status.value, "complete")
        self.assertEqual(len(projection.partitions), 1)
        self.assertIsNotNone(ledger)
        assert ledger is not None
        self.assertEqual(ledger.totals["tables"], 1)
        self.assertEqual(ledger.totals["figures"], 1)
        self.assertEqual(ledger.reviewed_counts["headings"], 0)
        partition = self.service.retrieve_partition(
            revision_id=self.observed.revision.revision_id,
            stable_paths=projection.partitions[0],
            plan=plan,
            budget=self.budget,
            run_id=run_id,
            partition_index=1,
            partition_count=1,
        )
        self.assertLessEqual(
            partition.character_count,
            partition.character_budget,
        )
        self.service.mark_partition_reviewed(
            run_id=run_id,
            revision_id=self.observed.revision.revision_id,
            stable_paths=projection.partitions[0],
        )
        ledger = self.coverage.get(run_id)
        assert ledger is not None
        self.assertEqual(ledger.reviewed_counts["headings"], 1)
        self.assertEqual(ledger.reviewed_counts["tables"], 1)
        self.assertEqual(ledger.reviewed_counts["figures"], 1)
        self.assertTrue(ledger.structurally_complete)
        self.assertFalse(ledger.complete)


if __name__ == "__main__":
    unittest.main()

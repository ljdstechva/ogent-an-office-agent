from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ogent_app.domain.document_intelligence import (
    CoverageLedger,
    DocumentFormat,
    IndexBatch,
    IndexStatus,
    LocatorNamespace,
    LocatorStability,
    NodeKind,
    StructuralManifest,
)
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
from ogent_app.infrastructure.sqlite import connection as connection_module
from ogent_app.infrastructure.sqlite.migrations import MIGRATIONS


class DocumentPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = SqliteDatabase(self.root / "ogent.db")
        self.blobs = ContentAddressedBlobStore(self.root / "blobs")
        self.workspaces = WorkspaceRepository(self.database)
        self.workspaces.create("workspace-index")
        self.documents = DocumentRepository(self.database, self.blobs)
        self.document = self.root / "active.docx"
        self.document.write_bytes(b"revision-one")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def observe(self):
        digest = hashlib.sha256(self.document.read_bytes()).hexdigest()
        quick = StructuralManifest(
            DocumentFormat.DOCX,
            digest,
            {"heading": 1},
            ("/body/p[@paraId=AAA]",),
            quick=True,
        )
        return self.documents.observe(
            workspace_id="workspace-index",
            source_path=self.document,
            active_path=self.document,
            mode="local_direct",
            document_format=DocumentFormat.DOCX,
            package_sha256=digest,
            quick_manifest=quick,
        )

    @staticmethod
    def nodes(*, changed: bool = False):
        return (
            indexed_node(
                "/document",
                NodeKind.DOCUMENT,
                title="active.docx",
                stability=LocatorStability.SYNTHETIC,
                lineage_key="document",
            ),
            indexed_node(
                "/body/p[@paraId=AAA]",
                NodeKind.HEADING,
                parent_path="/document",
                title="Changed" if changed else "Introduction",
                text="Changed text" if changed else "Environmental report",
                native_key="AAA",
                stability=LocatorStability.NATIVE,
                lineage_key="docx:paragraph:AAA",
                namespace=LocatorNamespace.OFFICECLI,
                resolvable=True,
            ),
            indexed_node(
                "/body/p[@paraId=BBB]",
                NodeKind.PARAGRAPH,
                parent_path="/document",
                text="Unchanged",
                native_key="BBB",
                stability=LocatorStability.NATIVE,
                lineage_key="docx:paragraph:BBB",
                namespace=LocatorNamespace.OFFICECLI,
                resolvable=True,
            ),
        )

    def persist(self, observed, *, changed: bool = False) -> None:
        assert observed.attempt_id is not None
        nodes = self.nodes(changed=changed)
        edges = (
            self._edge("/document", nodes[1].stable_path),
            self._edge("/document", nodes[2].stable_path),
        )
        self.assertTrue(
            self.documents.append_batch(
                observed.revision.revision_id,
                observed.attempt_id,
                IndexBatch(nodes, edges, progress=1.0),
            )
        )
        manifest = StructuralManifest(
            DocumentFormat.DOCX,
            observed.revision.package_sha256,
            {
                NodeKind.DOCUMENT.value: 1,
                NodeKind.HEADING.value: 1,
                NodeKind.PARAGRAPH.value: 1,
            },
            (nodes[1].stable_path,),
            quick=False,
        )
        self.assertTrue(
            self.documents.finish(
                observed.revision.revision_id,
                observed.attempt_id,
                manifest=manifest,
            )
        )

    @staticmethod
    def _edge(source: str, target: str):
        from ogent_app.domain.document_intelligence import IndexedEdge

        return IndexedEdge(source, target, "contains")

    def test_current_revision_fts_and_incremental_delta(self) -> None:
        first = self.observe()
        self.persist(first)
        hits = self.documents.search(
            first.revision.revision_id,
            "environmental report",
        )
        self.assertEqual([hit.stable_path for hit in hits], ["/body/p[@paraId=AAA]"])

        self.document.write_bytes(b"revision-two")
        second = self.observe()
        with mock.patch.object(
            self.blobs,
            "put_text",
            wraps=self.blobs.put_text,
        ) as put_text:
            self.persist(second, changed=True)
        self.assertEqual(put_text.call_count, 1)
        self.assertEqual(second.revision.revision_number, 2)
        self.assertEqual(
            self.documents.search(
                first.revision.revision_id,
                "environmental report",
            ),
            (),
        )
        delta = self.documents.delta(second.revision.revision_id)
        self.assertIn("/body/p[@paraId=AAA]", delta.changed_paths)
        self.assertIn("/body/p[@paraId=BBB]", delta.reused_paths)
        self.assertEqual(delta.removed_paths, ())
        inherited_hits = self.documents.search(
            second.revision.revision_id,
            "Unchanged",
        )
        self.assertEqual(
            [hit.stable_path for hit in inherited_hits],
            ["/body/p[@paraId=BBB]"],
        )
        with self.database.reader() as connection:
            materialized_paths = {
                str(row["stable_path"])
                for row in connection.execute(
                    "SELECT node.stable_path FROM document_chunks AS chunk "
                    "JOIN document_nodes AS node ON node.id = chunk.node_id "
                    "WHERE chunk.revision_id = ?",
                    (second.revision.revision_id,),
                )
            }
            inherited = connection.execute(
                "SELECT current.text_blob_id = parent.text_blob_id AS reused "
                "FROM document_nodes AS current "
                "JOIN document_nodes AS parent "
                "ON parent.id = current.origin_node_id "
                "WHERE current.revision_id = ? "
                "AND current.stable_path = ?",
                (second.revision.revision_id, "/body/p[@paraId=BBB]"),
            ).fetchone()
        self.assertEqual(materialized_paths, {"/body/p[@paraId=AAA]"})
        self.assertIsNotNone(inherited)
        self.assertEqual(int(inherited["reused"]), 1)

    def test_attempt_compare_and_swap_rejects_superseded_worker(self) -> None:
        observed = self.observe()
        attempt_a = observed.attempt_id
        assert attempt_a is not None
        attempt_b = self.documents.requeue(observed.revision.revision_id)
        batch = IndexBatch((self.nodes()[0],), progress=0.5)

        self.assertFalse(
            self.documents.append_batch(
                observed.revision.revision_id,
                attempt_a,
                batch,
            )
        )
        self.assertTrue(
            self.documents.append_batch(
                observed.revision.revision_id,
                attempt_b,
                batch,
            )
        )
        self.assertFalse(
            self.documents.finish(
                observed.revision.revision_id,
                attempt_a,
                manifest=StructuralManifest(
                    DocumentFormat.DOCX,
                    observed.revision.package_sha256,
                    {},
                    (),
                    quick=False,
                ),
            )
        )
        job = self.documents.job(observed.revision.revision_id)
        assert job is not None
        self.assertEqual(job.attempt_id, attempt_b)
        self.assertEqual(job.status, IndexStatus.INDEXING)

    def test_repeated_current_hash_deduplicates_revision(self) -> None:
        first = self.observe()
        second = self.observe()
        self.assertTrue(second.deduplicated)
        self.assertEqual(
            first.revision.revision_id,
            second.revision.revision_id,
        )

    def test_fts_strips_nul_but_canonical_node_text_is_lossless(self) -> None:
        observed = self.observe()
        assert observed.attempt_id is not None
        root = self.nodes()[0]
        paragraph = indexed_node(
            "/body/p[@paraId=NUL]",
            NodeKind.PARAGRAPH,
            parent_path="/document",
            text="Alpha\x00Beta",
            native_key="NUL",
            stability=LocatorStability.NATIVE,
            lineage_key="docx:paragraph:NUL",
            namespace=LocatorNamespace.OFFICECLI,
            resolvable=True,
        )
        self.assertTrue(
            self.documents.append_batch(
                observed.revision.revision_id,
                observed.attempt_id,
                IndexBatch(
                    (root, paragraph),
                    (self._edge("/document", paragraph.stable_path),),
                    progress=1.0,
                ),
            )
        )
        self.assertTrue(
            self.documents.finish(
                observed.revision.revision_id,
                observed.attempt_id,
                manifest=StructuralManifest(
                    DocumentFormat.DOCX,
                    observed.revision.package_sha256,
                    {
                        NodeKind.DOCUMENT.value: 1,
                        NodeKind.PARAGRAPH.value: 1,
                    },
                    (),
                    quick=False,
                ),
            )
        )
        self.assertEqual(
            self.documents.search(
                observed.revision.revision_id,
                "AlphaBeta",
            )[0].stable_path,
            paragraph.stable_path,
        )
        stored = self.documents.nodes(
            observed.revision.revision_id,
            include_text=True,
        )
        restored = next(
            item.node
            for item in stored
            if item.node.stable_path == paragraph.stable_path
        )
        self.assertEqual(restored.text, "Alpha\x00Beta")

    def test_coverage_is_set_based_and_requires_visuals(self) -> None:
        observed = self.observe()
        turn_repository = TurnRepository(self.database, self.blobs)
        turn = turn_repository.append(
            "workspace-index",
            "user",
            "Review everything.",
        )
        run = RunRepository(self.database).create(
            "workspace-index",
            turn.turn_id,
            mode=self._run_mode(),
            scope=self._scope_mode(),
        )
        nodes = (
            indexed_node("/heading", NodeKind.HEADING),
            indexed_node("/figure", NodeKind.FIGURE),
        )
        ledger = CoverageLedger.from_nodes(
            observed.revision.revision_id,
            nodes,
        )
        repeated = ledger.mark_reviewed((nodes[0],)).mark_reviewed((nodes[0],))
        self.assertEqual(repeated.reviewed_counts["headings"], 1)
        self.assertFalse(repeated.complete)
        structurally_complete = repeated.mark_reviewed((nodes[1],))
        self.assertTrue(structurally_complete.structurally_complete)
        self.assertFalse(structurally_complete.complete)
        complete = structurally_complete.mark_reviewed(
            (),
            visual_paths=("/figure",),
        )
        self.assertTrue(complete.complete)

        repository = CoverageRepository(self.database)
        repository.save(run.run_id, complete)
        restored = repository.get(run.run_id)
        self.assertEqual(restored, complete)

    @staticmethod
    def _run_mode():
        from ogent_app.domain.run import RunMode

        return RunMode.REVIEW

    @staticmethod
    def _scope_mode():
        from ogent_app.domain.run import ScopeMode

        return ScopeMode.WHOLE_DOCUMENT


class MigrationAtomicityTests(unittest.TestCase):
    def test_failed_migration_rolls_back_all_ddl_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "atomic.db"
            failing = (
                (
                    1,
                    "CREATE TABLE stable(id INTEGER PRIMARY KEY);",
                ),
                (
                    2,
                    "CREATE TABLE leaked(id INTEGER);THIS IS INVALID SQL;",
                ),
            )
            with (
                mock.patch.object(
                    connection_module,
                    "MIGRATIONS",
                    failing,
                ),
                mock.patch.object(connection_module, "SCHEMA_VERSION", 2),
            ):
                with self.assertRaises(sqlite3.DatabaseError):
                    SqliteDatabase(path).initialize()
            connection = sqlite3.connect(path)
            try:
                names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
            finally:
                connection.close()
            self.assertIn("stable", names)
            self.assertNotIn("leaked", names)
            self.assertEqual(versions, [1])

            corrected = (
                failing[0],
                (2, "CREATE TABLE recovered(id INTEGER PRIMARY KEY);"),
            )
            with (
                mock.patch.object(
                    connection_module,
                    "MIGRATIONS",
                    corrected,
                ),
                mock.patch.object(connection_module, "SCHEMA_VERSION", 2),
            ):
                SqliteDatabase(path).initialize()
            connection = sqlite3.connect(path)
            try:
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='recovered'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_v3_to_current_and_revision_ownership_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "upgrade.db"
            with (
                mock.patch.object(
                    connection_module,
                    "MIGRATIONS",
                    MIGRATIONS[:3],
                ),
                mock.patch.object(connection_module, "SCHEMA_VERSION", 3),
            ):
                SqliteDatabase(path).initialize()
            SqliteDatabase(path).initialize()
            connection = sqlite3.connect(path)
            try:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(document_nodes)")
                }
                self.assertIn("locator_namespace", columns)
                self.assertIn("lineage_key", columns)
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    )
                ]
                self.assertEqual(
                    versions,
                    list(
                        range(
                            1,
                            connection_module.SCHEMA_VERSION + 1,
                        )
                    ),
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()

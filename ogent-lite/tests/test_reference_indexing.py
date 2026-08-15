from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from ogent_app.application.reference_indexing import (
    ReferenceIndexCoordinator,
)
from ogent_app.domain.reference_index import ReferenceIndexStatus
from ogent_app.infrastructure.sqlite import (
    ContentAddressedBlobStore,
    ReferenceIndexRepository,
    SqliteDatabase,
    WorkspaceRepository,
)


class ReferenceIndexingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = SqliteDatabase(self.root / "ogent.db")
        self.blobs = ContentAddressedBlobStore(self.root / "blobs")
        WorkspaceRepository(self.database).create("workspace-reference")
        self.repository = ReferenceIndexRepository(
            self.database,
            self.blobs,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lossless_canonical_text_and_fts_chunks_are_separate(self) -> None:
        raw = "Monitoring\x00 results " + ("BOD 12 mg/L\n" * 1_000)
        self.repository.begin(
            workspace_id="workspace-reference",
            attachment_id="attachment-1",
            original_name="results.txt",
            source_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )
        record = self.repository.complete("attachment-1", raw)
        hits = self.repository.search(
            "workspace-reference",
            ("attachment-1",),
            "BOD",
        )

        self.assertEqual(record.status, ReferenceIndexStatus.COMPLETE)
        self.assertGreater(record.chunk_count, 1)
        self.assertEqual(self.repository.read_text("attachment-1"), raw)
        self.assertTrue(hits)
        self.assertNotIn("\x00", hits[0].text)

    def test_bounded_pool_and_cancellation_ownership(self) -> None:
        coordinator = ReferenceIndexCoordinator(max_workers=1)
        first_started = threading.Event()
        first_release = threading.Event()
        second_started = threading.Event()
        cancellable_started = threading.Event()
        cancelled = threading.Event()
        cleanup_job_started = threading.Event()
        cleanup_started = threading.Event()
        cleanup_release = threading.Event()

        def first_job(cancellation: threading.Event) -> None:
            first_started.set()
            first_release.wait(timeout=5)
            self.assertFalse(cancellation.is_set())

        def second_job(cancellation: threading.Event) -> None:
            self.assertFalse(cancellation.is_set())
            second_started.set()

        def cancellable_job(cancellation: threading.Event) -> None:
            cancellable_started.set()
            cancellation.wait(timeout=5)
            if cancellation.is_set():
                cancelled.set()

        def cleanup_job(cancellation: threading.Event) -> None:
            cleanup_job_started.set()
            cancellation.wait(timeout=5)
            cleanup_started.set()
            cleanup_release.wait(timeout=5)

        try:
            self.assertTrue(coordinator.schedule("workspace", "first", first_job))
            self.assertTrue(coordinator.schedule("workspace", "second", second_job))
            self.assertTrue(first_started.wait(timeout=1))
            self.assertFalse(second_started.is_set())
            first_release.set()
            self.assertTrue(coordinator.wait("workspace", "first", timeout=2))
            self.assertTrue(coordinator.wait("workspace", "second", timeout=2))
            self.assertTrue(second_started.is_set())

            self.assertTrue(
                coordinator.schedule(
                    "workspace",
                    "cancelled",
                    cancellable_job,
                )
            )
            self.assertTrue(cancellable_started.wait(timeout=1))
            self.assertTrue(coordinator.cancel("workspace", "cancelled"))
            self.assertTrue(cancelled.wait(timeout=2))

            self.assertTrue(
                coordinator.schedule(
                    "workspace",
                    "cleanup",
                    cleanup_job,
                )
            )
            self.assertTrue(cleanup_job_started.wait(timeout=2))
            self.assertTrue(coordinator.cancel_workspace("workspace"))
            self.assertTrue(cleanup_started.wait(timeout=2))
            self.assertFalse(coordinator.wait_workspace("workspace", timeout=0.01))
            cleanup_release.set()
            self.assertTrue(coordinator.wait_workspace("workspace", timeout=2))
        finally:
            first_release.set()
            cleanup_release.set()
            coordinator.stop()


if __name__ == "__main__":
    unittest.main()

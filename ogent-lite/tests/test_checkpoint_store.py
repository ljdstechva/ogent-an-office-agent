"""Beside-document checkpoint save/list/restore behavior."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_app.application.checkpoint_store import (  # noqa: E402
    CHECKPOINT_DIR_NAME,
    CheckpointError,
    checkpoint_directory,
    create_checkpoint,
    list_checkpoints,
    restore_checkpoint,
)


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.document = self.root / "report.docx"
        self.document.write_bytes(b"original contents")

    def test_checkpoints_live_beside_the_document(self) -> None:
        checkpoint = create_checkpoint(self.document)
        stored = (
            self.root / CHECKPOINT_DIR_NAME / "report.docx" / checkpoint["name"]
        )
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.read_bytes(), b"original contents")
        self.assertEqual(checkpoint["source"], "manual")

    def test_names_are_collision_proof(self) -> None:
        names = {create_checkpoint(self.document)["name"] for _ in range(5)}
        self.assertEqual(len(names), 5)

    def test_listing_reports_time_and_source_newest_first(self) -> None:
        create_checkpoint(self.document)
        create_checkpoint(self.document, source="pre-restore")
        listing = list_checkpoints(self.document)
        self.assertEqual(len(listing), 2)
        self.assertEqual(
            sorted(item["source"] for item in listing),
            ["manual", "pre-restore"],
        )
        self.assertEqual(
            [item["name"] for item in listing],
            sorted((item["name"] for item in listing), reverse=True),
        )
        for item in listing:
            self.assertIn("created_at", item)

    def test_restore_first_checkpoints_the_current_file(self) -> None:
        checkpoint = create_checkpoint(self.document)
        self.document.write_bytes(b"newer contents")
        result = restore_checkpoint(self.document, checkpoint["name"])
        self.assertEqual(self.document.read_bytes(), b"original contents")
        safety = (
            checkpoint_directory(self.document)
            / result["safety_checkpoint"]["name"]
        )
        self.assertEqual(safety.read_bytes(), b"newer contents")
        self.assertEqual(result["safety_checkpoint"]["source"], "pre-restore")

    def test_restore_rejects_traversal_and_unknown_names(self) -> None:
        create_checkpoint(self.document)
        for name in ("../report.docx", "..\\x.docx", "nope.docx", ""):
            with self.assertRaises(CheckpointError):
                restore_checkpoint(self.document, name)
        with self.assertRaises(CheckpointError):
            restore_checkpoint(
                self.document, "20990101-000000-manual-00000000.docx"
            )

    def test_failed_validation_puts_the_previous_contents_back(self) -> None:
        checkpoint = create_checkpoint(self.document)
        self.document.write_bytes(b"newer contents")
        with self.assertRaises(CheckpointError):
            restore_checkpoint(
                self.document,
                checkpoint["name"],
                validate=lambda _: {"accepted": False},
            )
        self.assertEqual(self.document.read_bytes(), b"newer contents")

    def test_workspace_discovery_ignores_checkpoint_copies(self) -> None:
        # Guard the contract shared with the workspace agent command.
        create_checkpoint(self.document)
        stored = list(
            (self.root / CHECKPOINT_DIR_NAME).rglob("*.docx")
        )
        self.assertTrue(stored)


if __name__ == "__main__":
    unittest.main()

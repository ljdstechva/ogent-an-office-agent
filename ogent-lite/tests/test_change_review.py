from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

from ogent_app.application import (
    ChangeReviewError,
    ChangeReviewService,
    RollbackManager,
)
from ogent_app.domain.run import RunMode, ScopeMode
from ogent_app.infrastructure.sqlite import (
    ChangesetRepository,
    ContentAddressedBlobStore,
    DocumentRepository,
    RunRepository,
    SqliteDatabase,
    TurnRepository,
    WorkspaceRepository,
)


class ChangeReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = SqliteDatabase(self.root / "ogent.db")
        self.blobs = ContentAddressedBlobStore(self.root / "blobs")
        self.workspace_id = uuid.uuid4().hex
        self.workspaces = WorkspaceRepository(self.database)
        self.turns = TurnRepository(self.database, self.blobs)
        self.runs = RunRepository(self.database)
        self.changesets = ChangesetRepository(self.database)
        self.documents = DocumentRepository(self.database, self.blobs)
        self.rollback = RollbackManager(
            self.database,
            self.blobs,
            self.changesets,
        )
        self.service = ChangeReviewService(
            self.changesets,
            self.documents,
            self.rollback,
        )
        self.workspaces.create(self.workspace_id)
        self.document = self.root / "active.docx"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _record_change(self) -> tuple[str, bytes, bytes]:
        before = b"verified-before-package"
        after = b"verified-after-package"
        self.document.write_bytes(before)
        turn = self.turns.append(
            self.workspace_id,
            "user",
            "Update the selected paragraph.",
        )
        run = self.runs.create(
            self.workspace_id,
            turn.turn_id,
            mode=RunMode.EDIT,
            scope=ScopeMode.SELECTED_ONLY,
        )
        snapshot = self.rollback.create(
            self.document,
            run_id=run.run_id,
        )
        self.document.write_bytes(after)
        changeset_id = self.rollback.record_changeset(
            snapshot,
            post_revision_sha256=hashlib.sha256(after).hexdigest(),
            affected_paths=("/body/p[1]",),
            assertions={
                "completion_kind": "edit_completed",
                "officecli_validate": True,
            },
        )
        return changeset_id, before, after

    def test_undo_restores_exact_package_and_is_single_use(self) -> None:
        changeset_id, before, _after = self._record_change()
        observed_during_validation: list[bytes] = []

        def validate(path: Path) -> dict[str, object]:
            observed_during_validation.append(path.read_bytes())
            return {
                "accepted": True,
                "observed_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

        result = self.service.undo(
            self.workspace_id,
            changeset_id,
            document_id=uuid.uuid4().hex,
            document=self.document,
            validate=validate,
        )

        self.assertEqual(self.document.read_bytes(), before)
        self.assertTrue(result["undone"])
        self.assertFalse(result["can_undo"])
        record = self.changesets.get(changeset_id)
        assert record is not None
        self.assertTrue(record.undone)
        self.assertEqual(
            record.restored_sha256,
            hashlib.sha256(before).hexdigest(),
        )
        with self.assertRaisesRegex(
            ChangeReviewError,
            "already undone",
        ):
            self.service.undo(
                self.workspace_id,
                changeset_id,
                document_id=uuid.uuid4().hex,
                document=self.document,
                validate=lambda _path: {"accepted": True},
            )
        self.assertEqual(observed_during_validation, [before])

    def test_validation_failure_restores_post_run_safety_snapshot(self) -> None:
        changeset_id, _before, after = self._record_change()

        with self.assertRaisesRegex(
            ChangeReviewError,
            "did not pass OfficeCLI validation",
        ):
            self.service.undo(
                self.workspace_id,
                changeset_id,
                document_id=uuid.uuid4().hex,
                document=self.document,
                validate=lambda _path: {"accepted": False},
            )

        self.assertEqual(self.document.read_bytes(), after)
        record = self.changesets.get(changeset_id)
        assert record is not None
        self.assertFalse(record.undone)

    def test_newer_external_change_blocks_undo_without_touching_file(self) -> None:
        changeset_id, _before, _after = self._record_change()
        newer = b"newer-user-authored-package"
        self.document.write_bytes(newer)

        with self.assertRaisesRegex(
            ChangeReviewError,
            "changed after this run",
        ):
            self.service.undo(
                self.workspace_id,
                changeset_id,
                document_id=uuid.uuid4().hex,
                document=self.document,
                validate=lambda _path: {"accepted": True},
            )

        self.assertEqual(self.document.read_bytes(), newer)
        review = self.service.review(
            self.workspace_id,
            document_id=uuid.uuid4().hex,
            document=self.document,
        )
        self.assertFalse(review["can_undo"])
        self.assertIn("protect newer work", review["undo_reason"])


if __name__ == "__main__":
    unittest.main()

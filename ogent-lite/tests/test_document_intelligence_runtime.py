from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ogent


@unittest.skipUnless(shutil.which("officecli"), "OfficeCLI is not installed")
class DocumentIntelligenceRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_state = ogent.STATE
        self.patch = mock.patch.multiple(
            ogent,
            WORK_ROOT=self.root / "work",
            IMPORT_ROOT=self.root / "imports",
            REFERENCE_ROOT=self.root / "temporary-references",
            BACKUP_ROOT=self.root / "backups",
            SESSION_MEMORY_ROOT=self.root / "session-memory",
            RECENT_PATH=self.root / "recent.json",
            BACKUP_STORE=ogent.BackupStore(
                self.root / "backups",
                application_version=ogent.APP_VERSION,
            ),
            SESSION_MEMORY_STORE=ogent.SessionMemoryStore(
                self.root / "session-memory",
                launch_id="document-index-runtime",
            ),
            SERVICES_INITIALIZED=False,
            DURABLE_SERVICES_ROOT=None,
            DURABLE_DATABASE=None,
            DURABLE_BLOB_STORE=None,
            WORKSPACE_REPOSITORY=None,
            TURN_REPOSITORY=None,
            RUN_REPOSITORY=None,
            EVENT_REPOSITORY=None,
            METADATA_REPOSITORY=None,
            WORKSPACE_ACTORS=None,
            LEGACY_IMPORTER=None,
            SKILL_POLICY_REPOSITORY=None,
            CAPABILITY_RECEIPT_REPOSITORY=None,
            TOOL_RECEIPT_REPOSITORY=None,
            OFFICECLI_EXECUTOR=None,
            OFFICECLI_TYPED_GATEWAY=None,
            OFFICECLI_SKILL_REGISTRY=None,
            DOCUMENT_CAPABILITY_BOOTSTRAP=None,
            GATEWAY_AUDIT_INGESTOR=None,
            ROLLBACK_MANAGER=None,
            OUTCOME_VERIFIER=None,
            DOCUMENT_REPOSITORY=None,
            DOCUMENT_INDEXER=None,
            DOCUMENT_INTELLIGENCE=None,
            VISUAL_REGION_REPOSITORY=None,
            VISUAL_REGION_SERVICE=None,
        )
        self.patch.start()
        ogent.WORK_ROOT.mkdir()
        ogent.IMPORT_ROOT.mkdir()
        ogent.REFERENCE_ROOT.mkdir()
        self.state = ogent.OgentState()
        ogent.STATE = self.state
        self.environment = {
            **os.environ,
            "OFFICECLI_NO_AUTO_RESIDENT": "1",
        }

    def tearDown(self) -> None:
        coordinator = ogent.DOCUMENT_INTELLIGENCE
        if coordinator is not None:
            coordinator.stop()
        with mock.patch.object(ogent, "stop_watch", return_value=None):
            for session in list(self.state.sessions.values()):
                ogent.close_session(session)
        ogent.STATE = self.original_state
        self.patch.stop()
        self.temporary.cleanup()

    def test_open_and_mutation_use_durable_revision_observer(self) -> None:
        document = self.root / "observed.docx"
        self._officecli("create", str(document))
        self._officecli(
            "add",
            str(document),
            "/body",
            "--type",
            "paragraph",
            "--prop",
            "text=Initial heading",
            "--prop",
            "style=Heading1",
        )
        session = self.state.create_session()

        def fake_watch(
            current: ogent.SessionState,
            _document: Path,
        ) -> None:
            with current.lock:
                current.watch_port = 26320

        with (
            mock.patch.object(
                ogent,
                "start_watch",
                side_effect=fake_watch,
            ),
            mock.patch.object(
                ogent,
                "start_selection_broker",
                return_value=None,
            ),
            mock.patch.object(
                ogent,
                "detect_complex_layout",
                return_value=(False, None),
            ),
        ):
            opened = ogent.open_document(session, str(document))

        self._finish_current_attempt(session)
        snapshot = session.public_snapshot(include_watch_probe=False)
        self.assertEqual(opened["document_id"], session.document_id)
        self.assertEqual(session.document_revision, 1)
        self.assertEqual(
            snapshot["document_index"]["status"],
            "complete",
        )
        self.assertGreater(
            snapshot["document_index"]["indexed_nodes"],
            0,
        )
        self.assertEqual(
            ogent.WORKSPACE_REPOSITORY.get(session.session_id).document_id,
            session.document_id,
        )

        self._officecli(
            "set",
            str(document),
            "/body/p[1]",
            "--prop",
            "text=Changed heading",
        )
        fingerprint = ogent.package_sha256(document)
        revision, advanced = ogent.advance_document_revision(
            session,
            document,
            fingerprint,
        )
        self.assertTrue(advanced)
        self.assertEqual(revision, 2)
        self._finish_current_attempt(session)
        current = ogent.DOCUMENT_REPOSITORY.current_state_for_workspace(
            session.session_id
        )
        assert current is not None
        durable_revision, job = current
        self.assertEqual(durable_revision.revision_number, 2)
        self.assertEqual(job.status.value, "complete")
        delta = ogent.DOCUMENT_REPOSITORY.delta(durable_revision.revision_id)
        self.assertTrue(delta.changed_paths)
        self.assertTrue(
            any(
                event["type"] == "index_progress"
                for event in session.current_events_after(0)
            )
        )

    def _finish_current_attempt(
        self,
        session: ogent.SessionState,
    ) -> None:
        assert ogent.DOCUMENT_REPOSITORY is not None
        assert ogent.DOCUMENT_INTELLIGENCE is not None
        current = ogent.DOCUMENT_REPOSITORY.current_state_for_workspace(
            session.session_id
        )
        assert current is not None
        revision, job = current
        if not job.status.terminal:
            with ogent.DOCUMENT_INTELLIGENCE.lock:
                future = ogent.DOCUMENT_INTELLIGENCE.tasks.get(revision.revision_id)
            if future is not None:
                future.result(timeout=30)
        final = ogent.DOCUMENT_REPOSITORY.job(revision.revision_id)
        assert final is not None
        self.assertTrue(final.status.terminal)

    def _officecli(self, *arguments: str) -> None:
        subprocess.run(
            ["officecli", *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment,
            timeout=45,
        )

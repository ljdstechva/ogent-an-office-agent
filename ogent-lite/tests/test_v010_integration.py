from __future__ import annotations

import hashlib
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

import ogent  # noqa: E402


class V010IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.originals = {
            name: getattr(ogent, name)
            for name in (
                "STATE",
                "WORK_ROOT",
                "IMPORT_ROOT",
                "REFERENCE_ROOT",
                "BACKUP_ROOT",
                "BACKUP_STORE",
                "SESSION_MEMORY_ROOT",
                "SESSION_MEMORY_STORE",
                "RECENT_PATH",
                "SERVICES_INITIALIZED",
            )
        }
        ogent.WORK_ROOT = self.root / "work"
        ogent.IMPORT_ROOT = self.root / "imports"
        ogent.REFERENCE_ROOT = self.root / "temporary-references"
        ogent.BACKUP_ROOT = self.root / "backups"
        ogent.SESSION_MEMORY_ROOT = self.root / "session-memory"
        ogent.RECENT_PATH = self.root / "recent.json"
        ogent.WORK_ROOT.mkdir()
        ogent.IMPORT_ROOT.mkdir()
        ogent.REFERENCE_ROOT.mkdir()
        ogent.BACKUP_STORE = ogent.BackupStore(
            ogent.BACKUP_ROOT,
            application_version=ogent.APP_VERSION,
        )
        ogent.SESSION_MEMORY_STORE = ogent.SessionMemoryStore(
            ogent.SESSION_MEMORY_ROOT,
            launch_id="v010-integration",
        )
        ogent.SERVICES_INITIALIZED = False
        self.state = ogent.OgentState()
        ogent.STATE = self.state

    def tearDown(self) -> None:
        with mock.patch.object(ogent, "stop_watch", return_value=None):
            for session in list(self.state.sessions.values()):
                ogent.close_session(session)
        for name, value in self.originals.items():
            setattr(ogent, name, value)
        self.temporary.cleanup()

    @staticmethod
    def _set_fake_watch(
        session: ogent.SessionState,
        _document: Path,
    ) -> None:
        with session.lock:
            session.watch_port = 26320

    def _open_patches(self) -> tuple[mock._patch, mock._patch, mock._patch]:
        return (
            mock.patch.object(
                ogent,
                "start_watch",
                side_effect=self._set_fake_watch,
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
        )

    def _canonical_attachment(
        self,
        session: ogent.SessionState,
        index: int,
    ) -> ogent.ReferenceAttachment:
        assert session.attachment_store is not None
        assert session.memory is not None
        identifier = f"{index + 1:032x}"
        incoming = session.attachment_store.begin_upload(identifier)
        source = incoming / "source.txt"
        content = f"ATTACHMENT-{index:02d}".encode()
        source.write_bytes(content)
        pending = ogent.ReferenceAttachment(
            attachment_id=identifier,
            original_name=f"attachment-{index:02d}.txt",
            source_path=source,
            detected_type="text/plain; charset=utf-8",
            kind="Text",
            byte_size=len(content),
            uploaded_at=ogent.now_iso(),
        )
        attachment = session.attachment_store.commit_upload(source, pending)
        session.retained_references[identifier] = attachment
        session.pending_references.append(attachment)
        session.memory.record_attachment(
            attachment_id=identifier,
            filename=attachment.original_name,
            detected_type=attachment.detected_type,
            kind=attachment.kind,
            byte_size=attachment.byte_size,
            uploaded_at=attachment.uploaded_at,
            status=attachment.status,
            canonical_path=attachment.source_path,
        )
        return attachment

    def test_local_office_formats_edit_source_only_after_verified_backup(
        self,
    ) -> None:
        for index, extension in enumerate((".docx", ".xlsx", ".pptx")):
            with self.subTest(extension=extension):
                session = self.state.create_session()
                source = self.root / f"local-{index}{extension}"
                original = f"synthetic-{extension}".encode()
                source.write_bytes(original)
                observed_backup: list[ogent.BackupRecord] = []

                def start_after_backup(
                    current: ogent.SessionState,
                    document: Path,
                ) -> None:
                    records = [
                        item
                        for item in ogent.BACKUP_STORE.list_records()
                        if item.source_path == source.resolve()
                    ]
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0].backup_path.read_bytes(), original)
                    observed_backup.extend(records)
                    self._set_fake_watch(current, document)

                with (
                    mock.patch.object(
                        ogent,
                        "start_watch",
                        side_effect=start_after_backup,
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
                    result = ogent.dispatch_open_path(session, str(source))

                self.assertEqual(result["action"], "document_opened")
                self.assertEqual(result["document_mode"], "local_direct")
                self.assertEqual(
                    result["message"],
                    "Editing original · recovery backup created",
                )
                self.assertEqual(session.active_doc, source.resolve())
                self.assertEqual(session.active_source, source.resolve())
                self.assertIsNotNone(session.recovery_backup)
                self.assertEqual(
                    hashlib.sha256(observed_backup[0].backup_path.read_bytes()).digest(),
                    hashlib.sha256(original).digest(),
                )
                source.write_bytes(original + b"-edited")
                self.assertEqual(observed_backup[0].backup_path.read_bytes(), original)

    def test_backup_failure_aborts_before_watch_or_document_commit(self) -> None:
        session = self.state.create_session()
        source = self.root / "blocked.docx"
        source.write_bytes(b"synthetic")
        with (
            mock.patch.object(
                ogent.BACKUP_STORE,
                "create_backup",
                side_effect=ogent.BackupError("intentional backup failure"),
            ),
            mock.patch.object(ogent, "start_watch") as start_watch,
        ):
            with self.assertRaisesRegex(
                ogent.UserFacingError,
                "verified recovery backup could not be created",
            ):
                ogent.open_document(session, str(source))
        start_watch.assert_not_called()
        self.assertIsNone(session.active_doc)
        self.assertIsNone(session.active_source)

    def test_browser_upload_edits_only_imported_copy_without_backup_claim(
        self,
    ) -> None:
        session = self.state.create_session()
        original = self.root / "browser-source.docx"
        original_bytes = b"browser source fixture"
        original.write_bytes(original_bytes)
        import_directory = ogent.IMPORT_ROOT / session.session_id / ("a" * 32)
        import_directory.mkdir(parents=True)
        imported = import_directory / original.name
        shutil.copy2(original, imported)
        patches = self._open_patches()
        with patches[0], patches[1], patches[2]:
            result = ogent.dispatch_open_path(
                session,
                str(imported),
                origin="browser_upload",
            )

        self.assertEqual(result["document_mode"], "browser_import")
        self.assertEqual(
            result["message"],
            "Browser upload · editing an imported copy",
        )
        self.assertEqual(session.active_doc, imported.resolve())
        self.assertIsNone(session.recovery_backup)
        self.assertEqual(ogent.BACKUP_STORE.summary()["count"], 0)
        imported.write_bytes(b"edited imported copy")
        self.assertEqual(original.read_bytes(), original_bytes)

    def test_same_source_deduplicates_before_a_second_backup_or_watch(self) -> None:
        first = self.state.create_session()
        second = self.state.create_session()
        source = self.root / "deduplicated.xlsx"
        source.write_bytes(b"synthetic workbook")
        patches = self._open_patches()
        with patches[0], patches[1], patches[2]:
            first_result = ogent.dispatch_open_path(first, str(source))
            second_result = ogent.dispatch_open_path(second, str(source))

        self.assertEqual(first_result["action"], "document_opened")
        self.assertEqual(second_result["action"], "focus_session")
        self.assertEqual(second_result["session_id"], first.session_id)
        self.assertEqual(ogent.BACKUP_STORE.summary()["count"], 1)
        self.assertIsNone(second.active_doc)

    def test_pdf_dispatch_remains_source_protected(self) -> None:
        session = self.state.create_session()
        source = self.root / "protected.pdf"
        original = b"%PDF-1.7\nsynthetic"
        source.write_bytes(original)
        with mock.patch.object(
            ogent,
            "start_pdf_import",
            return_value="b" * 32,
        ) as start_pdf:
            result = ogent.dispatch_open_path(session, str(source))
        start_pdf.assert_called_once()
        self.assertEqual(result["action"], "pdf_import")
        self.assertIn("original PDF will remain untouched", result["message"])
        self.assertEqual(source.read_bytes(), original)
        self.assertIsNone(session.active_doc)
        self.assertEqual(ogent.BACKUP_STORE.summary()["count"], 0)

    def test_memory_survives_reconnect_grace_then_reap_deletes_it(self) -> None:
        session = self.state.create_session()
        assert session.memory is not None
        memory_root = session.memory.root
        session.add_message("user", "Remember the approved teal heading.")
        session.connect_sse("browser-client")
        session.mark_page_closed("browser-client")
        with session.lock:
            session.orphan_since = 1_000.0
        self.state.session_grace_seconds = 10.0
        self.assertFalse(
            ogent.close_session(session, require_reapable_at=1_005.0)
        )
        self.assertTrue(memory_root.is_dir())
        session.connect_sse("reconnected-client")
        self.assertIsNone(session.orphan_since)
        session.mark_page_closed("reconnected-client")
        with session.lock:
            session.orphan_since = 2_000.0
        self.assertTrue(
            ogent.close_session(session, require_reapable_at=2_011.0)
        )
        self.assertFalse(memory_root.exists())
        with self.assertRaises(ogent.UserFacingError):
            self.state.get_session(session.session_id)

    def test_clear_memory_removes_canonical_attachments_but_keeps_document(
        self,
    ) -> None:
        session = self.state.create_session()
        assert session.memory is not None
        session.memory.set_active_document(
            document_id="document-12345678",
            basename="active.docx",
            revision=4,
        )
        session.add_message("user", "Keep this only until I clear memory.")
        attachment = self._canonical_attachment(session, 0)
        canonical_directory = attachment.source_path.parent

        summary = ogent.clear_session_memory(session)

        self.assertEqual(summary["retained_turns"], 0)
        self.assertEqual(summary["retained_attachments"], 0)
        self.assertEqual(
            session.memory.active_document["document_id"],
            "document-12345678",
        )
        self.assertFalse(canonical_directory.exists())
        self.assertEqual(session.transcript, [])
        self.assertEqual(session.pending_references, [])
        self.assertEqual(session.retained_references, {})

    def test_send_atomically_claims_20_attachments_and_selection_memory(
        self,
    ) -> None:
        session = self.state.create_session()
        document = self.root / "focused.docx"
        document.write_bytes(b"synthetic")
        self.state.commit_document(
            session,
            document,
            document,
            preserve_transcript=True,
            reset_run=True,
            complex_layout=False,
            complex_layout_detail=None,
            document_mode="local_direct",
        )
        session.preview_selection.apply_paths(
            ["/body/p[2]"],
            lambda paths: [
                {
                    "path": paths[0],
                    "type": "paragraph",
                    "text": "SELECTED_SENTINEL",
                }
            ],
        )
        for index in range(20):
            self._canonical_attachment(session, index)

        captured: dict[str, object] = {}
        completed = threading.Event()

        def fake_worker(
            current: ogent.SessionState,
            message: str,
            active_document: Path | None,
            source: Path | None,
            provider: str,
            model: str,
            effort: str,
            run_id: str,
            references: list[ogent.ReferenceAttachment],
            selection_snapshot: ogent.PreviewSelectionSnapshot | None,
            user_sequence: int | None,
            timing: ogent.RunTiming,
        ) -> None:
            captured.update(
                {
                    "message": message,
                    "document": active_document,
                    "source": source,
                    "provider": provider,
                    "model": model,
                    "effort": effort,
                    "references": references,
                    "selection": selection_snapshot,
                    "user_sequence": user_sequence,
                    "timing": timing,
                }
            )
            ogent._finish_session_run(
                current,
                run_id,
                "completed",
                kind=provider,
            )
            completed.set()

        with mock.patch.object(ogent, "_agent_worker", side_effect=fake_worker):
            ogent.start_agent_run(
                session,
                "Update only the selected paragraph.",
                "gpt-5.6-sol",
                "automatic",
                "codex",
            )
            self.assertTrue(completed.wait(timeout=10))

        selection = captured["selection"]
        assert isinstance(selection, ogent.PreviewSelectionSnapshot)
        self.assertEqual(len(captured["references"]), 20)  # type: ignore[arg-type]
        self.assertEqual(selection.targets[0].path, "/body/p[2]")
        self.assertEqual(session.preview_selection.targets, [])
        self.assertEqual(session.pending_references, [])
        self.assertEqual(len(session.transcript), 1)
        submitted = session.transcript[0]
        self.assertEqual(len(submitted["attachments"]), 20)
        self.assertEqual(len(submitted["preview_selections"]), 1)
        self.assertEqual(
            submitted["preview_selections"][0]["path"],
            "/body/p[2]",
        )
        self.assertEqual(
            submitted["attachments"][0]["processing_status"],
            "Ready",
        )

        focused_prompt = ogent.agent_prompt(
            "Update only the selected paragraph.",
            document,
            document,
            preview_selection=selection,
            document_mode="local_direct",
        )
        self.assertIn("/body/p[2]", focused_prompt)
        self.assertIn("SELECTED_SENTINEL", focused_prompt)
        self.assertNotIn("UNSELECTED_OUTSIDE_SENTINEL", focused_prompt)
        self.assertIn("Do not run full-document", focused_prompt)

        assert session.memory is not None
        claude_context = session.memory.build_provider_context(
            "Continue from the submitted selection.",
            provider="claude",
            model="claude-sonnet",
            effort="automatic",
            fresh_context=True,
        )
        switched_model_context = session.memory.build_provider_context(
            "Continue from the submitted selection.",
            provider="codex",
            model="gpt-5.6-terra",
            effort="high",
            fresh_context=True,
        )
        for context in (claude_context, switched_model_context):
            self.assertIn("/body/p[2]", context.text)
            self.assertIn("SELECTED_SENTINEL", context.text)

    def test_backend_run_status_keeps_terminal_outcome_separate(self) -> None:
        expectations = {
            "completed": ("idle", "completed"),
            "error": ("error", "error"),
            "stopped": ("stopped", "stopped"),
        }
        for index, (terminal, expected) in enumerate(expectations.items()):
            with self.subTest(terminal=terminal):
                session = self.state.create_session()
                run_id = f"{index + 10:032x}"
                session.run_id = run_id
                session.run_status = "working"
                session.run_complete.clear()
                self.assertTrue(
                    ogent._finish_session_run(session, run_id, terminal)
                )
                self.assertEqual(
                    (session.run_status, session.last_run_outcome),
                    expected,
                )
                self.assertTrue(session.run_complete.is_set())

    def test_touch_multi_select_bridge_does_not_overwrite_broker_merge(
        self,
    ) -> None:
        session = self.state.create_session()
        document = self.root / "multi-select.docx"
        document.write_bytes(b"synthetic")
        document_id = "document-12345678"
        with session.lock:
            session.active_doc = document.resolve()
            session.watch_port = 26320
            session.document_id = document_id
            session.document_revision = 1
            session.selection_multi_mode = True
        session.preview_selection.reset_for_watch(
            document_id=document_id,
            document_name=document.name,
            document_format="docx",
            revision=1,
        )

        def resolve(paths: list[str]) -> list[dict[str, object]]:
            return [
                {
                    "type": "paragraph",
                    "text": f"Target {index}",
                    "preview": f"Target {index}",
                    "style": "Normal",
                    "format": {},
                }
                for index, _path in enumerate(paths, start=1)
            ]

        session.preview_selection.apply_paths(
            ["/body/p[1]", "/body/p[2]"],
            resolve,
            expected_watch_id=session.preview_selection.watch_id,
            expected_document_id=document_id,
            expected_revision=1,
        )
        payload = session.preview_selection.public_state()
        payload.update(
            {
                "type": "selection.changed",
                "selected": [{"path": "/body/p[2]"}],
                "primary_path": "/body/p[2]",
            }
        )

        with (
            mock.patch.object(ogent, "_resolve_preview_nodes") as resolver,
            mock.patch.object(ogent, "post_watch_selection") as post_selection,
        ):
            ogent.accept_postmessage_selection(
                session,
                payload,
                event_origin="http://127.0.0.1:26320",
                source_matches=True,
            )

        self.assertEqual(
            [item.path for item in session.preview_selection.targets],
            ["/body/p[1]", "/body/p[2]"],
        )
        resolver.assert_not_called()
        post_selection.assert_not_called()

    def test_ui_template_has_accessible_status_settings_and_selection_contract(
        self,
    ) -> None:
        html = ogent.HTML_TEMPLATE
        self.assertIn('aria-label="Settings and recovery"', html)
        self.assertIn('role="dialog"', html)
        self.assertIn('role="status"', html)
        self.assertIn("@media (prefers-reduced-motion: reduce)", html)
        for state in ("working", "completed", "error", "stopped", "neutral"):
            self.assertIn(f".run-status.{state}", html)
        self.assertIn('id="selectionTray"', html)
        self.assertIn("selection-chip.primary", html)
        self.assertIn("Clear selection", html)
        self.assertIn("Touch multi-select", html)
        self.assertIn("@media (max-width: 820px)", html)
        self.assertIn("grid-template-rows: auto auto auto auto auto;", html)
        self.assertIn("max-height: min(320px, 38vh);", html)
        self.assertIn("LITE __VERSION__", html)
        self.assertIn("Editing original · recovery backup created", html)
        self.assertIn("Browser upload · editing an imported copy", html)
        self.assertNotIn("Delete all backups", html)
        self.assertNotIn("legacySetRunStatus", html)

        declared_ids = set(re.findall(r'\bid="([^"]+)"', html))
        referenced_ids = set(
            re.findall(r'document\.getElementById\("([^"]+)"\)', html)
        )
        self.assertEqual(referenced_ids - declared_ids, set())


if __name__ == "__main__":
    unittest.main()

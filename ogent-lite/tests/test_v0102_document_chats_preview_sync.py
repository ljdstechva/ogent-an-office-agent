from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import shutil
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

import ogent  # noqa: E402


@unittest.skipUnless(shutil.which("officecli"), "OfficeCLI is not installed")
class V0102WorkspaceTests(unittest.TestCase):
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
            launch_id="v0102-workspaces",
        )
        ogent.SERVICES_INITIALIZED = False
        self.state = ogent.OgentState()
        ogent.STATE = self.state
        self.watch_port = 27000

    def tearDown(self) -> None:
        with mock.patch.object(ogent, "stop_watch", return_value=None):
            for session in list(self.state.sessions.values()):
                ogent.close_session(session)
        for name, value in self.originals.items():
            setattr(ogent, name, value)
        self.temporary.cleanup()

    def _fake_watch(
        self,
        session: ogent.SessionState,
        _document: Path,
    ) -> None:
        self.watch_port += 1
        generation = f"{self.watch_port:032x}"
        with session.lock:
            session.watch_port = self.watch_port
            session.watch_generation = generation

    def _open_patches(self) -> tuple[mock._patch, mock._patch, mock._patch]:
        return (
            mock.patch.object(
                ogent,
                "start_watch",
                side_effect=self._fake_watch,
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

    def _open(
        self,
        session: ogent.SessionState,
        document: Path,
        *,
        origin: str = "local_path",
    ) -> dict[str, object]:
        patches = self._open_patches()
        with patches[0], patches[1], patches[2]:
            return ogent.dispatch_open_path(
                session,
                str(document),
                origin=origin,
            )

    def _attachment(
        self,
        session: ogent.SessionState,
        identifier: str = "a" * 32,
    ) -> ogent.ReferenceAttachment:
        assert session.attachment_store is not None
        assert session.memory is not None
        incoming = session.attachment_store.begin_upload(identifier)
        source = incoming / "reference.txt"
        content = b"DOCUMENT-A-REFERENCE"
        source.write_bytes(content)
        pending = ogent.ReferenceAttachment(
            attachment_id=identifier,
            original_name="reference.txt",
            source_path=source,
            detected_type="text/plain; charset=utf-8",
            kind="Text",
            byte_size=len(content),
            uploaded_at=ogent.now_iso(),
        )
        attachment = session.attachment_store.commit_upload(source, pending)
        session.pending_references.append(attachment)
        session.retained_references[identifier] = attachment
        session.memory.record_attachment(
            attachment_id=identifier,
            filename=attachment.original_name,
            detected_type=attachment.detected_type,
            kind=attachment.kind,
            byte_size=attachment.byte_size,
            uploaded_at=attachment.uploaded_at,
            status="Available in this session",
            canonical_path=attachment.source_path,
        )
        return attachment

    @staticmethod
    def _context(
        session: ogent.SessionState,
        *,
        provider: str,
    ) -> str:
        assert session.memory is not None
        return session.memory.build_provider_context(
            "Continue the current document work.",
            provider=provider,
            model=f"{provider}-model",
            effort="automatic",
            fresh_context=True,
        ).text

    def test_document_switching_restores_only_each_document_workspace(
        self,
    ) -> None:
        first_dir = self.root / "first"
        second_dir = self.root / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        document_a = first_dir / "same-name.docx"
        document_b = second_dir / "same-name.docx"
        document_a.write_bytes(b"document-a")
        document_b.write_bytes(b"document-b")
        shell = self.state.create_session()

        result_a = self._open(shell, document_a)
        self.assertEqual(result_a["action"], "document_opened")
        shell.add_message(
            "user",
            "DOCUMENT-A-ONLY",
            provider="codex",
            model="codex-model",
        )
        shell.add_message(
            "assistant",
            "A-ANSWER",
            provider="claude",
            model="claude-model",
        )
        self.assertIn(
            "A-ANSWER",
            self._context(shell, provider="codex"),
        )
        self.assertIn(
            "DOCUMENT-A-ONLY",
            self._context(shell, provider="claude"),
        )

        result_b = self._open(shell, document_b)
        self.assertEqual(result_b["action"], "focus_session")
        workspace_b = self.state.get_session(str(result_b["session_id"]))
        self.assertIsNot(workspace_b, shell)
        self.assertEqual(workspace_b.transcript, [])
        self.assertNotIn(
            "DOCUMENT-A-ONLY", self._context(workspace_b, provider="codex")
        )
        workspace_b.add_message(
            "user",
            "DOCUMENT-B-ONLY",
            provider="claude",
            model="claude-model",
        )

        return_a = self._open(workspace_b, document_a)
        self.assertEqual(return_a["session_id"], shell.session_id)
        self.assertEqual(
            [item["text"] for item in shell.transcript],
            ["DOCUMENT-A-ONLY", "A-ANSWER"],
        )
        self.assertNotIn(
            "DOCUMENT-B-ONLY",
            self._context(shell, provider="claude"),
        )
        return_b = self._open(shell, document_b)
        self.assertEqual(return_b["session_id"], workspace_b.session_id)
        self.assertEqual(
            [item["text"] for item in workspace_b.transcript],
            ["DOCUMENT-B-ONLY"],
        )

    def test_connected_workspace_retains_inactive_document_past_grace(
        self,
    ) -> None:
        document_a = self.root / "retained-a.docx"
        document_b = self.root / "connected-b.docx"
        document_a.write_bytes(b"document-a")
        document_b.write_bytes(b"document-b")
        session_a = self.state.create_session()
        session_b = self.state.create_session()
        self._open(session_a, document_a)
        self._open(session_b, document_b)
        self.state.session_grace_seconds = 5
        with session_a.lock:
            session_a.orphan_since = 1.0
        session_b.connect_sse("connected-client-1234")

        self.assertFalse(
            ogent.close_session(
                session_a,
                require_reapable_at=100.0,
            )
        )
        with session_a.lock:
            self.assertFalse(session_a.closed)
            self.assertEqual(session_a.orphan_since, 100.0)
        self.assertIn(session_a.session_id, self.state.sessions)

        session_b.disconnect_sse("connected-client-1234")
        self.assertFalse(
            ogent.close_session(
                session_a,
                require_reapable_at=104.9,
            )
        )
        self.assertTrue(
            ogent.close_session(
                session_a,
                require_reapable_at=105.0,
            )
        )
        self.assertNotIn(session_a.session_id, self.state.sessions)

    def test_aliases_dedupe_and_distinct_same_names_do_not_merge(self) -> None:
        directory = self.root / "alias"
        directory.mkdir()
        document = directory / "Alias.docx"
        document.write_bytes(b"alias")
        session = self.state.create_session()
        self._open(session, document)

        separator_alias = str(document).replace("\\", "/")
        result = ogent.dispatch_open_path(session, separator_alias)
        self.assertEqual(result["action"], "document_already_open")

        dot_alias = document.parent / ".." / document.parent.name / document.name
        result = ogent.dispatch_open_path(session, str(dot_alias))
        self.assertEqual(result["session_id"], session.session_id)

        with mock.patch.object(ogent.Path, "cwd", return_value=directory):
            result = ogent.dispatch_open_path(session, document.name)
        self.assertEqual(result["session_id"], session.session_id)
        if os.name == "nt":
            result = ogent.dispatch_open_path(session, str(document).swapcase())
            self.assertEqual(result["session_id"], session.session_id)

    def test_concurrent_open_claims_one_workspace_without_cross_contamination(
        self,
    ) -> None:
        document = self.root / "concurrent.xlsx"
        document.write_bytes(b"workbook")
        first = self.state.create_session()
        second = self.state.create_session()
        barrier = threading.Barrier(2)
        original_claim = self.state.claim_source

        def synchronized_claim(
            session: ogent.SessionState,
            source: Path,
        ) -> ogent.SessionState | None:
            barrier.wait(timeout=5)
            return original_claim(session, source)

        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def run(session: ogent.SessionState) -> None:
            try:
                results.append(ogent.dispatch_open_path(session, str(document)))
            except BaseException as exc:  # pragma: no cover - asserted below.
                errors.append(exc)

        patches = self._open_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch.object(
                self.state,
                "claim_source",
                side_effect=synchronized_claim,
            ),
        ):
            threads = [
                threading.Thread(target=run, args=(first,)),
                threading.Thread(target=run, args=(second,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        owners = {str(item["session_id"]) for item in results}
        self.assertEqual(len(owners), 1)
        owner = self.state.get_session(owners.pop())
        owner.add_message("user", "OWNER-ONLY")
        other = second if owner is first else first
        self.assertEqual(other.transcript, [])

    def test_browser_import_and_pdf_allocate_isolated_workspaces(self) -> None:
        document = self.root / "active.pptx"
        pdf = self.root / "protected.pdf"
        document.write_bytes(b"slides")
        pdf.write_bytes(b"%PDF-1.7\nsynthetic")
        active = self.state.create_session()
        self._open(active, document)
        active.add_message("user", "ACTIVE-DOCUMENT-CHAT")

        import_workspace, created = self.state.allocate_document_session(active)
        import_directory = ogent.IMPORT_ROOT / import_workspace.session_id / ("a" * 32)
        import_directory.mkdir(parents=True)
        imported = import_directory / "browser.docx"
        imported.write_bytes(b"browser")
        patches = self._open_patches()
        with patches[0], patches[1], patches[2]:
            imported_result = ogent.dispatch_open_path(
                active,
                str(imported),
                origin="browser_upload",
                target_session=import_workspace,
                target_created=created,
            )
        imported_workspace = self.state.get_session(str(imported_result["session_id"]))
        self.assertEqual(imported_workspace.transcript, [])
        self.assertEqual(
            imported_workspace.document_mode,
            "browser_import",
        )

        with mock.patch.object(
            ogent,
            "start_pdf_import",
            return_value="b" * 32,
        ):
            pdf_result = ogent.dispatch_open_path(active, str(pdf))
        pdf_workspace = self.state.get_session(str(pdf_result["session_id"]))
        self.assertIsNot(pdf_workspace, active)
        self.assertEqual(pdf_workspace.transcript, [])
        self.assertEqual(
            [item["text"] for item in active.transcript],
            ["ACTIVE-DOCUMENT-CHAT"],
        )

    def test_new_chat_is_scoped_transactional_and_rejects_late_events(
        self,
    ) -> None:
        document_a = self.root / "reset-a.docx"
        document_b = self.root / "reset-b.docx"
        document_a.write_bytes(b"document-a-edits")
        document_b.write_bytes(b"document-b-edits")
        workspace_a = self.state.create_session()
        self._open(workspace_a, document_a)
        result_b = self._open(workspace_a, document_b)
        workspace_b = self.state.get_session(str(result_b["session_id"]))
        workspace_a.add_message("user", "CLEAR-ME")
        workspace_b.add_message("user", "KEEP-B")
        attachment = self._attachment(workspace_a)
        workspace_a.preview_selection.apply_paths(
            ["/body/p[1]"],
            lambda paths: [
                {
                    "path": paths[0],
                    "type": "paragraph",
                    "text": "CLEAR-SELECTION",
                }
            ],
        )
        workspace_a.connect_sse("reset-client-one")
        workspace_a.connect_sse("reset-client-two")
        attachment_root = attachment.source_path.parent
        with workspace_a.lock:
            workspace_a.codex_thread_id = "codex-old"
            workspace_a.claude_session_id = "claude-old"
            old_generation = workspace_a.conversation_generation
            watch_port = workspace_a.watch_port
            watch_generation = workspace_a.watch_generation
            document_id = workspace_a.document_id
            backup = workspace_a.recovery_backup
        self.assertIsNotNone(backup)
        assert backup is not None
        document_hash = hashlib.sha256(document_a.read_bytes()).hexdigest()
        backup_hash = hashlib.sha256(backup.backup_path.read_bytes()).hexdigest()
        event_cursor = workspace_a.sequence

        with mock.patch.object(
            ogent,
            "post_session_watch_selection",
            return_value=None,
        ):
            result = ogent.reset_document_conversation(
                workspace_a,
                reason="new_chat",
            )

        self.assertEqual(result["generation"], old_generation + 1)
        self.assertEqual(workspace_a.transcript, [])
        self.assertEqual(workspace_a.pending_references, [])
        self.assertEqual(workspace_a.retained_references, {})
        self.assertEqual(workspace_a.preview_selection.targets, [])
        self.assertFalse(attachment_root.exists())
        self.assertIsNone(workspace_a.codex_thread_id)
        self.assertIsNone(workspace_a.claude_session_id)
        self.assertEqual(
            [item["text"] for item in workspace_b.transcript],
            ["KEEP-B"],
        )
        self.assertEqual(
            hashlib.sha256(document_a.read_bytes()).hexdigest(),
            document_hash,
        )
        self.assertEqual(
            hashlib.sha256(backup.backup_path.read_bytes()).hexdigest(),
            backup_hash,
        )
        with workspace_a.lock:
            self.assertEqual(workspace_a.watch_port, watch_port)
            self.assertEqual(workspace_a.watch_generation, watch_generation)
            self.assertEqual(workspace_a.document_id, document_id)
        rejected = workspace_a.add_message(
            "assistant",
            "LATE-OLD-GENERATION",
            expected_generation=old_generation,
        )
        self.assertTrue(rejected["rejected"])
        self.assertEqual(workspace_a.transcript, [])
        self.assertNotIn("CLEAR-ME", self._context(workspace_a, provider="codex"))
        events = workspace_a.current_events_after(event_cursor)
        resets = [item for item in events if item["type"] == "conversation_reset"]
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0]["generation"], old_generation + 1)
        self.assertEqual(workspace_a.sse_clients, 2)

    def test_new_chat_busy_gate_changes_nothing(self) -> None:
        session = self.state.create_session()
        session.add_message("user", "KEEP-WHILE-BUSY")
        with session.lock:
            session.run_status = "working"
            generation = session.conversation_generation
        with self.assertRaises(ogent.UserFacingError) as caught:
            ogent.reset_document_conversation(session, reason="new_chat")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(session.conversation_generation, generation)
        self.assertEqual(
            [item["text"] for item in session.transcript],
            ["KEEP-WHILE-BUSY"],
        )

    def test_new_chat_rolls_back_attachments_when_memory_commit_fails(self) -> None:
        session = self.state.create_session()
        attachment = self._attachment(session)
        session.add_message(
            "user",
            "KEEP-AFTER-FAILED-RESET",
            attachment_ids=[attachment.attachment_id],
        )
        generation = session.conversation_generation
        assert session.memory is not None

        with mock.patch.object(
            session.memory,
            "clear_conversation",
            side_effect=ogent.SessionMemoryError("persist failed"),
        ):
            with self.assertRaises(ogent.UserFacingError) as caught:
                ogent.reset_document_conversation(session, reason="new_chat")

        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(session.conversation_generation, generation)
        self.assertEqual(
            [item["text"] for item in session.transcript],
            ["KEEP-AFTER-FAILED-RESET"],
        )
        self.assertTrue(attachment.source_path.is_file())
        self.assertIn(attachment.attachment_id, session.retained_references)
        self.assertEqual(len(session.memory.turns), 1)
        self.assertEqual(len(session.memory.attachments), 1)
        self.assertEqual(
            list(session.memory.root.glob(".conversation-reset-*")),
            [],
        )

    def test_reset_endpoint_requires_client_attached_to_exact_session(self) -> None:
        session_a = self.state.create_session()
        session_b = self.state.create_session()
        document_a = self.root / "http-a.docx"
        document_b = self.root / "http-b.docx"
        document_a.write_bytes(b"a")
        document_b.write_bytes(b"b")
        for session, document in (
            (session_a, document_a),
            (session_b, document_b),
        ):
            with session.lock:
                session.active_doc = document
                session.active_source = document
                session.document_id = f"document-{session.session_id}"
            assert session.memory is not None
            session.memory.set_active_document(
                document_id=session.document_id,
                basename=document.name,
                revision=1,
            )
        session_a.connect_sse("client-a-12345678")
        session_b.connect_sse("client-b-12345678")
        server = ogent.OgentServer((ogent.HOST, 0), ogent.OgentHandler)
        port = int(server.server_address[1])
        self.state.server_port = port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(
            session: ogent.SessionState,
            client: str,
        ) -> urllib.request.Request:
            return urllib.request.Request(
                f"http://{ogent.HOST}:{port}/conversation/reset",
                data=b'{"confirm":true}',
                headers={
                    "Content-Type": "application/json",
                    "X-Ogent-Token": self.state.token,
                    "X-Ogent-Session": session.session_id,
                    "X-Ogent-Client": client,
                },
                method="POST",
            )

        try:
            with self.assertRaises(urllib.error.HTTPError) as wrong:
                urllib.request.urlopen(
                    request(session_b, "client-a-12345678"),
                    timeout=10,
                )
            self.assertEqual(wrong.exception.code, 403)
            wrong.exception.close()
            with urllib.request.urlopen(
                request(session_a, "client-a-12345678"),
                timeout=10,
            ) as response:
                payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["generation"], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_preview_relay_channel_requires_connected_session_client(self) -> None:
        session = self.state.create_session()
        document = self.root / "preview-auth.docx"
        document.write_bytes(b"preview")
        generation = "1" * 32
        with session.lock:
            session.active_doc = document
            session.active_source = document
            session.document_id = "document-preview-auth"
            session.watch_port = 27999
            session.watch_generation = generation
        session.preview_sync.activate_watch(
            session.document_id,
            generation,
        )
        server = ogent.OgentServer((ogent.HOST, 0), ogent.OgentHandler)
        port = int(server.server_address[1])
        self.state.server_port = port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client_id = "client-preview-auth"
        query = urllib.parse.urlencode(
            {
                "s": session.session_id,
                "client": client_id,
                "document": session.document_id,
                "generation": generation,
            }
        )
        request = urllib.request.Request(
            f"http://{ogent.HOST}:{port}/preview?{query}",
        )

        try:
            with self.assertRaises(urllib.error.HTTPError) as disconnected:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(disconnected.exception.code, 403)
            disconnected.exception.close()
            preview_key = (
                client_id,
                session.document_id,
                generation,
            )
            self.assertNotIn(preview_key, session.preview_sync.channels)

            session.connect_sse(client_id)
            with self.assertRaises(urllib.error.HTTPError) as upstream:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(upstream.exception.code, 502)
            upstream.exception.close()
            self.assertIn(preview_key, session.preview_sync.channels)
        finally:
            session.disconnect_sse(client_id)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class V0102PreviewSyncTests(unittest.TestCase):
    def _state(
        self,
    ) -> tuple[
        ogent.PreviewSyncState,
        str,
        str,
        str,
        str,
    ]:
        state = ogent.PreviewSyncState()
        document_id = "document-12345678"
        generation = "1" * 32
        client_id = "client-12345678"
        state.activate_watch(document_id, generation)
        channel = state.register_client(
            client_id=client_id,
            document_id=document_id,
            watch_generation=generation,
        )
        return state, document_id, generation, client_id, channel

    def test_mutation_ack_requires_matching_canonical_dom(self) -> None:
        state, document_id, generation, client_id, channel = self._state()
        for label, canonical_dom in (
            ("missing", None),
            ("mismatch", "e" * 64),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ogent.PreviewSyncError):
                    state.acknowledge(
                        client_id=client_id,
                        document_id=document_id,
                        watch_generation=generation,
                        channel=channel,
                        kind="mutation",
                        version=1,
                        package_sha256="a" * 64,
                        event_fingerprint_value="b" * 64,
                        dom_fingerprint="d" * 64,
                        canonical_dom_fingerprint=canonical_dom,
                    )
        self.assertEqual(list(state.acks), [])

    def test_stale_generation_cannot_authorize_ack_or_receive_controls(
        self,
    ) -> None:
        state, document_id, old_generation, client_id, old_channel = self._state()
        state.enqueue_control(
            client_id=client_id,
            document_id=document_id,
            watch_generation=old_generation,
            action="full",
            package_sha256="a" * 64,
        )
        new_generation = "2" * 32
        state.activate_watch(document_id, new_generation)
        new_channel = state.register_client(
            client_id=client_id,
            document_id=document_id,
            watch_generation=new_generation,
        )
        self.assertNotEqual(old_channel, new_channel)

        with self.assertRaises(ogent.PreviewSyncError):
            state.authorize(
                client_id=client_id,
                document_id=document_id,
                watch_generation=old_generation,
                channel=old_channel,
            )
        with self.assertRaises(ogent.PreviewSyncError):
            state.acknowledge(
                client_id=client_id,
                document_id=document_id,
                watch_generation=old_generation,
                channel=old_channel,
                kind="initial",
                version=0,
                package_sha256="a" * 64,
                dom_fingerprint="d" * 64,
            )
        with self.assertRaises(ogent.PreviewSyncError):
            state.enqueue_control(
                client_id=client_id,
                document_id=document_id,
                watch_generation=old_generation,
                action="full",
                package_sha256="a" * 64,
            )
        with self.assertRaises(ogent.PreviewSyncError):
            state.controls_after(
                client_id=client_id,
                document_id=document_id,
                watch_generation=old_generation,
                channel=old_channel,
                sequence=0,
                timeout=0,
            )

        new_control = state.enqueue_control(
            client_id=client_id,
            document_id=document_id,
            watch_generation=new_generation,
            action="full",
            package_sha256="b" * 64,
        )
        self.assertEqual(
            state.controls_after(
                client_id=client_id,
                document_id=document_id,
                watch_generation=new_generation,
                channel=new_channel,
                sequence=0,
                timeout=0,
            ),
            [new_control],
        )
        self.assertEqual(list(state.acks), [])

    def test_exact_mutation_and_initiating_client_ack_are_required(self) -> None:
        state, document_id, generation, client_id, channel = self._state()
        before = "a" * 64
        after = "b" * 64
        baseline = state.begin_run(
            package_sha256=before,
            client_id=client_id,
        )
        event = {
            "action": "word-patch",
            "version": 7,
            "baseVersion": 6,
            "patches": [{"op": "replace", "block": 4, "html": "<p>new</p>"}],
        }
        mutation = state.observe_mutation(
            event,
            document_id=document_id,
            watch_generation=generation,
            package_sha256=after,
        )
        assert mutation is not None
        unconfirmed = state.wait_for_mutation_confirmation(
            baseline,
            after,
            timeout=0,
        )
        self.assertFalse(unconfirmed.confirmed)
        state.acknowledge(
            client_id=client_id,
            document_id=document_id,
            watch_generation=generation,
            channel=channel,
            kind="mutation",
            version=mutation.version,
            package_sha256=after,
            event_fingerprint_value="c" * 64,
            dom_fingerprint="d" * 64,
            canonical_dom_fingerprint="d" * 64,
        )
        self.assertFalse(
            state.wait_for_mutation_confirmation(
                baseline,
                after,
                timeout=0,
            ).confirmed
        )
        ack = state.acknowledge(
            client_id=client_id,
            document_id=document_id,
            watch_generation=generation,
            channel=channel,
            kind="mutation",
            version=mutation.version,
            package_sha256=after,
            event_fingerprint_value=mutation.event_fingerprint,
            dom_fingerprint="d" * 64,
            canonical_dom_fingerprint="d" * 64,
        )
        confirmed = state.wait_for_mutation_confirmation(
            baseline,
            after,
            timeout=0,
        )
        self.assertTrue(confirmed.confirmed)
        self.assertEqual(confirmed.ack, ack)
        self.assertIsNone(
            state.observe_mutation(
                event,
                document_id="wrong-document-1234",
                watch_generation=generation,
                package_sha256=after,
            )
        )

    def test_two_consecutive_word_edits_each_require_their_own_revision(self) -> None:
        state, document_id, generation, client_id, channel = self._state()
        current = "a" * 64
        for version, action in ((1, "word-patch"), (2, "full")):
            baseline = state.begin_run(
                package_sha256=current,
                client_id=client_id,
            )
            current = f"{version + 1:x}" * 64
            event = {
                "action": action,
                "version": version,
                "baseVersion": version - 1,
            }
            mutation = state.observe_mutation(
                event,
                document_id=document_id,
                watch_generation=generation,
                package_sha256=current,
            )
            assert mutation is not None
            state.acknowledge(
                client_id=client_id,
                document_id=document_id,
                watch_generation=generation,
                channel=channel,
                kind="mutation",
                version=version,
                package_sha256=current,
                event_fingerprint_value=mutation.event_fingerprint,
                dom_fingerprint=f"{version:x}" * 64,
                canonical_dom_fingerprint=f"{version:x}" * 64,
            )
            self.assertTrue(
                state.wait_for_mutation_confirmation(
                    baseline,
                    current,
                    timeout=0,
                ).confirmed
            )

    def test_formatting_and_structural_word_events_confirm_updated_content(
        self,
    ) -> None:
        state, document_id, generation, client_id, channel = self._state()
        current = "a" * 64
        events = (
            {
                "action": "word-patch",
                "version": 11,
                "baseVersion": 10,
                "patches": [
                    {
                        "op": "replace",
                        "block": 4,
                        "html": "<p><strong>same text</strong></p>",
                    }
                ],
            },
            {
                "action": "full",
                "version": 12,
                "baseVersion": 11,
                "reason": "table-row-added",
            },
        )
        for index, event in enumerate(events, start=1):
            baseline = state.begin_run(
                package_sha256=current,
                client_id=client_id,
            )
            current = f"{index + 10:x}" * 64
            mutation = state.observe_mutation(
                event,
                document_id=document_id,
                watch_generation=generation,
                package_sha256=current,
            )
            assert mutation is not None
            state.acknowledge(
                client_id=client_id,
                document_id=document_id,
                watch_generation=generation,
                channel=channel,
                kind="mutation",
                version=mutation.version,
                package_sha256=current,
                event_fingerprint_value=mutation.event_fingerprint,
                dom_fingerprint=f"{index:x}" * 64,
                canonical_dom_fingerprint=f"{index:x}" * 64,
            )
            confirmation = state.wait_for_mutation_confirmation(
                baseline,
                current,
                timeout=0,
            )
            self.assertTrue(confirmation.confirmed)
            self.assertEqual(confirmation.status, "updated")
            assert confirmation.mutation is not None
            self.assertEqual(
                confirmation.mutation.action,
                event["action"],
            )

    def test_early_watch_event_is_associated_with_validated_final_package(
        self,
    ) -> None:
        state, document_id, generation, client_id, channel = self._state()
        before = "a" * 64
        after = "b" * 64
        baseline = state.begin_run(
            package_sha256=before,
            client_id=client_id,
        )
        mutation = state.observe_mutation(
            {
                "action": "word-patch",
                "version": 3,
                "baseVersion": 2,
                "patches": [],
            },
            document_id=document_id,
            watch_generation=generation,
            package_sha256=before,
        )
        assert mutation is not None
        state.acknowledge(
            client_id=client_id,
            document_id=document_id,
            watch_generation=generation,
            channel=channel,
            kind="mutation",
            version=3,
            package_sha256=before,
            event_fingerprint_value=mutation.event_fingerprint,
            dom_fingerprint="d" * 64,
            canonical_dom_fingerprint="d" * 64,
        )
        confirmation = state.wait_for_mutation_confirmation(
            baseline,
            after,
            timeout=0,
        )
        self.assertTrue(confirmation.confirmed)
        assert confirmation.mutation is not None
        self.assertEqual(confirmation.mutation.package_sha256, after)

    def test_full_refresh_recovers_without_watch_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "recover.docx"
            document.write_bytes(b"updated")
            session = ogent.SessionState("preview-refresh")
            with session.lock:
                session.active_doc = document
                session.document_id = "document-12345678"
                session.watch_generation = "1" * 32
            session.preview_sync.activate_watch(
                session.document_id,
                session.watch_generation,
            )
            client = "client-12345678"
            session.preview_sync.register_client(
                client_id=client,
                document_id=session.document_id,
                watch_generation=session.watch_generation,
            )
            baseline = session.preview_sync.begin_run(
                package_sha256="a" * 64,
                client_id=client,
            )
            mutation = ogent.WatchMutation(
                sequence=1,
                document_id=session.document_id,
                watch_generation=session.watch_generation,
                action="word-patch",
                version=4,
                base_version=3,
                event_fingerprint="b" * 64,
                package_sha256="c" * 64,
                observed_at=ogent.now_iso(),
            )
            refresh_ack = ogent.PreviewAck(
                kind="refresh",
                client_id=client,
                document_id=session.document_id,
                watch_generation=session.watch_generation,
                version=4,
                event_fingerprint=mutation.event_fingerprint,
                package_sha256="c" * 64,
                dom_fingerprint="d" * 64,
                control_id="e" * 32,
                viewport_path=None,
                acknowledged_at=ogent.now_iso(),
            )
            with (
                mock.patch.object(
                    session.preview_sync,
                    "wait_for_mutation_confirmation",
                    return_value=ogent.PreviewConfirmation(
                        False,
                        "waiting",
                        "not confirmed",
                        mutation=mutation,
                    ),
                ),
                mock.patch.object(
                    session.preview_sync,
                    "enqueue_control",
                    return_value={"_ogent": {"control_id": "e" * 32}},
                ),
                mock.patch.object(
                    session.preview_sync,
                    "wait_for_control_ack",
                    return_value=ogent.PreviewConfirmation(
                        True,
                        "recovered",
                        "Preview updated",
                        ack=refresh_ack,
                    ),
                ),
                mock.patch.object(ogent, "start_watch") as start_watch,
            ):
                result = ogent.confirm_word_preview(
                    session,
                    document,
                    baseline,
                    "c" * 64,
                    expected_generation=1,
                )
            self.assertTrue(result.confirmed)
            self.assertEqual(result.recovery, "full_refresh")
            start_watch.assert_not_called()
            self.assertEqual(session.preview_update_status, "updated")

    def test_failed_refresh_restarts_once_and_restores_semantic_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = Path(temporary) / "restart.docx"
            document.write_bytes(b"updated")
            session = ogent.SessionState("preview-restart")
            document_id = "document-12345678"
            old_generation = "1" * 32
            new_generation = "2" * 32
            client = "client-12345678"
            with session.lock:
                session.active_doc = document
                session.document_id = document_id
                session.watch_generation = old_generation
            session.preview_sync.activate_watch(document_id, old_generation)
            session.preview_sync.register_client(
                client_id=client,
                document_id=document_id,
                watch_generation=old_generation,
            )
            baseline = session.preview_sync.begin_run(
                package_sha256="a" * 64,
                client_id=client,
            )
            mutation = ogent.WatchMutation(
                1,
                document_id,
                old_generation,
                "word-patch",
                9,
                8,
                "b" * 64,
                "c" * 64,
                ogent.now_iso(),
            )
            viewport_ack = ogent.PreviewAck(
                "viewport",
                client,
                document_id,
                old_generation,
                0,
                None,
                "c" * 64,
                "d" * 64,
                "f" * 32,
                "/body/p[42]",
                ogent.now_iso(),
            )
            initial_ack = ogent.PreviewAck(
                "initial",
                client,
                document_id,
                new_generation,
                0,
                None,
                "c" * 64,
                "e" * 64,
                None,
                None,
                ogent.now_iso(),
            )

            controls = [
                {"_ogent": {"control_id": "e" * 32}},
                {"_ogent": {"control_id": "f" * 32}},
            ]
            confirmations = [
                ogent.PreviewConfirmation(
                    False,
                    "waiting",
                    "refresh failed",
                ),
                ogent.PreviewConfirmation(
                    True,
                    "captured",
                    "captured",
                    ack=viewport_ack,
                ),
            ]

            def restart(
                current: ogent.SessionState,
                _document: Path,
            ) -> None:
                with current.lock:
                    current.watch_generation = new_generation
                current.preview_sync.activate_watch(
                    document_id,
                    new_generation,
                )

            with (
                mock.patch.object(
                    session.preview_sync,
                    "wait_for_mutation_confirmation",
                    return_value=ogent.PreviewConfirmation(
                        False,
                        "waiting",
                        "not confirmed",
                        mutation=mutation,
                    ),
                ),
                mock.patch.object(
                    session.preview_sync,
                    "enqueue_control",
                    side_effect=controls,
                ),
                mock.patch.object(
                    session.preview_sync,
                    "wait_for_control_ack",
                    side_effect=confirmations,
                ),
                mock.patch.object(
                    session.preview_sync,
                    "wait_for_initial_ack",
                    return_value=initial_ack,
                ),
                mock.patch.object(
                    ogent,
                    "start_watch",
                    side_effect=restart,
                ) as start_watch,
                mock.patch.object(
                    ogent,
                    "run_quiet",
                    return_value=subprocess.CompletedProcess(
                        [],
                        0,
                        "{}",
                        "",
                    ),
                ) as run_quiet,
            ):
                result = ogent.confirm_word_preview(
                    session,
                    document,
                    baseline,
                    "c" * 64,
                    expected_generation=1,
                )

            self.assertTrue(result.confirmed)
            self.assertEqual(result.recovery, "watch_restart")
            start_watch.assert_called_once_with(session, document)
            self.assertIn(
                [
                    "officecli",
                    "watch",
                    "goto",
                    str(document),
                    "/body/p[42]",
                    "--json",
                ],
                [list(call.args[0]) for call in run_quiet.call_args_list],
            )

    def test_relay_script_uses_named_updates_and_semantic_dom_confirmation(
        self,
    ) -> None:
        session = ogent.SessionState("relay-test")
        with session.lock:
            session.active_doc = Path("relay.docx")
            session.document_id = "document-12345678"
            session.watch_port = 26320
            session.watch_generation = "1" * 32
        session.preview_sync.activate_watch(
            session.document_id,
            session.watch_generation,
        )
        channel = session.preview_sync.register_client(
            client_id="client-12345678",
            document_id=session.document_id,
            watch_generation=session.watch_generation,
        )
        rendered = ogent.rewrite_preview_html(
            (
                "<html><head><style>p{color:red}</style></head><body>"
                "<p data-path='/body/p[1]'>old</p>"
                "<script>new EventSource('/events');fetch('/')</script>"
                "</body></html>"
            ),
            session,
            "client-12345678",
            channel,
            "a" * 64,
        )
        self.assertIn('source.addEventListener("update"', rendered)
        self.assertNotIn("source.onmessage", rendered)
        self.assertIn("semanticFingerprint", rendered)
        self.assertIn("currentDom === expectedDom", rendered)
        self.assertIn("renderedCanonical", rendered)
        self.assertIn("canonical=1", rendered)
        self.assertNotIn("new DOMParser()", rendered)
        self.assertIn(".officecli-mark,.cjk-done", rendered)
        self.assertIn('item.style.marginRight === "-0.2em"', rendered)
        self.assertIn("canonical_dom_fingerprint: canonicalDom", rendered)
        self.assertIn('confirmCanonical("mutation", message)', rendered)
        self.assertNotIn("confirmSynchronousMutation", rendered)
        for action in (
            '"add"',
            '"full"',
            '"remove"',
            '"replace"',
            '"word-patch"',
        ):
            self.assertIn(action, rendered)
        self.assertIn("/preview/events?", rendered)
        self.assertIn("/preview/control?", rendered)
        self.assertIn("/preview/ack?", rendered)
        self.assertNotIn("window.confirm", ogent.HTML_TEMPLATE)
        self.assertIn("Start a new chat?", ogent.HTML_TEMPLATE)
        self.assertIn("+ New chat", ogent.HTML_TEMPLATE)

        comparison_only = ogent.rewrite_preview_html(
            "<html><body><p data-path='/body/p[1]'>proof</p></body></html>",
            session,
            "client-12345678",
            channel,
            "a" * 64,
            document_id=session.document_id,
            watch_generation=session.watch_generation,
            comparison_only=True,
        )
        self.assertIn('"comparisonOnly":true', comparison_only)
        self.assertLess(
            comparison_only.index("if (config.comparisonOnly) return;"),
            comparison_only.index("const source = window._watchEs;"),
        )


if __name__ == "__main__":
    unittest.main()

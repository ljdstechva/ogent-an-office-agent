from __future__ import annotations

import http.client
import shutil
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ogent
from ogent_app.domain.run import RunMode, ScopeMode
from ogent_app.domain.workspace import RunState
from ogent_app.infrastructure.sqlite import (
    ContentAddressedBlobStore,
    LegacyImportSummary,
    LegacyMetadataImporter,
    MetadataRepository,
    SqliteDatabase,
    TurnRepository,
    WorkspaceRepository,
)


class LegacyImportTests(unittest.TestCase):
    def test_recents_backups_and_session_memory_import_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recent_path = root / "recent.json"
            backup_root = root / "backups"
            memory_root = root / "session-memory"
            recent_path.write_text(
                json.dumps([str(root / "report.docx")]),
                encoding="utf-8",
            )
            backup_id = "a" * 32
            backup_directory = backup_root / backup_id
            backup_directory.mkdir(parents=True)
            manifest = {
                "schema_version": 1,
                "backup_id": backup_id,
                "backup_file": "report.docx",
                "source_path": str(root / "report.docx"),
                "source_name": "report.docx",
                "extension": ".docx",
                "sha256": "b" * 64,
                "byte_size": 42,
                "application_version": "0.10.2",
                "created_at": "2026-07-29T00:00:00+00:00",
                "expires_at": "2026-08-29T00:00:00+00:00",
                "pending_delete": False,
                "delete_error": None,
            }
            (backup_directory / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            memory_directory = memory_root / ("c" * 32) / "cafebabe"
            memory_directory.mkdir(parents=True)
            exact = "  legacy\r\n" + ("x" * 200_000) + "\r\n  "
            (memory_directory / "memory.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": "cafebabe",
                        "turns": [
                            {
                                "sequence": 1,
                                "role": "user",
                                "text": exact,
                                "timestamp": "2026-07-29T00:00:01+00:00",
                                "provider": "codex",
                                "model": "fixture",
                                "preview_selections": [{"path": "/body/p[1]"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            database = SqliteDatabase(root / "ogent.db")
            blobs = ContentAddressedBlobStore(root / "blobs")
            workspaces = WorkspaceRepository(database)
            turns = TurnRepository(database, blobs)
            metadata = MetadataRepository(database)
            importer = LegacyMetadataImporter(
                database,
                workspaces,
                turns,
                metadata,
            )

            first = importer.import_all(
                recent_path=recent_path,
                backup_root=backup_root,
                session_memory_root=memory_root,
            )
            second = importer.import_all(
                recent_path=recent_path,
                backup_root=backup_root,
                session_memory_root=memory_root,
            )

            self.assertEqual(
                first,
                LegacyImportSummary(
                    recent_documents=1,
                    recovery_backups=1,
                    workspaces=1,
                    turns=1,
                ),
            )
            self.assertEqual(second, LegacyImportSummary())
            page = turns.page("cafebabe")
            self.assertEqual(len(page.items), 1)
            self.assertEqual(turns.raw_content(page.items[0].turn_id), exact)
            self.assertEqual(
                page.items[0].metadata["preview_selections"][0]["path"],
                "/body/p[1]",
            )
            self.assertEqual(
                tuple(Path(item).resolve() for item in metadata.recent_documents()),
                ((root / "report.docx").resolve(),),
            )
            self.assertEqual(metadata.recovery_count(), 1)


class ResumeRuntimeTests(unittest.TestCase):
    def test_read_only_partition_resume_seeds_new_run_from_manifest(
        self,
    ) -> None:
        session = ogent.SessionState("cafebabe")
        with session.lock:
            session.document_index = {"revision_id": "revision-1"}
        checkpoint = {
            "revision_id": "revision-1",
            "partition_manifest_blob_id": "a" * 64,
            "completed_partitions": 2,
            "next_partition": 3,
            "partition_count": 5,
        }
        run_repository = mock.Mock()
        run_repository.get.return_value = SimpleNamespace(
            workspace_id=session.session_id,
            state=RunState.FAILED,
            scope=ScopeMode.WHOLE_DOCUMENT,
            mode=RunMode.REVIEW,
            request_turn_id="turn-1",
        )
        run_repository.steps.return_value = (
            SimpleNamespace(
                step=SimpleNamespace(step_id="execute"),
                checkpoint=checkpoint,
            ),
        )
        turn_repository = mock.Mock()
        turn_repository.get.return_value = SimpleNamespace(
            turn_id="turn-1",
            provider="codex",
            model="fixture",
            effort="automatic",
            metadata={},
        )
        turn_repository.raw_content.return_value = "Review the whole document."
        selection = ogent.AgentSelection(
            provider_id="codex",
            model="fixture",
            effort="automatic",
        )
        with (
            mock.patch.object(
                ogent,
                "RUN_REPOSITORY",
                run_repository,
            ),
            mock.patch.object(
                ogent,
                "TURN_REPOSITORY",
                turn_repository,
            ),
            mock.patch.object(
                ogent,
                "validate_agent_selection",
                return_value=selection,
            ),
            mock.patch.object(
                ogent,
                "start_agent_run",
                return_value="b" * 32,
            ) as starter,
        ):
            result = ogent.resume_agent_run(
                session,
                "c" * 32,
                client_id="client-1234",
            )

        self.assertEqual(result["run_id"], "b" * 32)
        self.assertEqual(result["completed_partitions"], 2)
        starter.assert_called_once_with(
            session,
            "Review the whole document.",
            "fixture",
            "automatic",
            "codex",
            selection=selection,
            client_id="client-1234",
            resume_checkpoint=checkpoint,
        )


@unittest.skipUnless(shutil.which("officecli"), "OfficeCLI is not installed")
class DurableRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_state = ogent.STATE
        self.patch = mock.patch.multiple(
            ogent,
            BACKUP_STORE=ogent.BackupStore(
                self.root / "backups",
                application_version=ogent.APP_VERSION,
            ),
            SESSION_MEMORY_STORE=ogent.SessionMemoryStore(self.root / "session-memory"),
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
            LAST_LEGACY_IMPORT=LegacyImportSummary(),
        )
        self.patch.start()
        self.state = ogent.OgentState()
        ogent.STATE = self.state

    def tearDown(self) -> None:
        self.state.shutdown_requested = True
        if ogent.WORKSPACE_ACTORS is not None:
            ogent.WORKSPACE_ACTORS.stop_all()
        ogent.STATE = self.original_state
        self.patch.stop()
        self.temporary.cleanup()

    def test_restart_restores_lossless_history_without_snapshot_retransmit(
        self,
    ) -> None:
        session = self.state.create_session()
        exact = "  START\r\n" + ("z" * 199_984) + "\r\nEND  "
        session.add_message("user", exact, provider="codex", model="fixture")
        session.add_message(
            "assistant",
            "Persistent answer",
            provider="codex",
            model="fixture",
        )
        session_id = session.session_id
        snapshot = session.public_snapshot(include_watch_probe=False)

        self.assertEqual(snapshot["transcript"], [])
        self.assertTrue(snapshot["transcript_paged"])
        self.assertEqual(snapshot["transcript_total"], 2)
        replay_json = json.dumps(session.current_events_after(0))
        self.assertNotIn(exact, replay_json)

        assert ogent.WORKSPACE_ACTORS is not None
        ogent.WORKSPACE_ACTORS.stop_all()
        ogent.SESSION_MEMORY_STORE.clear_all()
        ogent.SERVICES_INITIALIZED = False
        restarted = ogent.OgentState()
        ogent.STATE = restarted
        self.state = restarted

        restored = restarted.get_session(session_id)
        self.assertEqual(
            [item["text"] for item in restored.transcript],
            [exact, "Persistent answer"],
        )
        self.assertEqual(
            restored.public_snapshot(include_watch_probe=False)["transcript"],
            [],
        )

    def test_authenticated_paged_route_and_sse_replay_are_content_safe(
        self,
    ) -> None:
        session = self.state.create_session()
        secret = "PRIVATE-" + ("q" * 20_000)
        session.add_message("user", secret)
        session.add_message("assistant", "Done")
        server = ogent.OgentServer((ogent.HOST, 0), ogent.OgentHandler)
        port = int(server.server_address[1])
        self.state.server_port = port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(ogent.HOST, port, timeout=5)
            connection.request(
                "GET",
                (
                    f"/api/workspaces/{session.session_id}/turns"
                    f"?s={session.session_id}&direction=tail&limit=1"
                ),
                headers={"X-Ogent-Token": self.state.token},
            )
            response = connection.getresponse()
            page = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(page["total"], 2)
            self.assertEqual(page["items"][0]["text"], "Done")

            sse = http.client.HTTPConnection(ogent.HOST, port, timeout=5)
            sse.request(
                "GET",
                (
                    f"/events?s={session.session_id}"
                    f"&token={self.state.token}&client=phase2-replay"
                ),
                headers={"Last-Event-ID": "1"},
            )
            stream = sse.getresponse()
            self.assertEqual(stream.status, 200)
            blocks: list[str] = []
            current: list[str] = []
            while len(blocks) < 4:
                line = stream.readline().decode("utf-8")
                if not line:
                    break
                if line in {"\n", "\r\n"}:
                    if current:
                        blocks.append("".join(current))
                        current = []
                    if blocks and '"type":"snapshot"' in blocks[-1]:
                        break
                    continue
                current.append(line)
            sse.close()
            joined = "".join(blocks)
            self.assertIn('"type":"snapshot"', joined)
            self.assertNotIn(secret, joined)
            snapshot_payload = next(
                block for block in blocks if '"type":"snapshot"' in block
            )
            self.assertIn('"transcript":[]', snapshot_payload)
        finally:
            self.state.shutdown_requested = True
            with session.condition:
                session.condition.notify_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()

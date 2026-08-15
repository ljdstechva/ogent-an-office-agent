from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


from ogent_app.infrastructure.sqlite import (
    ContentAddressedBlobStore,
    EventRepository,
    RunRepository,
    SqliteDatabase,
    TurnRepository,
    WorkspaceRepository,
)


class WorkspacePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = SqliteDatabase(self.root / "ogent.db")
        self.blobs = ContentAddressedBlobStore(self.root / "blobs")
        self.database.initialize()
        self.workspaces = WorkspaceRepository(self.database)
        self.turns = TurnRepository(self.database, self.blobs)
        self.runs = RunRepository(self.database)
        self.events = EventRepository(self.database)
        self.workspaces.create("workspace-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_schema_uses_wal_and_contains_principal_tables(self) -> None:
        with self.database.reader() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }

        self.assertEqual(str(journal_mode).casefold(), "wal")
        self.assertTrue(
            {
                "workspaces",
                "documents",
                "document_revisions",
                "document_nodes",
                "document_edges",
                "document_chunks",
                "document_chunks_fts",
                "turns",
                "runs",
                "run_steps",
                "run_events",
                "tool_receipts",
                "changesets",
                "attachments",
            }.issubset(tables)
        )

    def test_lossless_200k_turn_survives_repository_restart(self) -> None:
        raw = "  START\r\n" + ("x" * 199_984) + "\r\nEND  "
        self.assertEqual(len(raw), 200_000)

        turn = self.turns.append(
            "workspace-1",
            "user",
            raw,
            provider="codex",
            model="fixture",
            effort="high",
        )
        restarted_database = SqliteDatabase(self.root / "ogent.db")
        restarted_turns = TurnRepository(
            restarted_database,
            ContentAddressedBlobStore(self.root / "blobs"),
        )

        self.assertEqual(turn.character_count, 200_000)
        self.assertLess(len(turn.display_excerpt), len(raw))
        self.assertEqual(restarted_turns.raw_content(turn.turn_id), raw)

    def test_transcript_retrieval_is_paged_and_cursor_based(self) -> None:
        for index in range(123):
            self.turns.append(
                "workspace-1",
                "user" if index % 2 == 0 else "assistant",
                f"turn-{index:03}",
            )

        first = self.turns.page("workspace-1", limit=50)
        second = self.turns.page(
            "workspace-1",
            after_sequence=first.next_sequence or 0,
            limit=50,
        )
        third = self.turns.page(
            "workspace-1",
            after_sequence=second.next_sequence or 0,
            limit=50,
        )

        self.assertEqual(len(first.items), 50)
        self.assertEqual(len(second.items), 50)
        self.assertEqual(len(third.items), 23)
        self.assertIsNone(third.next_sequence)
        sequences = [
            item.sequence for page in (first, second, third) for item in page.items
        ]
        self.assertEqual(sequences, list(range(1, 124)))

    def test_event_replay_is_bounded_and_does_not_include_turn_content(self) -> None:
        raw_secret = "PRIVATE-DOCUMENT-CONTENT"
        self.turns.append("workspace-1", "user", raw_secret)
        for index in range(8):
            self.events.append(
                "workspace-1",
                "run.progress",
                {"step": index, "status": "working"},
            )

        replay = self.events.replay(
            "workspace-1",
            after_sequence=3,
            limit=3,
        )

        self.assertEqual([event.sequence for event in replay], [4, 5, 6])
        self.assertNotIn(
            raw_secret,
            "".join(str(event.payload) for event in replay),
        )

    def test_blob_reads_verify_content_integrity(self) -> None:
        reference = self.blobs.put_text("verified")
        target = self.blobs.root / reference.relative_path
        target.write_bytes(b"tampered")

        with self.assertRaisesRegex(OSError, "integrity"):
            self.blobs.read_text(reference)


if __name__ == "__main__":
    unittest.main()

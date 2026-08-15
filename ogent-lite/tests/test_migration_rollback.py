from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ogent_app.infrastructure.sqlite import (
    MigrationBackupError,
    SqliteDatabase,
)
from ogent_app.infrastructure.sqlite.migrations import SCHEMA_VERSION


INTRODUCED_OBJECTS = {
    1: ("table", "documents"),
    2: ("table", "recovery_backups"),
    3: ("table", "capability_receipts"),
    4: ("table", "index_jobs"),
    5: ("column", ("run_steps", "checkpoint_json")),
    6: ("table", "reference_indexes"),
    7: ("table", "changeset_undos"),
}


def schema_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def object_exists(path: Path, kind: str, identifier: object) -> bool:
    connection = sqlite3.connect(path)
    try:
        if kind == "table":
            return (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (str(identifier),),
                ).fetchone()
                is not None
            )
        table, column = identifier
        return any(
            str(row[1]) == column
            for row in connection.execute(f"PRAGMA table_info({table})")
        )
    finally:
        connection.close()


class MigrationRollbackTests(unittest.TestCase):
    def test_every_forward_migration_restores_its_exact_prior_snapshot(
        self,
    ) -> None:
        self.assertEqual(set(INTRODUCED_OBJECTS), set(range(1, SCHEMA_VERSION + 1)))
        for version in range(1, SCHEMA_VERSION + 1):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as temporary,
            ):
                path = Path(temporary) / "state.sqlite3"
                previous = SqliteDatabase(
                    path,
                    target_version=version - 1,
                    migration_backups=False,
                )
                previous.initialize()
                with previous.transaction() as connection:
                    connection.execute(
                        "CREATE TABLE rollback_probe(value TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO rollback_probe(value) VALUES ('preserved')"
                    )
                backup = previous.create_migration_backup(version)

                upgraded = SqliteDatabase(path, target_version=version)
                upgraded.initialize()
                self.assertEqual(schema_version(path), version)
                kind, identifier = INTRODUCED_OBJECTS[version]
                self.assertTrue(object_exists(path, kind, identifier))

                upgraded.rollback_migration(backup)
                self.assertEqual(schema_version(path), version - 1)
                self.assertFalse(object_exists(path, kind, identifier))
                connection = sqlite3.connect(path)
                try:
                    self.assertEqual(
                        connection.execute(
                            "SELECT value FROM rollback_probe"
                        ).fetchone()[0],
                        "preserved",
                    )
                    self.assertEqual(
                        connection.execute("PRAGMA integrity_check").fetchone()[0],
                        "ok",
                    )
                finally:
                    connection.close()

    def test_restore_rejects_a_tampered_migration_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            database = SqliteDatabase(
                path,
                target_version=1,
                migration_backups=False,
            )
            database.initialize()
            backup = database.create_migration_backup(2)
            with backup.backup_path.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaises(MigrationBackupError):
                database.rollback_migration(backup)
            self.assertEqual(schema_version(path), 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

import ogent_backup_store as backup_module  # noqa: E402
from ogent_backup_store import (  # noqa: E402
    MANIFEST_SCHEMA_VERSION,
    BackupError,
    BackupStore,
)


class MutableClock:
    def __init__(self, value: dt.datetime) -> None:
        self.value = value

    def __call__(self) -> dt.datetime:
        return self.value


class BackupStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "backups"
        self.source = Path(self.temporary.name) / "Synthetic Report.docx"
        self.source.write_bytes(b"synthetic-office-package-content")
        self.created = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
        self.clock = MutableClock(self.created)
        self.store = BackupStore(
            self.root,
            application_version="0.10.0",
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_verified_atomic_backup_is_physical_and_manifested(self) -> None:
        record = self.store.create_backup(self.source)

        expected_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertEqual(record.sha256, expected_hash)
        self.assertEqual(
            hashlib.sha256(record.backup_path.read_bytes()).hexdigest(),
            expected_hash,
        )
        self.assertFalse(os.path.samefile(self.source, record.backup_path))
        self.assertEqual(record.backup_path.read_bytes(), self.source.read_bytes())
        manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], MANIFEST_SCHEMA_VERSION)
        self.assertEqual(manifest["source_path"], str(self.source.resolve()))
        self.assertEqual(manifest["application_version"], "0.10.0")
        self.assertEqual(
            backup_module.parse_utc(manifest["expires_at"])
            - backup_module.parse_utc(manifest["created_at"]),
            dt.timedelta(days=30),
        )
        self.assertFalse(any(self.root.rglob("*.partial")))

    def test_source_change_during_copy_aborts_without_committed_backup(self) -> None:
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with mock.patch.object(
            self.store,
            "_hash_file",
            side_effect=[digest, digest, "0" * 64],
        ):
            with self.assertRaisesRegex(BackupError, "changed during backup"):
                self.store.create_backup(self.source)

        committed = [
            item
            for item in self.root.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        ]
        self.assertEqual(committed, [])
        self.assertEqual(self.source.read_bytes(), b"synthetic-office-package-content")

    def test_read_only_or_locked_source_fails_before_copy(self) -> None:
        with mock.patch.object(
            backup_module.os,
            "open",
            side_effect=PermissionError("locked"),
        ):
            with self.assertRaisesRegex(BackupError, "cannot be opened for writing"):
                self.store.create_backup(self.source)
        self.assertEqual(self.store.list_records(), [])

    def test_insufficient_space_fails_before_copy(self) -> None:
        store = BackupStore(
            self.root,
            application_version="0.10.0",
            clock=self.clock,
            disk_usage=lambda _: types.SimpleNamespace(free=1),
        )
        with self.assertRaisesRegex(BackupError, "not enough free space"):
            store.create_backup(self.source)
        self.assertEqual(store.list_records(), [])

    def test_29_day_backup_is_retained_and_exactly_30_day_backup_expires(self) -> None:
        record = self.store.create_backup(self.source)
        self.clock.value = self.created + dt.timedelta(days=29)
        result_29 = self.store.cleanup_expired(reason="test-29")
        self.assertEqual(result_29["deleted"], 0)
        self.assertTrue(record.backup_path.exists())

        self.clock.value = self.created + dt.timedelta(days=30)
        result_30 = self.store.cleanup_expired(reason="test-30")
        self.assertEqual(result_30["deleted"], 1)
        self.assertFalse(record.backup_path.exists())

    def test_failed_deletion_records_pending_state_and_retries(self) -> None:
        record = self.store.create_backup(self.source)
        self.clock.value = self.created + dt.timedelta(days=30)
        original_delete = self.store._delete_directory_contents
        with mock.patch.object(
            self.store,
            "_delete_directory_contents",
            side_effect=PermissionError("file is locked"),
        ):
            result = self.store.cleanup_expired(reason="locked")

        self.assertEqual(result["pending_delete"], 1)
        retained = self.store.list_records()
        self.assertEqual(len(retained), 1)
        self.assertTrue(retained[0].pending_delete)
        self.assertIn("locked", retained[0].delete_error or "")

        with mock.patch.object(
            self.store,
            "_delete_directory_contents",
            side_effect=original_delete,
        ):
            retry = self.store.cleanup_expired(reason="retry")
        self.assertEqual(retry["deleted"], 1)
        self.assertFalse(record.manifest_path.exists())

    def test_root_containment_refuses_outside_paths_and_unsafe_manifest(self) -> None:
        outside = Path(self.temporary.name) / "outside.docx"
        outside.write_bytes(b"outside")
        with self.assertRaisesRegex(BackupError, "outside"):
            self.store._require_contained(outside, "test")

        unsafe_dir = self.root / ("a" * 32)
        unsafe_dir.mkdir(parents=True)
        (unsafe_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "backup_id": "a" * 32,
                    "backup_file": "../outside.docx",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BackupError, "unsafe backup path"):
            self.store._record_from_directory(unsafe_dir)
        self.assertTrue(outside.exists())

    def test_summary_and_restore_are_verified(self) -> None:
        first = self.store.create_backup(self.source)
        self.clock.value = self.created + dt.timedelta(hours=1)
        second_source = Path(self.temporary.name) / "Workbook.xlsx"
        second_source.write_bytes(b"synthetic-workbook-content")
        second = self.store.create_backup(second_source)

        summary = self.store.summary()
        self.assertEqual(summary["folder"], str(self.root.resolve()))
        self.assertEqual(summary["retention_days"], 30)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(
            summary["total_size"],
            first.byte_size + second.byte_size,
        )
        self.assertEqual(summary["oldest_created_at"], first.created_at)
        self.assertEqual(summary["newest_created_at"], second.created_at)

        restored = Path(self.temporary.name) / "restored.docx"
        self.store.restore_backup(first.backup_id, restored)
        self.assertEqual(restored.read_bytes(), self.source.read_bytes())

    def test_initialize_uses_injected_clock_and_schedules_six_hour_cleanup(self) -> None:
        result = self.store.initialize()
        self.assertEqual(result["reason"], "startup")
        self.assertEqual(
            self.store.next_cleanup_at,
            self.created + dt.timedelta(hours=6),
        )
        self.clock.value = self.created + dt.timedelta(hours=5, minutes=59)
        self.assertIsNone(self.store.cleanup_if_due())
        self.clock.value = self.created + dt.timedelta(hours=6)
        self.assertEqual(
            self.store.cleanup_if_due()["reason"],
            "scheduled",
        )


if __name__ == "__main__":
    unittest.main()

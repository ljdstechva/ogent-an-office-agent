"""Integrity-checked SQLite snapshots for release migration rollback."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import sqlite3
import uuid
from pathlib import Path


class MigrationBackupError(RuntimeError):
    """A migration snapshot is missing, unsafe, or corrupt."""


@dataclasses.dataclass(frozen=True, slots=True)
class MigrationBackup:
    database_path: Path
    backup_path: Path
    from_version: int
    to_version: int
    sha256: str
    byte_size: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MigrationBackupStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve(strict=False)

    def create(
        self,
        source: sqlite3.Connection,
        *,
        from_version: int,
        to_version: int,
    ) -> MigrationBackup:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        target = self._backup_path(from_version, to_version)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
        try:
            self._verify_database(temporary, from_version)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return MigrationBackup(
            database_path=self.database_path,
            backup_path=target,
            from_version=int(from_version),
            to_version=int(to_version),
            sha256=_sha256(target),
            byte_size=target.stat().st_size,
        )

    def restore(self, backup: MigrationBackup) -> None:
        expected = self._backup_path(
            backup.from_version,
            backup.to_version,
        )
        actual = Path(backup.backup_path).resolve(strict=True)
        if actual != expected or actual.is_symlink():
            raise MigrationBackupError(
                "The migration backup is outside the expected rollback path."
            )
        if backup.database_path.resolve(strict=False) != self.database_path:
            raise MigrationBackupError(
                "The migration backup belongs to a different database."
            )
        if actual.stat().st_size != backup.byte_size:
            raise MigrationBackupError("The migration backup size changed.")
        if _sha256(actual) != backup.sha256:
            raise MigrationBackupError(
                "The migration backup failed integrity verification."
            )
        self._verify_database(actual, backup.from_version)
        temporary = self.database_path.with_name(
            f".{self.database_path.name}.{uuid.uuid4().hex}.rollback"
        )
        try:
            with actual.open("rb") as source, temporary.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if _sha256(temporary) != backup.sha256:
                raise MigrationBackupError(
                    "The restored migration copy failed integrity verification."
                )
            self._remove_sidecars()
            os.replace(temporary, self.database_path)
        finally:
            temporary.unlink(missing_ok=True)
        self._verify_database(self.database_path, backup.from_version)

    def _backup_path(self, from_version: int, to_version: int) -> Path:
        name = (
            f"{self.database_path.stem}.pre-migration-"
            f"v{int(from_version)}-to-v{int(to_version)}.sqlite3"
        )
        return (self.database_path.parent / name).resolve(strict=False)

    def _remove_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.database_path}{suffix}")
            if sidecar.exists():
                sidecar.resolve(strict=True).relative_to(self.database_path.parent)
                sidecar.unlink()

    @staticmethod
    def _verify_database(path: Path, expected_version: int) -> None:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
        )
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.casefold() != "ok":
                raise MigrationBackupError(
                    "The migration backup failed SQLite integrity checking."
                )
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            version = (
                int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                    ).fetchone()[0]
                )
                if table is not None
                else 0
            )
            if version != int(expected_version):
                raise MigrationBackupError(
                    "The migration backup schema version is not the expected "
                    "rollback version."
                )
        finally:
            connection.close()

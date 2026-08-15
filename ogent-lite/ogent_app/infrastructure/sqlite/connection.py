"""SQLite connection, WAL configuration, migrations, and transactions."""

from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

from ogent_app.infrastructure.fault_injection import FaultInjector, FaultPoint

from .migration_backup import MigrationBackup, MigrationBackupStore
from .migrations import MIGRATIONS, SCHEMA_VERSION


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class SqliteDatabase:
    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 5.0,
        fault_injector: FaultInjector | None = None,
        target_version: int | None = None,
        migration_backups: bool = True,
    ) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.fault_injector = fault_injector
        self.target_version = int(
            SCHEMA_VERSION if target_version is None else target_version
        )
        if not 0 <= self.target_version <= SCHEMA_VERSION:
            raise ValueError("Unsupported target schema version.")
        self.migration_backups = bool(migration_backups)
        self.migration_backup_store = MigrationBackupStore(self.path)
        self.last_migration_backup: MigrationBackup | None = None
        self.initialization_lock = threading.RLock()
        self.initialized = False

    def connect(self) -> sqlite3.Connection:
        if self.fault_injector is not None and self.fault_injector.consume(
            FaultPoint.DATABASE_LOCK
        ):
            raise sqlite3.OperationalError("database is locked")
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        with self.initialization_lock:
            if self.initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self.connect()
            try:
                mode = str(
                    connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                ).casefold()
                if mode != "wal":
                    raise RuntimeError("SQLite WAL mode could not be enabled.")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                applied = {
                    int(row["version"])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    )
                }
                current = max(applied, default=0)
                if current > self.target_version:
                    raise RuntimeError(
                        f"Ogent schema version {current} is newer than this "
                        f"runtime target {self.target_version}."
                    )
                pending = [
                    (version, script)
                    for version, script in MIGRATIONS
                    if current < version <= self.target_version
                ]
                if pending and current > 0 and self.migration_backups:
                    self.last_migration_backup = self.migration_backup_store.create(
                        connection,
                        from_version=current,
                        to_version=self.target_version,
                    )
                for version, script in pending:
                    timestamp = utc_now_iso().replace("'", "''")
                    try:
                        connection.executescript(
                            "BEGIN IMMEDIATE;\n"
                            f"{script}\n"
                            "INSERT INTO schema_migrations(version, applied_at) "
                            f"VALUES ({int(version)}, '{timestamp}');\n"
                            "COMMIT;"
                        )
                    except BaseException:
                        if connection.in_transaction:
                            connection.rollback()
                        raise
                current = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
                if int(current) != self.target_version:
                    raise RuntimeError(f"Unexpected Ogent schema version {current}.")
            finally:
                connection.close()
            self.initialized = True

    def create_migration_backup(
        self,
        to_version: int,
    ) -> MigrationBackup:
        self.initialize()
        with self.reader() as connection:
            current = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
            return self.migration_backup_store.create(
                connection,
                from_version=current,
                to_version=int(to_version),
            )

    def rollback_migration(self, backup: MigrationBackup) -> None:
        self.migration_backup_store.restore(backup)
        self.target_version = int(backup.from_version)
        self.initialized = False

    @contextlib.contextmanager
    def transaction(
        self,
        *,
        immediate: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextlib.contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

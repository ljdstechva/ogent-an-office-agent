"""Per-run, content-addressed Office package rollback."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from ogent_app.domain.verification import RollbackSnapshot
from ogent_app.infrastructure.sqlite import (
    ChangesetRepository,
    ContentAddressedBlobStore,
    SqliteDatabase,
)
from ogent_app.infrastructure.sqlite.connection import utc_now_iso


class RollbackError(RuntimeError):
    pass


class RollbackManager:
    def __init__(
        self,
        database: SqliteDatabase,
        blobs: ContentAddressedBlobStore,
        changesets: ChangesetRepository,
    ) -> None:
        self.database = database
        self.blobs = blobs
        self.changesets = changesets

    def create(self, document: Path, *, run_id: str) -> RollbackSnapshot:
        active = Path(document).resolve(strict=True)
        before = active.stat()
        payload = active.read_bytes()
        after = active.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise RollbackError(
                "The document changed while Ogent created its run snapshot."
            )
        blob = self.blobs.put_bytes(
            payload,
            media_type=("application/vnd.openxmlformats-officedocument"),
        )
        timestamp = utc_now_iso()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO blobs("
                "id, sha256, byte_size, media_type, relative_path, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    blob.blob_id,
                    blob.sha256,
                    blob.byte_size,
                    blob.media_type,
                    blob.relative_path,
                    timestamp,
                ),
            )
        return RollbackSnapshot(
            run_id=run_id,
            document=active,
            blob_id=blob.blob_id,
            package_sha256=blob.sha256,
            byte_size=blob.byte_size,
            created_at=timestamp,
        )

    def restore(
        self,
        snapshot: RollbackSnapshot,
        document: Path,
    ) -> str:
        target = Path(document).resolve(strict=True)
        if target != snapshot.document:
            raise RollbackError("The rollback target no longer matches the run.")
        payload = self.blobs.read_bytes(snapshot.blob_id)
        if hashlib.sha256(payload).hexdigest() != snapshot.package_sha256:
            raise RollbackError("The rollback snapshot failed integrity checking.")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.rollback")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        restored = hashlib.sha256(target.read_bytes()).hexdigest()
        if restored != snapshot.package_sha256:
            raise RollbackError("The restored document failed hash verification.")
        return restored

    def record_changeset(
        self,
        snapshot: RollbackSnapshot,
        *,
        post_revision_sha256: str,
        affected_paths: tuple[str, ...],
        assertions: dict[str, object],
    ) -> str:
        return self.changesets.record(
            run_id=snapshot.run_id,
            pre_revision_sha256=snapshot.package_sha256,
            post_revision_sha256=post_revision_sha256,
            affected_paths=affected_paths,
            assertions=assertions,
            rollback_blob_id=snapshot.blob_id,
        )

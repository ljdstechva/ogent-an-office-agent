"""Durable document revisions, bounded index batches, graphs, and FTS."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from ogent_app.domain.document_intelligence import (
    DocumentFormat,
    IndexBatch,
    IndexStatus,
    ObservedRevision,
    StructuralManifest,
)

from .blob_store import ContentAddressedBlobStore
from .connection import SqliteDatabase, utc_now_iso
from .document_repository_constants import (
    INDEX_VERSION,
)
from .document_query_mixin import DocumentQueryMixin
from .document_storage_mixin import DocumentStorageMixin


class StaleIndexAttempt(RuntimeError):
    """An index write came from an attempt that no longer owns the revision."""


class DocumentRepository(DocumentStorageMixin, DocumentQueryMixin):
    def __init__(
        self,
        database: SqliteDatabase,
        blobs: ContentAddressedBlobStore,
    ) -> None:
        self.database = database
        self.blobs = blobs

    @staticmethod
    def canonical_path_key(path: str | Path) -> str:
        return os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))

    def observe(
        self,
        *,
        workspace_id: str,
        source_path: Path | None,
        active_path: Path,
        mode: str,
        document_format: DocumentFormat,
        package_sha256: str,
        quick_manifest: StructuralManifest,
    ) -> ObservedRevision:
        active = Path(active_path).expanduser().resolve(strict=True)
        source = (
            Path(source_path).expanduser().resolve(strict=False)
            if source_path is not None
            else None
        )
        stat = active.stat()
        now = utc_now_iso()
        canonical_key = self.canonical_path_key(active)
        with self.database.transaction() as connection:
            document_row = connection.execute(
                "SELECT * FROM documents WHERE canonical_path_key = ?",
                (canonical_key,),
            ).fetchone()
            if document_row is None:
                document_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO documents("
                    "id, source_path, active_path, mode, format, "
                    "canonical_path_key, backup_id, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        document_id,
                        str(source) if source is not None else None,
                        str(active),
                        mode,
                        document_format.value,
                        canonical_key,
                        now,
                    ),
                )
            else:
                document_id = str(document_row["id"])
                connection.execute(
                    "UPDATE documents SET source_path = ?, active_path = ?, "
                    "mode = ?, format = ? WHERE id = ?",
                    (
                        str(source) if source is not None else None,
                        str(active),
                        mode,
                        document_format.value,
                        document_id,
                    ),
                )
            prior = connection.execute(
                "SELECT revision.* FROM documents "
                "JOIN document_revisions AS revision "
                "ON revision.id = documents.current_revision_id "
                "WHERE documents.id = ? AND revision.package_sha256 = ?",
                (document_id, package_sha256),
            ).fetchone()
            deduplicated = prior is not None
            if prior is None:
                parent = connection.execute(
                    "SELECT current_revision_id FROM documents WHERE id = ?",
                    (document_id,),
                ).fetchone()
                parent_id = (
                    parent["current_revision_id"] if parent is not None else None
                )
                revision_number = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(revision_number), 0) + 1 "
                        "FROM document_revisions WHERE document_id = ?",
                        (document_id,),
                    ).fetchone()[0]
                )
                revision_id = uuid.uuid4().hex
                attempt_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO document_revisions("
                    "id, document_id, revision_number, package_sha256, "
                    "created_at, index_status, index_version, "
                    "parent_revision_id, quick_manifest_json, manifest_json, "
                    "source_size, source_mtime_ns"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)",
                    (
                        revision_id,
                        document_id,
                        revision_number,
                        package_sha256,
                        now,
                        IndexStatus.QUICK_READY.value,
                        INDEX_VERSION,
                        parent_id,
                        self._json(quick_manifest.public()),
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                    ),
                )
                connection.execute(
                    "INSERT INTO index_jobs("
                    "revision_id, attempt_id, attempt_generation, status, "
                    "progress, indexed_nodes, total_estimate, started_at, "
                    "updated_at, error_code"
                    ") VALUES (?, ?, 1, ?, 0.0, 0, ?, NULL, ?, NULL)",
                    (
                        revision_id,
                        attempt_id,
                        IndexStatus.QUICK_READY.value,
                        sum(quick_manifest.counts.values()),
                        now,
                    ),
                )
                if parent_id is not None:
                    connection.execute(
                        "UPDATE index_jobs SET status = ?, updated_at = ? "
                        "WHERE revision_id = ? AND status IN (?, ?)",
                        (
                            IndexStatus.STALE.value,
                            now,
                            parent_id,
                            IndexStatus.QUICK_READY.value,
                            IndexStatus.INDEXING.value,
                        ),
                    )
                    connection.execute(
                        "UPDATE document_revisions SET index_status = ? "
                        "WHERE id = ? AND index_status IN (?, ?)",
                        (
                            IndexStatus.STALE.value,
                            parent_id,
                            IndexStatus.QUICK_READY.value,
                            IndexStatus.INDEXING.value,
                        ),
                    )
            else:
                revision_id = str(prior["id"])
                job = connection.execute(
                    "SELECT * FROM index_jobs WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                attempt_id = (
                    str(job["attempt_id"])
                    if job is not None
                    and IndexStatus(str(job["status"]))
                    in {
                        IndexStatus.QUICK_READY,
                        IndexStatus.INDEXING,
                    }
                    else None
                )
            connection.execute(
                "UPDATE documents SET current_revision_id = ? WHERE id = ?",
                (revision_id, document_id),
            )
            workspace_cursor = connection.execute(
                "UPDATE workspaces SET document_id = ?, last_active_at = ? "
                "WHERE id = ?",
                (document_id, now, workspace_id),
            )
            if workspace_cursor.rowcount != 1:
                raise KeyError(workspace_id)
            document_row = connection.execute(
                "SELECT * FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            revision_row = connection.execute(
                "SELECT * FROM document_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
        assert document_row is not None
        assert revision_row is not None
        return ObservedRevision(
            self._document_record(document_row),
            self._revision_record(revision_row),
            attempt_id,
            deduplicated,
        )

    def requeue(self, revision_id: str) -> str:
        attempt_id = uuid.uuid4().hex
        now = utc_now_iso()
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT documents.current_revision_id "
                "FROM document_revisions "
                "JOIN documents ON documents.id = "
                "document_revisions.document_id "
                "WHERE document_revisions.id = ?",
                (revision_id,),
            ).fetchone()
            if current is None:
                raise KeyError(revision_id)
            if current["current_revision_id"] != revision_id:
                raise StaleIndexAttempt(
                    "Only the current document revision can be requeued."
                )
            connection.execute(
                "DELETE FROM document_nodes WHERE revision_id = ?",
                (revision_id,),
            )
            cursor = connection.execute(
                "UPDATE index_jobs SET attempt_id = ?, "
                "attempt_generation = attempt_generation + 1, "
                "status = ?, progress = 0.0, indexed_nodes = 0, "
                "started_at = NULL, updated_at = ?, error_code = NULL "
                "WHERE revision_id = ?",
                (
                    attempt_id,
                    IndexStatus.QUICK_READY.value,
                    now,
                    revision_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(revision_id)
            connection.execute(
                "UPDATE document_revisions SET index_status = ?, "
                "manifest_json = '{}', indexed_at = NULL, error_code = NULL "
                "WHERE id = ?",
                (IndexStatus.QUICK_READY.value, revision_id),
            )
        return attempt_id

    def append_batch(
        self,
        revision_id: str,
        attempt_id: str,
        batch: IndexBatch,
    ) -> bool:
        with self.database.reader() as connection:
            if not self._attempt_owned(connection, revision_id, attempt_id):
                return False
            reuse_plans = [
                (node, *self._node_reuse_plan(connection, revision_id, node))
                for node in batch.nodes
            ]
        prepared = [
            (
                node,
                (
                    None
                    if not node.text or reuse_text_index
                    else self.blobs.put_text(node.text)
                ),
                origin_id,
                reused_text_blob_id,
                reuse_text_index,
            )
            for node, origin_id, reused_text_blob_id, reuse_text_index in reuse_plans
        ]
        now = utc_now_iso()
        with self.database.transaction() as connection:
            if not self._attempt_owned(
                connection,
                revision_id,
                attempt_id,
            ):
                return False
            connection.execute(
                "UPDATE index_jobs SET status = ?, started_at = "
                "COALESCE(started_at, ?), updated_at = ? "
                "WHERE revision_id = ? AND attempt_id = ?",
                (
                    IndexStatus.INDEXING.value,
                    now,
                    now,
                    revision_id,
                    attempt_id,
                ),
            )
            connection.execute(
                "UPDATE document_revisions SET index_status = ? WHERE id = ?",
                (IndexStatus.INDEXING.value, revision_id),
            )
            inserted = 0
            for (
                node,
                blob,
                origin_id,
                reused_text_blob_id,
                reuse_text_index,
            ) in prepared:
                inserted += self._insert_node(
                    connection,
                    revision_id,
                    node,
                    blob,
                    now,
                    origin_id=origin_id,
                    reused_text_blob_id=reused_text_blob_id,
                    reuse_text_index=reuse_text_index,
                )
            for edge in batch.edges:
                source_id = self._node_id_for_path(
                    connection,
                    revision_id,
                    edge.source_path,
                )
                target_id = self._node_id_for_path(
                    connection,
                    revision_id,
                    edge.target_path,
                )
                if source_id is None or target_id is None:
                    raise ValueError(
                        "An index edge references a node that has not been "
                        f"persisted: {edge.source_path} -> {edge.target_path}"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO document_edges("
                    "id, revision_id, source_node_id, target_node_id, "
                    "edge_type, metadata_json"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self._edge_id(
                            revision_id,
                            source_id,
                            target_id,
                            edge.edge_type,
                        ),
                        revision_id,
                        source_id,
                        target_id,
                        edge.edge_type,
                        self._json(edge.metadata),
                    ),
                )
            cursor = connection.execute(
                "UPDATE index_jobs SET progress = MAX(progress, ?), "
                "indexed_nodes = indexed_nodes + ?, updated_at = ? "
                "WHERE revision_id = ? AND attempt_id = ?",
                (
                    max(0.0, min(1.0, float(batch.progress))),
                    inserted,
                    now,
                    revision_id,
                    attempt_id,
                ),
            )
            return cursor.rowcount == 1

    def finish(
        self,
        revision_id: str,
        attempt_id: str,
        *,
        manifest: StructuralManifest,
    ) -> bool:
        now = utc_now_iso()
        status = IndexStatus.PARTIAL if manifest.unsupported else IndexStatus.COMPLETE
        with self.database.transaction() as connection:
            if not self._attempt_owned(
                connection,
                revision_id,
                attempt_id,
            ):
                return False
            current = connection.execute(
                "SELECT documents.current_revision_id "
                "FROM document_revisions "
                "JOIN documents ON documents.id = "
                "document_revisions.document_id "
                "WHERE document_revisions.id = ?",
                (revision_id,),
            ).fetchone()
            if current is None or current["current_revision_id"] != revision_id:
                self._mark_stale(
                    connection,
                    revision_id,
                    attempt_id,
                    now,
                )
                return False
            cursor = connection.execute(
                "UPDATE index_jobs SET status = ?, progress = 1.0, "
                "updated_at = ?, error_code = NULL "
                "WHERE revision_id = ? AND attempt_id = ?",
                (status.value, now, revision_id, attempt_id),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "UPDATE document_revisions SET index_status = ?, "
                "manifest_json = ?, indexed_at = ?, error_code = NULL "
                "WHERE id = ?",
                (
                    status.value,
                    self._json(manifest.public()),
                    now,
                    revision_id,
                ),
            )
        return True

    def fail(
        self,
        revision_id: str,
        attempt_id: str,
        *,
        error_code: str,
    ) -> bool:
        safe_code = re.sub(r"[^a-z0-9_.-]", "_", error_code.casefold())[:80]
        now = utc_now_iso()
        with self.database.transaction() as connection:
            if not self._attempt_owned(
                connection,
                revision_id,
                attempt_id,
            ):
                return False
            current = connection.execute(
                "SELECT documents.current_revision_id "
                "FROM document_revisions JOIN documents "
                "ON documents.id = document_revisions.document_id "
                "WHERE document_revisions.id = ?",
                (revision_id,),
            ).fetchone()
            status = (
                IndexStatus.FAILED
                if current is not None and current["current_revision_id"] == revision_id
                else IndexStatus.STALE
            )
            connection.execute(
                "UPDATE index_jobs SET status = ?, updated_at = ?, "
                "error_code = ? WHERE revision_id = ? AND attempt_id = ?",
                (
                    status.value,
                    now,
                    safe_code,
                    revision_id,
                    attempt_id,
                ),
            )
            connection.execute(
                "UPDATE document_revisions SET index_status = ?, "
                "error_code = ? WHERE id = ?",
                (status.value, safe_code, revision_id),
            )
        return True

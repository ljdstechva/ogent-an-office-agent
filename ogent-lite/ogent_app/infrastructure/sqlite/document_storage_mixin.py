"""Row, blob, and identity helpers for the document repository."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from ogent_app.domain.document_intelligence import (
    DocumentFormat,
    DocumentRecord,
    DocumentRevision,
    IndexJob,
    IndexStatus,
    IndexedNode,
    LocatorNamespace,
    LocatorStability,
    NodeKind,
    StructuralLocator,
    StoredDocumentNode,
)

from .blob_store import BlobRef
from .document_repository_constants import (
    TEXT_CHUNK_CHARACTERS,
)


class DocumentStorageMixin:
    blobs: Any

    def _insert_node(
        self,
        connection: sqlite3.Connection,
        revision_id: str,
        node: IndexedNode,
        blob: BlobRef | None,
        now: str,
        *,
        origin_id: str | None,
        reused_text_blob_id: str | None,
        reuse_text_index: bool,
    ) -> int:
        node_id = self._node_id(revision_id, node.stable_path)
        if blob is not None:
            self._insert_blob(connection, blob, now)
        text_blob_id = (
            reused_text_blob_id
            if reuse_text_index
            else blob.blob_id
            if blob is not None
            else None
        )
        cursor = connection.execute(
            "INSERT OR IGNORE INTO document_nodes("
            "id, revision_id, stable_path, parent_path, kind, title, "
            "text_blob_id, metadata_json, content_sha256, sheet_name, "
            "slide_number, page_number, ordinal, origin_node_id, "
            "native_key, locator_stability, lineage_key, "
            "source_paths_json, locator_namespace, locator_resolvable"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?)",
            (
                node_id,
                revision_id,
                node.stable_path,
                node.parent_path,
                node.kind.value,
                node.title,
                text_blob_id,
                self._json(node.metadata),
                node.content_sha256,
                node.sheet_name,
                node.slide_number,
                node.page_number,
                int(node.ordinal),
                origin_id,
                node.native_key,
                node.locator_stability.value,
                node.lineage_key,
                self._json(list(node.locator.source_paths)),
                node.locator.namespace.value,
                1 if node.locator.resolvable else 0,
            ),
        )
        if cursor.rowcount == 1 and node.text and not reuse_text_index:
            search_text = node.text.replace("\x00", "")
            for index, chunk in enumerate(self._chunks(search_text)):
                chunk_id = hashlib.sha256(f"{node_id}\0{index}".encode()).hexdigest()
                connection.execute(
                    "INSERT INTO document_chunks("
                    "id, revision_id, node_id, chunk_index, text, "
                    "token_count, content_sha256"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        revision_id,
                        node_id,
                        index,
                        chunk,
                        max(1, (len(chunk) + 3) // 4),
                        hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                    ),
                )
        return int(cursor.rowcount == 1)

    def _node_reuse_plan(
        self,
        connection: sqlite3.Connection,
        revision_id: str,
        node: IndexedNode,
    ) -> tuple[str | None, str | None, bool]:
        """Resolve immutable parent identity and reusable text materialization."""

        origin_id = self._origin_node_id(connection, revision_id, node)
        if origin_id is None or not node.text:
            return origin_id, None, False
        row = connection.execute(
            "SELECT content_sha256, text_blob_id FROM document_nodes WHERE id = ?",
            (origin_id,),
        ).fetchone()
        if (
            row is None
            or str(row["content_sha256"]) != node.content_sha256
            or row["text_blob_id"] is None
        ):
            return origin_id, None, False
        return origin_id, str(row["text_blob_id"]), True

    @staticmethod
    def _chunks(text: str) -> tuple[str, ...]:
        return tuple(
            text[index : index + TEXT_CHUNK_CHARACTERS]
            for index in range(0, len(text), TEXT_CHUNK_CHARACTERS)
        )

    def _origin_node_id(
        self,
        connection: sqlite3.Connection,
        revision_id: str,
        node: IndexedNode,
    ) -> str | None:
        revision = connection.execute(
            "SELECT parent_revision_id FROM document_revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
        parent_id = revision["parent_revision_id"] if revision else None
        if parent_id is None:
            return None
        candidates: list[sqlite3.Row] = []
        if node.lineage_key:
            candidates = connection.execute(
                "SELECT id, content_sha256 FROM document_nodes "
                "WHERE revision_id = ? AND lineage_key = ? AND kind = ? "
                "LIMIT 3",
                (parent_id, node.lineage_key, node.kind.value),
            ).fetchall()
        if len(candidates) == 1:
            return str(candidates[0]["id"])
        row = connection.execute(
            "SELECT id FROM document_nodes WHERE revision_id = ? "
            "AND stable_path = ? AND kind = ?",
            (parent_id, node.stable_path, node.kind.value),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    @staticmethod
    def _attempt_owned(
        connection: sqlite3.Connection,
        revision_id: str,
        attempt_id: str,
    ) -> bool:
        row = connection.execute(
            "SELECT 1 FROM index_jobs "
            "JOIN document_revisions AS revision "
            "ON revision.id = index_jobs.revision_id "
            "JOIN documents ON documents.id = revision.document_id "
            "WHERE index_jobs.revision_id = ? "
            "AND index_jobs.attempt_id = ? "
            "AND documents.current_revision_id = index_jobs.revision_id "
            "AND index_jobs.status IN (?, ?)",
            (
                revision_id,
                attempt_id,
                IndexStatus.QUICK_READY.value,
                IndexStatus.INDEXING.value,
            ),
        ).fetchone()
        return row is not None

    @staticmethod
    def _mark_stale(
        connection: sqlite3.Connection,
        revision_id: str,
        attempt_id: str,
        now: str,
    ) -> None:
        connection.execute(
            "UPDATE index_jobs SET status = ?, updated_at = ? "
            "WHERE revision_id = ? AND attempt_id = ?",
            (
                IndexStatus.STALE.value,
                now,
                revision_id,
                attempt_id,
            ),
        )
        connection.execute(
            "UPDATE document_revisions SET index_status = ? WHERE id = ?",
            (IndexStatus.STALE.value, revision_id),
        )

    @staticmethod
    def _node_id_for_path(
        connection: sqlite3.Connection,
        revision_id: str,
        stable_path: str,
    ) -> str | None:
        row = connection.execute(
            "SELECT id FROM document_nodes WHERE revision_id = ? AND stable_path = ?",
            (revision_id, stable_path),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    def _stored_node(
        self,
        row: sqlite3.Row,
        *,
        include_text: bool,
    ) -> StoredDocumentNode:
        text = ""
        if include_text and row["text_blob_id"]:
            text = self.blobs.read_text(str(row["text_blob_id"]))
        locator = StructuralLocator(
            stable_path=str(row["stable_path"]),
            native_key=row["native_key"],
            stability=LocatorStability(str(row["locator_stability"])),
            lineage_key=row["lineage_key"],
            source_paths=tuple(json.loads(str(row["source_paths_json"]))),
            namespace=LocatorNamespace(str(row["locator_namespace"])),
            resolvable=bool(row["locator_resolvable"]),
        )
        return StoredDocumentNode(
            str(row["id"]),
            str(row["revision_id"]),
            IndexedNode(
                locator,
                NodeKind(str(row["kind"])),
                parent_path=row["parent_path"],
                title=row["title"],
                text=text,
                metadata=json.loads(str(row["metadata_json"])),
                sheet_name=row["sheet_name"],
                slide_number=row["slide_number"],
                page_number=row["page_number"],
                ordinal=int(row["ordinal"]),
                content_sha256=str(row["content_sha256"]),
            ),
        )

    @staticmethod
    def _document_record(row: sqlite3.Row) -> DocumentRecord:
        return DocumentRecord(
            document_id=str(row["id"]),
            source_path=row["source_path"],
            active_path=str(row["active_path"]),
            mode=str(row["mode"]),
            document_format=DocumentFormat(str(row["format"])),
            canonical_path_key=str(row["canonical_path_key"]),
            created_at=str(row["created_at"]),
            backup_id=row["backup_id"],
        )

    @staticmethod
    def _revision_record(row: sqlite3.Row) -> DocumentRevision:
        return DocumentRevision(
            revision_id=str(row["id"]),
            document_id=str(row["document_id"]),
            revision_number=int(row["revision_number"]),
            package_sha256=str(row["package_sha256"]),
            created_at=str(row["created_at"]),
            index_status=IndexStatus(str(row["index_status"])),
            index_version=int(row["index_version"]),
            quick_manifest=json.loads(str(row["quick_manifest_json"])),
            manifest=json.loads(str(row["manifest_json"])),
            indexed_at=row["indexed_at"],
            error_code=row["error_code"],
        )

    @staticmethod
    def _job_record(row: sqlite3.Row) -> IndexJob:
        return IndexJob(
            revision_id=str(row["revision_id"]),
            attempt_id=str(row["attempt_id"]),
            attempt_generation=int(row["attempt_generation"]),
            status=IndexStatus(str(row["status"])),
            progress=float(row["progress"]),
            indexed_nodes=int(row["indexed_nodes"]),
            total_estimate=int(row["total_estimate"]),
            started_at=row["started_at"],
            updated_at=str(row["updated_at"]),
            error_code=row["error_code"],
        )

    @staticmethod
    def _node_id(revision_id: str, path: str) -> str:
        return hashlib.sha256(
            f"node\0{revision_id}\0{path}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _edge_id(
        revision_id: str,
        source_id: str,
        target_id: str,
        edge_type: str,
    ) -> str:
        return hashlib.sha256(
            (f"edge\0{revision_id}\0{source_id}\0{target_id}\0{edge_type}").encode(
                "utf-8"
            )
        ).hexdigest()

    @staticmethod
    def _insert_blob(
        connection: sqlite3.Connection,
        blob: BlobRef,
        now: str,
    ) -> None:
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
                now,
            ),
        )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )

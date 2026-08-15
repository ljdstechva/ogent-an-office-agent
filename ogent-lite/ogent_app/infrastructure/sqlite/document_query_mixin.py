"""Revision queries and structural retrieval for the document repository."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable

from ogent_app.domain.document_intelligence import (
    DocumentRevision,
    IndexJob,
    IndexStatus,
    NodeKind,
    RevisionDelta,
    SearchHit,
    StoredDocumentNode,
)

from .document_repository_constants import (
    COVERAGE_NODE_KINDS,
    MAX_SEARCH_RESULTS,
)
from .connection import SqliteDatabase


class DocumentQueryMixin:
    database: SqliteDatabase

    @staticmethod
    def _revision_record(row: sqlite3.Row) -> DocumentRevision:
        raise NotImplementedError

    @staticmethod
    def _job_record(row: sqlite3.Row) -> IndexJob:
        raise NotImplementedError

    def _stored_node(
        self,
        row: sqlite3.Row,
        *,
        include_text: bool,
    ) -> StoredDocumentNode:
        raise NotImplementedError

    def current_revision(
        self,
        document_id: str,
    ) -> DocumentRevision | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT revision.* FROM documents "
                "JOIN document_revisions AS revision "
                "ON revision.id = documents.current_revision_id "
                "WHERE documents.id = ?",
                (document_id,),
            ).fetchone()
        return self._revision_record(row) if row is not None else None

    def revision(self, revision_id: str) -> DocumentRevision | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM document_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
        return self._revision_record(row) if row is not None else None

    def revision_for_package(
        self,
        document_id: str,
        package_sha256: str,
    ) -> DocumentRevision | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM document_revisions "
                "WHERE document_id = ? AND package_sha256 = ? "
                "ORDER BY revision_number DESC LIMIT 1",
                (str(document_id), str(package_sha256)),
            ).fetchone()
        return self._revision_record(row) if row is not None else None

    def job(self, revision_id: str) -> IndexJob | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
        return self._job_record(row) if row is not None else None

    def current_state_for_workspace(
        self,
        workspace_id: str,
    ) -> tuple[DocumentRevision, IndexJob] | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT revision.*, job.attempt_id AS job_attempt_id, "
                "job.attempt_generation AS job_attempt_generation, "
                "job.status AS job_status, job.progress AS job_progress, "
                "job.indexed_nodes AS job_indexed_nodes, "
                "job.total_estimate AS job_total_estimate, "
                "job.started_at AS job_started_at, "
                "job.updated_at AS job_updated_at, "
                "job.error_code AS job_error_code "
                "FROM workspaces "
                "JOIN documents ON documents.id = workspaces.document_id "
                "JOIN document_revisions AS revision "
                "ON revision.id = documents.current_revision_id "
                "JOIN index_jobs AS job ON job.revision_id = revision.id "
                "WHERE workspaces.id = ?",
                (workspace_id,),
            ).fetchone()
        if row is None:
            return None
        revision = self._revision_record(row)
        job = IndexJob(
            revision.revision_id,
            str(row["job_attempt_id"]),
            int(row["job_attempt_generation"]),
            IndexStatus(str(row["job_status"])),
            float(row["job_progress"]),
            int(row["job_indexed_nodes"]),
            int(row["job_total_estimate"]),
            row["job_started_at"],
            str(row["job_updated_at"]),
            row["job_error_code"],
        )
        return revision, job

    def nodes(
        self,
        revision_id: str,
        *,
        kinds: Iterable[NodeKind] | None = None,
        limit: int = 500,
        offset: int = 0,
        include_text: bool = False,
    ) -> tuple[StoredDocumentNode, ...]:
        parameters: list[Any] = [revision_id]
        where = "WHERE revision_id = ?"
        kind_values = tuple(kind.value for kind in (kinds or ()))
        if kind_values:
            placeholders = ",".join("?" for _ in kind_values)
            where += f" AND kind IN ({placeholders})"
            parameters.extend(kind_values)
        parameters.extend(
            (
                max(1, min(5_000, int(limit))),
                max(0, int(offset)),
            )
        )
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM document_nodes "
                f"{where} ORDER BY ordinal, stable_path LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
        return tuple(self._stored_node(row, include_text=include_text) for row in rows)

    def resolve_node_ids(
        self,
        revision_id: str,
        stable_paths: Iterable[str],
    ) -> tuple[str, ...]:
        paths = tuple(
            dict.fromkeys(
                str(path).strip() for path in stable_paths if str(path).strip()
            )
        )
        if not paths:
            return ()
        if len(paths) > 10_000:
            raise ValueError("Too many structural paths were requested.")
        placeholders = ",".join("?" for _ in paths)
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT id, stable_path FROM document_nodes "
                "WHERE revision_id = ? "
                f"AND stable_path IN ({placeholders})",
                (revision_id, *paths),
            ).fetchall()
        identifiers = {str(row["stable_path"]): str(row["id"]) for row in rows}
        return tuple(identifiers[path] for path in paths if path in identifiers)

    def nodes_for_ids(
        self,
        revision_id: str,
        node_ids: Iterable[str],
        *,
        include_text: bool = True,
        limit: int = 500,
    ) -> tuple[StoredDocumentNode, ...]:
        identifiers = tuple(
            dict.fromkeys(
                str(node_id).strip() for node_id in node_ids if str(node_id).strip()
            )
        )[: max(1, min(5_000, int(limit)))]
        if not identifiers:
            return ()
        placeholders = ",".join("?" for _ in identifiers)
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM document_nodes WHERE revision_id = ? "
                f"AND id IN ({placeholders}) ORDER BY ordinal, stable_path",
                (revision_id, *identifiers),
            ).fetchall()
        by_id = {
            str(row["id"]): self._stored_node(
                row,
                include_text=include_text,
            )
            for row in rows
        }
        return tuple(
            by_id[identifier] for identifier in identifiers if identifier in by_id
        )

    def descendant_nodes(
        self,
        revision_id: str,
        node_ids: Iterable[str],
        *,
        include_text: bool = True,
        limit: int = 500,
    ) -> tuple[StoredDocumentNode, ...]:
        identifiers = tuple(
            dict.fromkeys(
                str(node_id).strip() for node_id in node_ids if str(node_id).strip()
            )
        )
        if not identifiers:
            return ()
        if len(identifiers) > 1_000:
            raise ValueError("Too many structural roots were requested.")
        placeholders = ",".join("?" for _ in identifiers)
        bounded_limit = max(1, min(5_000, int(limit)))
        with self.database.reader() as connection:
            rows = connection.execute(
                "WITH RECURSIVE scoped(id, stable_path) AS ("
                "SELECT id, stable_path FROM document_nodes "
                "WHERE revision_id = ? "
                f"AND id IN ({placeholders}) "
                "UNION "
                "SELECT child.id, child.stable_path "
                "FROM document_nodes AS child "
                "JOIN scoped AS parent "
                "ON child.parent_path = parent.stable_path "
                "WHERE child.revision_id = ?"
                ") SELECT node.* FROM document_nodes AS node "
                "JOIN scoped ON scoped.id = node.id "
                "WHERE node.revision_id = ? "
                "ORDER BY node.ordinal, node.stable_path LIMIT ?",
                (
                    revision_id,
                    *identifiers,
                    revision_id,
                    revision_id,
                    bounded_limit,
                ),
            ).fetchall()
        return tuple(self._stored_node(row, include_text=include_text) for row in rows)

    def nodes_for_paths(
        self,
        revision_id: str,
        stable_paths: Iterable[str],
        *,
        include_text: bool = True,
        limit: int = 500,
    ) -> tuple[StoredDocumentNode, ...]:
        paths = tuple(
            dict.fromkeys(
                str(path).strip() for path in stable_paths if str(path).strip()
            )
        )
        if not paths:
            return ()
        if len(paths) > 500:
            raise ValueError("Too many document paths were requested.")
        placeholders = ",".join("?" for _ in paths)
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM document_nodes WHERE revision_id = ? "
                f"AND stable_path IN ({placeholders}) "
                "ORDER BY ordinal, stable_path LIMIT ?",
                (
                    str(revision_id),
                    *paths,
                    max(1, min(500, int(limit))),
                ),
            ).fetchall()
        return tuple(self._stored_node(row, include_text=include_text) for row in rows)

    def related_nodes(
        self,
        revision_id: str,
        node_ids: Iterable[str],
        *,
        include_text: bool = True,
        limit: int = 200,
    ) -> tuple[StoredDocumentNode, ...]:
        identifiers = tuple(
            dict.fromkeys(
                str(node_id).strip() for node_id in node_ids if str(node_id).strip()
            )
        )
        if not identifiers:
            return ()
        if len(identifiers) > 1_000:
            raise ValueError("Too many graph roots were requested.")
        placeholders = ",".join("?" for _ in identifiers)
        bounded_limit = max(1, min(2_000, int(limit)))
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT DISTINCT node.* FROM document_edges AS edge "
                "JOIN document_nodes AS node ON node.id = "
                "CASE WHEN edge.source_node_id "
                f"IN ({placeholders}) THEN edge.target_node_id "
                "ELSE edge.source_node_id END "
                "WHERE edge.revision_id = ? AND ("
                f"edge.source_node_id IN ({placeholders}) OR "
                f"edge.target_node_id IN ({placeholders})"
                ") ORDER BY node.ordinal, node.stable_path LIMIT ?",
                (
                    *identifiers,
                    revision_id,
                    *identifiers,
                    *identifiers,
                    bounded_limit,
                ),
            ).fetchall()
        return tuple(self._stored_node(row, include_text=include_text) for row in rows)

    def coverage_nodes(
        self,
        revision_id: str,
        *,
        limit: int = 100_000,
    ) -> tuple[StoredDocumentNode, ...]:
        kinds = tuple(kind.value for kind in COVERAGE_NODE_KINDS)
        placeholders = ",".join("?" for _ in kinds)
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM document_nodes WHERE revision_id = ? "
                f"AND kind IN ({placeholders}) "
                "ORDER BY ordinal, stable_path LIMIT ?",
                (
                    revision_id,
                    *kinds,
                    max(1, min(100_000, int(limit))),
                ),
            ).fetchall()
        return tuple(self._stored_node(row, include_text=False) for row in rows)

    def coverage_node_count(self, revision_id: str) -> int:
        kinds = tuple(kind.value for kind in COVERAGE_NODE_KINDS)
        placeholders = ",".join("?" for _ in kinds)
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM document_nodes "
                "WHERE revision_id = ? "
                f"AND kind IN ({placeholders})",
                (revision_id, *kinds),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def search(
        self,
        revision_id: str,
        query: str,
        *,
        limit: int = 20,
        require_complete: bool = True,
    ) -> tuple[SearchHit, ...]:
        terms = re.findall(r"[\w.-]+", query, re.UNICODE)
        if not terms:
            return ()
        expression = " AND ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:20]
        )
        allowed = (
            (IndexStatus.COMPLETE.value,)
            if require_complete
            else (
                IndexStatus.COMPLETE.value,
                IndexStatus.PARTIAL.value,
            )
        )
        placeholders = ",".join("?" for _ in allowed)
        with self.database.reader() as connection:
            rows = connection.execute(
                "WITH RECURSIVE search_lineage("
                "current_node_id, source_node_id, depth"
                ") AS ("
                "SELECT id, id, 0 FROM document_nodes "
                "WHERE revision_id = ? AND text_blob_id IS NOT NULL "
                "UNION ALL "
                "SELECT lineage.current_node_id, source.origin_node_id, "
                "lineage.depth + 1 "
                "FROM search_lineage AS lineage "
                "JOIN document_nodes AS source "
                "ON source.id = lineage.source_node_id "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM document_chunks AS local_chunk "
                "WHERE local_chunk.node_id = lineage.source_node_id"
                ") AND source.origin_node_id IS NOT NULL "
                "AND lineage.depth < 999"
                "), search_sources AS ("
                "SELECT current_node_id, source_node_id "
                "FROM search_lineage AS lineage "
                "WHERE EXISTS ("
                "SELECT 1 FROM document_chunks AS local_chunk "
                "WHERE local_chunk.node_id = lineage.source_node_id"
                ")"
                ") "
                "SELECT node.id AS node_id, node.stable_path, node.kind, "
                "node.title, node.metadata_json, node.sheet_name, "
                "node.slide_number, fts.text, bm25(document_chunks_fts) "
                "AS rank FROM document_chunks_fts AS fts "
                "JOIN search_sources AS source "
                "ON source.source_node_id = fts.node_id "
                "JOIN document_nodes AS node "
                "ON node.id = source.current_node_id "
                "JOIN document_revisions AS revision "
                "ON revision.id = node.revision_id "
                "JOIN documents ON documents.id = revision.document_id "
                "WHERE document_chunks_fts MATCH ? "
                "AND documents.current_revision_id = revision.id "
                f"AND revision.index_status IN ({placeholders}) "
                "ORDER BY rank, node.stable_path LIMIT ?",
                (
                    revision_id,
                    expression,
                    *allowed,
                    max(1, min(MAX_SEARCH_RESULTS, int(limit))),
                ),
            ).fetchall()
        return tuple(
            SearchHit(
                node_id=str(row["node_id"]),
                stable_path=str(row["stable_path"]),
                kind=NodeKind(str(row["kind"])),
                title=row["title"],
                text=str(row["text"]),
                rank=float(row["rank"]),
                sheet_name=row["sheet_name"],
                slide_number=row["slide_number"],
                metadata=json.loads(str(row["metadata_json"])),
            )
            for row in rows
        )

    def delta(self, revision_id: str) -> RevisionDelta:
        with self.database.reader() as connection:
            revision = connection.execute(
                "SELECT parent_revision_id FROM document_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            rows = connection.execute(
                "SELECT current.stable_path, current.content_sha256, "
                "origin.id AS origin_id, "
                "origin.content_sha256 AS origin_sha256 "
                "FROM document_nodes AS current "
                "LEFT JOIN document_nodes AS origin "
                "ON origin.id = current.origin_node_id "
                "WHERE current.revision_id = ?",
                (revision_id,),
            ).fetchall()
            referenced_origins = {
                str(row["origin_id"]) for row in rows if row["origin_id"] is not None
            }
            parent_id = revision["parent_revision_id"]
            removed_rows = (
                connection.execute(
                    "SELECT id, stable_path FROM document_nodes WHERE revision_id = ?",
                    (parent_id,),
                ).fetchall()
                if parent_id is not None
                else ()
            )
        added: list[str] = []
        changed: list[str] = []
        reused: list[str] = []
        for row in rows:
            path = str(row["stable_path"])
            if row["origin_id"] is None:
                added.append(path)
            elif row["content_sha256"] == row["origin_sha256"]:
                reused.append(path)
            else:
                changed.append(path)
        removed = [
            str(row["stable_path"])
            for row in removed_rows
            if str(row["id"]) not in referenced_origins
        ]
        return RevisionDelta(
            revision_id,
            tuple(sorted(added)),
            tuple(sorted(changed)),
            tuple(sorted(reused)),
            tuple(sorted(removed)),
        )

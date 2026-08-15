"""Background revision observation, indexing, and content-safe progress."""

from __future__ import annotations

import concurrent.futures
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ogent_app.domain.document_intelligence import (
    DocumentFormat,
    IndexStatus,
    NodeKind,
    ObservedRevision,
    StructuralManifest,
)
from ogent_app.infrastructure.indexing import DocumentIndexer
from ogent_app.infrastructure.indexing.common import package_sha256
from ogent_app.infrastructure.sqlite.document_repository import (
    DocumentRepository,
)


CORE_MANIFEST_KINDS = {
    NodeKind.SECTION,
    NodeKind.HEADING,
    NodeKind.TABLE,
    NodeKind.FIGURE,
    NodeKind.CHART,
    NodeKind.SHEET,
    NodeKind.SLIDE,
    NodeKind.PROCESS_FLOW,
}


class DocumentIndexNotReady(RuntimeError):
    pass


class DocumentIntelligenceCoordinator:
    """Own a bounded pool of attempt-scoped background index workers."""

    def __init__(
        self,
        repository: DocumentRepository,
        indexer: DocumentIndexer,
        *,
        on_progress: Callable[[str, dict[str, Any]], None] | None = None,
        max_workers: int = 2,
    ) -> None:
        self.repository = repository
        self.indexer = indexer
        self.on_progress = on_progress
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="ogent-index",
        )
        self.lock = threading.RLock()
        self.tasks: dict[str, concurrent.futures.Future[None]] = {}
        self.failure_codes: dict[str, str] = {}
        self.closed = False

    def observe_and_schedule(
        self,
        *,
        workspace_id: str,
        source_path: Path | None,
        active_path: Path,
        mode: str,
        expected_package_sha256: str | None = None,
    ) -> ObservedRevision:
        active = Path(active_path).expanduser().resolve(strict=True)
        digest = expected_package_sha256 or package_sha256(active)
        quick = self.indexer.quick_inventory(
            active,
            expected_package_sha256=digest,
        )
        observed = self.repository.observe(
            workspace_id=workspace_id,
            source_path=source_path,
            active_path=active,
            mode=mode,
            document_format=DocumentFormat.from_path(active),
            package_sha256=digest,
            quick_manifest=quick,
        )
        self._emit(workspace_id, observed.revision.revision_id)
        if observed.revision.index_status in {
            IndexStatus.COMPLETE,
            IndexStatus.PARTIAL,
        }:
            return observed
        with self.lock:
            if self.closed:
                raise RuntimeError("The document intelligence coordinator is closed.")
            existing = self.tasks.get(observed.revision.revision_id)
            if existing is not None and not existing.done():
                return observed
            attempt_id = observed.attempt_id
            if observed.deduplicated:
                attempt_id = self.repository.requeue(observed.revision.revision_id)
            if attempt_id is None:
                attempt_id = self.repository.requeue(observed.revision.revision_id)
            future = self.executor.submit(
                self._index_revision,
                workspace_id,
                active,
                observed.revision.revision_id,
                observed.revision.package_sha256,
                attempt_id,
                quick,
            )
            self.tasks[observed.revision.revision_id] = future
            future.add_done_callback(
                lambda completed, revision_id=observed.revision.revision_id: (
                    self._forget(revision_id, completed)
                )
            )
        return observed

    def _index_revision(
        self,
        workspace_id: str,
        document: Path,
        revision_id: str,
        expected_sha256: str,
        attempt_id: str,
        quick: StructuralManifest,
    ) -> None:
        counts: Counter[str] = Counter()
        paths: list[str] = []
        unsupported: list[str] = []
        try:
            terminal_verified = False
            last_emitted_progress = -1.0
            for batch in self.indexer.iter_batches(
                document,
                expected_package_sha256=expected_sha256,
            ):
                if batch.nodes or batch.edges:
                    if not self.repository.append_batch(
                        revision_id,
                        attempt_id,
                        batch,
                    ):
                        return
                for node in batch.nodes:
                    counts[node.kind.value] += 1
                    if node.kind in CORE_MANIFEST_KINDS:
                        paths.append(node.stable_path)
                unsupported.extend(batch.unsupported)
                if batch.complete:
                    terminal_verified = True
                if (
                    batch.progress >= 1.0
                    or batch.progress - last_emitted_progress >= 0.02
                ):
                    last_emitted_progress = batch.progress
                    self._emit(workspace_id, revision_id)
            if not terminal_verified:
                raise RuntimeError(
                    "The index stream ended without revision verification."
                )
            manifest = StructuralManifest(
                quick.document_format,
                expected_sha256,
                dict(counts),
                tuple(dict.fromkeys(paths)),
                tuple(dict.fromkeys(unsupported)),
                quick=False,
            )
            self.repository.finish(
                revision_id,
                attempt_id,
                manifest=manifest,
            )
            self._emit(workspace_id, revision_id)
        except Exception as exc:
            self.repository.fail(
                revision_id,
                attempt_id,
                error_code=type(exc).__name__,
            )
            self._emit(workspace_id, revision_id)
            raise

    def _emit(self, workspace_id: str, revision_id: str) -> None:
        if self.on_progress is None:
            return
        revision = self.repository.revision(revision_id)
        if revision is None:
            return
        current = self.repository.current_revision(revision.document_id)
        job = self.repository.job(revision_id)
        if current is None or current.revision_id != revision_id or job is None:
            return
        self.on_progress(
            workspace_id,
            self.progress_payload(revision, job),
        )

    @staticmethod
    def progress_payload(
        revision: Any,
        job: Any,
    ) -> dict[str, Any]:
        return {
            "document_id": revision.document_id,
            "revision_id": revision.revision_id,
            "revision_number": revision.revision_number,
            "status": job.status.value,
            "progress": round(job.progress, 4),
            "indexed_nodes": job.indexed_nodes,
            "total_estimate": job.total_estimate,
            "quick_manifest": revision.quick_manifest,
            "error_code": job.error_code,
        }

    def payload_for_revision(
        self,
        revision_id: str,
    ) -> dict[str, Any] | None:
        revision = self.repository.revision(revision_id)
        job = self.repository.job(revision_id)
        if revision is None or job is None:
            return None
        return self.progress_payload(revision, job)

    def _forget(
        self,
        revision_id: str,
        future: concurrent.futures.Future[None],
    ) -> None:
        with self.lock:
            self.tasks.pop(revision_id, None)
            if not future.cancelled():
                error = future.exception()
                if error is not None:
                    self.failure_codes[revision_id] = type(error).__name__

    def stop(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            futures = tuple(self.tasks.values())
            self.tasks.clear()
        for future in futures:
            future.cancel()
        self.executor.shutdown(wait=True, cancel_futures=True)

    @property
    def active_count(self) -> int:
        with self.lock:
            return sum(not future.done() for future in self.tasks.values())

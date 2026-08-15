"""Bounded upload-time reference indexing ownership."""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Callable
from typing import Any


ReferenceIndexJob = Callable[[threading.Event], Any]


class ReferenceIndexCoordinator:
    """Own background extraction tasks independently from provider runs."""

    def __init__(self, *, max_workers: int = 2) -> None:
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="ogent-reference-index",
        )
        self.lock = threading.RLock()
        self.tasks: dict[
            tuple[str, str],
            concurrent.futures.Future[Any],
        ] = {}
        self.cancellations: dict[tuple[str, str], threading.Event] = {}
        self.closed = False

    def schedule(
        self,
        workspace_id: str,
        attachment_id: str,
        job: ReferenceIndexJob,
    ) -> bool:
        key = (str(workspace_id), str(attachment_id))
        with self.lock:
            if self.closed:
                raise RuntimeError("The reference index coordinator is closed.")
            existing = self.tasks.get(key)
            if existing is not None and not existing.done():
                return False
            cancellation = threading.Event()
            future = self.executor.submit(job, cancellation)
            self.tasks[key] = future
            self.cancellations[key] = cancellation
            future.add_done_callback(
                lambda completed, owned_key=key: self._forget(
                    owned_key,
                    completed,
                )
            )
        return True

    def wait(
        self,
        workspace_id: str,
        attachment_id: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        key = (str(workspace_id), str(attachment_id))
        with self.lock:
            future = self.tasks.get(key)
        if future is None:
            return True
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return False
        return True

    def cancel(
        self,
        workspace_id: str,
        attachment_id: str,
    ) -> bool:
        key = (str(workspace_id), str(attachment_id))
        with self.lock:
            cancellation = self.cancellations.get(key)
            future = self.tasks.get(key)
        if cancellation is None and future is None:
            return False
        if cancellation is not None:
            cancellation.set()
        if future is not None:
            future.cancel()
        return True

    def cancel_workspace(self, workspace_id: str) -> int:
        with self.lock:
            keys = [key for key in self.tasks if key[0] == str(workspace_id)]
        for _, attachment_id in keys:
            self.cancel(workspace_id, attachment_id)
        return len(keys)

    def wait_workspace(
        self,
        workspace_id: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Wait until all jobs currently owned by one workspace have exited."""
        with self.lock:
            futures = tuple(
                future
                for (owner, _), future in self.tasks.items()
                if owner == str(workspace_id)
            )
        if not futures:
            return True
        _, pending = concurrent.futures.wait(futures, timeout=timeout)
        return not pending

    def _forget(
        self,
        key: tuple[str, str],
        future: concurrent.futures.Future[Any],
    ) -> None:
        with self.lock:
            if self.tasks.get(key) is not future:
                return
            self.tasks.pop(key, None)
            self.cancellations.pop(key, None)

    @property
    def active_count(self) -> int:
        with self.lock:
            return sum(not future.done() for future in self.tasks.values())

    def stop(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            cancellations = tuple(self.cancellations.values())
            futures = tuple(self.tasks.values())
            self.tasks.clear()
            self.cancellations.clear()
        for cancellation in cancellations:
            cancellation.set()
        for future in futures:
            future.cancel()
        self.executor.shutdown(wait=True, cancel_futures=True)

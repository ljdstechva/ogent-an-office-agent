"""Revision-bound lazy visual rendering through targeted OfficeCLI crops."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from pathlib import Path

from ogent_app.domain.document_intelligence import VisualRegion
from ogent_app.infrastructure.indexing.common import package_sha256
from ogent_app.infrastructure.officecli import OfficeCliExecutor
from ogent_app.infrastructure.sqlite.coverage_repository import (
    VisualRegionRepository,
)


class VisualRegionService:
    def __init__(
        self,
        executor: OfficeCliExecutor,
        repository: VisualRegionRepository,
    ) -> None:
        self.executor = executor
        self.repository = repository
        self._locks_guard = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}

    def get_or_render(
        self,
        *,
        revision_id: str,
        expected_package_sha256: str,
        document: Path,
        stable_path: str,
        render_path: str,
        viewport: str = "default",
    ) -> VisualRegion:
        active = Path(document).expanduser().resolve(strict=True)
        if (
            not render_path.startswith("/")
            or len(render_path) > 2_048
            or any(value in render_path for value in "\x00\r\n")
        ):
            raise ValueError("The visual-region locator is invalid.")
        version = self.executor.version()
        profile = f"officecli:{version}:screenshot-range:v1:{viewport}"
        region_key = hashlib.sha256(
            json.dumps(
                {
                    "render_path": render_path,
                    "viewport": viewport,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cached = self.repository.get(
            revision_id,
            stable_path,
            profile,
            region_key,
        )
        if cached is not None:
            return cached
        lock = self._lock_for(f"{revision_id}\0{stable_path}\0{profile}\0{region_key}")
        with lock:
            cached = self.repository.get(
                revision_id,
                stable_path,
                profile,
                region_key,
            )
            if cached is not None:
                return cached
            if package_sha256(active) != expected_package_sha256:
                raise RuntimeError("The document changed before visual rendering.")
            with tempfile.TemporaryDirectory(prefix="ogent-visual-") as temporary:
                output = Path(temporary) / "region.png"
                result = self.executor.execute(
                    [
                        "view",
                        str(active),
                        "screenshot",
                        "--range",
                        render_path,
                        "--out",
                        str(output),
                    ],
                    cwd=active.parent,
                    timeout_seconds=180,
                )
                if result.exit_code != 0 or not output.is_file():
                    raise RuntimeError(
                        "OfficeCLI could not render the requested visual region."
                    )
                payload = output.read_bytes()
            if package_sha256(active) != expected_package_sha256:
                raise RuntimeError("The document changed during visual rendering.")
            return self.repository.put_png(
                revision_id=revision_id,
                stable_path=stable_path,
                renderer_profile=profile,
                region_key=region_key,
                payload=payload,
            )

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

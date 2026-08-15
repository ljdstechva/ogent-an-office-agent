"""Disk forecasting and bounded cleanup for Ogent-owned local data."""

from __future__ import annotations

import dataclasses
import os
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ogent_app.settings import ResourceQuotas
from ogent_app.infrastructure.fault_injection import FaultInjector, FaultPoint


class StorageQuotaError(OSError):
    """A write would exceed a configured or physical storage boundary."""


@dataclasses.dataclass(frozen=True, slots=True)
class DiskForecast:
    required_bytes: int
    managed_bytes: int
    managed_limit_bytes: int
    free_bytes: int
    reserved_free_bytes: int
    projected_managed_bytes: int
    projected_free_bytes: int
    accepted: bool
    reason: str | None = None

    def public(self) -> dict[str, int | bool | str | None]:
        return dataclasses.asdict(self)


class StorageResourceManager:
    """Forecast writes without following links outside one managed root."""

    def __init__(
        self,
        root: Path,
        quotas: ResourceQuotas,
        *,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        now: Callable[[], float] = time.time,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.quotas = quotas
        self.disk_usage = disk_usage
        self.now = now
        self.fault_injector = fault_injector

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise StorageQuotaError(
                "The Ogent local-data root is not a safe directory."
            )

    def managed_bytes(self) -> int:
        self.initialize()
        total = 0
        pending = [self.root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        # Intentional TOCTOU tolerance: another owner removed it.
                        continue
                    if stat.S_ISLNK(metadata.st_mode):
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(metadata.st_mode):
                        total += int(metadata.st_size)
        return total

    def forecast(self, required_bytes: int) -> DiskForecast:
        required = int(required_bytes)
        if required < 0:
            raise ValueError("required_bytes cannot be negative")
        if self.fault_injector is not None and self.fault_injector.consume(
            FaultPoint.DISK_FULL
        ):
            raise StorageQuotaError("Not enough managed storage for this operation.")
        self.initialize()
        managed = self.managed_bytes()
        free = int(self.disk_usage(self.root).free)
        projected_managed = managed + required
        projected_free = free - required
        reason = None
        if projected_managed > self.quotas.max_local_data_bytes:
            reason = "managed_quota_exceeded"
        elif projected_free < self.quotas.minimum_free_disk_bytes:
            reason = "free_disk_reserve_exceeded"
        return DiskForecast(
            required_bytes=required,
            managed_bytes=managed,
            managed_limit_bytes=self.quotas.max_local_data_bytes,
            free_bytes=free,
            reserved_free_bytes=self.quotas.minimum_free_disk_bytes,
            projected_managed_bytes=projected_managed,
            projected_free_bytes=projected_free,
            accepted=reason is None,
            reason=reason,
        )

    def ensure_capacity(
        self,
        required_bytes: int,
        *,
        purpose: str = "local data",
    ) -> DiskForecast:
        forecast = self.forecast(required_bytes)
        if not forecast.accepted:
            raise StorageQuotaError(
                f"Not enough managed storage for {purpose}. "
                "Free space or clean expired Ogent data, then retry."
            )
        return forecast

    def cleanup_partials(self) -> dict[str, int]:
        """Delete only stale, regular temporary files below the managed root."""
        self.initialize()
        cutoff = self.now() - self.quotas.partial_retention_seconds
        deleted = 0
        reclaimed = 0
        suffixes = (".partial", ".uploading", ".rollback")
        pending = [self.root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        # Intentional TOCTOU tolerance: another owner removed it.
                        continue
                    if stat.S_ISLNK(metadata.st_mode):
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(Path(entry.path))
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    if metadata.st_mtime > cutoff:
                        continue
                    if not entry.name.endswith(suffixes):
                        continue
                    target = Path(entry.path).resolve(strict=True)
                    target.relative_to(self.root)
                    size = int(metadata.st_size)
                    target.unlink()
                    deleted += 1
                    reclaimed += size
        return {"deleted": deleted, "reclaimed_bytes": reclaimed}

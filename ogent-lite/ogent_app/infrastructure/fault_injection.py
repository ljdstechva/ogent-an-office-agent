"""Explicit one-shot fault points for recovery and release testing."""

from __future__ import annotations

import enum
import os
import threading
from collections.abc import Mapping

from ogent_app.settings import FeatureFlags, SettingsError


class FaultPoint(str, enum.Enum):
    PROVIDER_CRASH = "provider_crash"
    OFFICECLI_CRASH = "officecli_crash"
    DATABASE_LOCK = "db_lock"
    DISK_FULL = "disk_full"
    WORD_LOCK = "word_lock"
    PREVIEW_MISMATCH = "preview_mismatch"


class InjectedFault(RuntimeError):
    def __init__(self, point: FaultPoint) -> None:
        self.point = point
        super().__init__(f"Injected Ogent test fault: {point.value}")


class FaultInjector:
    """Thread-safe one-shot faults; empty in all normal production runs."""

    def __init__(self, points: set[FaultPoint] | None = None) -> None:
        self._points = set(points or ())
        self._lock = threading.RLock()

    @classmethod
    def load(
        cls,
        features: FeatureFlags,
        environ: Mapping[str, str] | None = None,
    ) -> "FaultInjector":
        source = os.environ if environ is None else environ
        raw = str(source.get("OGENT_FAULT_POINTS", "")).strip()
        if not raw:
            return cls()
        if not features.fault_injection:
            raise SettingsError(
                "OGENT_FAULT_POINTS requires OGENT_ENABLE_FAULT_INJECTION=true."
            )
        values = {item.strip().casefold() for item in raw.split(",") if item.strip()}
        try:
            points = {FaultPoint(value) for value in values}
        except ValueError as exc:
            known = ", ".join(point.value for point in FaultPoint)
            raise SettingsError(
                f"OGENT_FAULT_POINTS contains an unknown point; use: {known}."
            ) from exc
        return cls(points)

    def consume(self, point: FaultPoint | str) -> bool:
        value = point if isinstance(point, FaultPoint) else FaultPoint(point)
        with self._lock:
            if value not in self._points:
                return False
            self._points.remove(value)
            return True

    def trigger(self, point: FaultPoint | str) -> None:
        value = point if isinstance(point, FaultPoint) else FaultPoint(point)
        if self.consume(value):
            raise InjectedFault(value)

    def pending(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(point.value for point in self._points))

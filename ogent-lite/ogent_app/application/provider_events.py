"""Provider-neutral tool and assistant streaming events."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .provider_event_normalizer import (
    NormalizedProviderEvent as NormalizedProviderEvent,
    ProviderEventNormalizer,
)


class AssistantStreamAccumulator:
    """Keep reconnect-safe provisional text while relaying deltas immediately."""

    def __init__(
        self,
        session: Any,
        *,
        run_id: str,
        provider: str,
        expected_generation: int,
        sanitize: Callable[[str], str] | None = None,
    ) -> None:
        self.session = session
        self.run_id = run_id
        self.provider = provider
        self.expected_generation = expected_generation
        self.sanitize = sanitize or (lambda value: value)
        self.normalizer = ProviderEventNormalizer()
        self.lock = threading.RLock()
        self.text = ""
        self.status = "streaming"
        self.first_delta_at: float | None = None
        self.last_delta_at: float | None = None
        self.delta_count = 0
        self._publish_snapshot()

    def observe(self, provider: str, event: dict[str, Any]) -> None:
        for normalized in self.normalizer.normalize(provider, event):
            if normalized.event_type == "assistant.delta":
                self._append(normalized.text, normalized.metadata)
            else:
                self.session.emit(
                    normalized.event_type,
                    normalized.public(run_id=self.run_id),
                    expected_generation=self.expected_generation,
                )

    def append_segment(
        self,
        text: str,
        *,
        source_event: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Publish a coordinator-produced provisional answer segment."""

        self._append(
            text,
            {
                "source_event": str(source_event)[:120],
                "provisional": True,
                **dict(metadata or {}),
            },
        )

    def finalize(self, final_text: str) -> str:
        sanitized = self.sanitize(str(final_text or ""))
        with self.lock:
            current = self.text
        if sanitized and sanitized != current:
            missing = (
                sanitized[len(current) :]
                if sanitized.startswith(current)
                else sanitized
            )
            if missing:
                self._append(
                    missing,
                    {
                        "source_event": "provider_result",
                        "provisional": True,
                    },
                )
        with self.lock:
            self.status = "completed"
            if sanitized:
                self.text = sanitized
            payload = self._snapshot_unlocked()
        self._publish_snapshot()
        self.session.emit(
            "assistant.completed",
            {
                "run_id": self.run_id,
                "provider": self.provider,
                "text": payload["text"],
                "character_count": payload["character_count"],
                "delta_count": payload["delta_count"],
            },
            expected_generation=self.expected_generation,
        )
        return sanitized

    def fail(self, *, cancelled: bool) -> None:
        with self.lock:
            if self.status != "streaming":
                return
            self.status = "cancelled" if cancelled else "failed"
        self._publish_snapshot()

    def _append(
        self,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        delta = self.sanitize(str(text))
        if not delta:
            return
        now = time.time()
        with self.lock:
            self.text += delta
            self.delta_count += 1
            self.first_delta_at = self.first_delta_at or now
            self.last_delta_at = now
            cumulative = len(self.text)
        self._publish_snapshot()
        self.session.emit(
            "assistant.delta",
            {
                "run_id": self.run_id,
                "provider": self.provider,
                "delta": delta,
                "character_count": cumulative,
                "delta_index": self.delta_count,
                **metadata,
            },
            expected_generation=self.expected_generation,
        )

    def _publish_snapshot(self) -> None:
        with self.lock:
            payload = self._snapshot_unlocked()
        with self.session.lock:
            if (
                self.session.run_id == self.run_id
                and self.session.conversation_generation == self.expected_generation
                and not self.session.closed
            ):
                self.session.assistant_stream = payload

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "status": self.status,
            "text": self.text,
            "character_count": len(self.text),
            "delta_count": self.delta_count,
            "first_delta_at": self.first_delta_at,
            "last_delta_at": self.last_delta_at,
        }

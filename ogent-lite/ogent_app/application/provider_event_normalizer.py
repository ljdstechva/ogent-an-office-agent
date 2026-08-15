"""Normalize provider JSONL into one shared streaming event vocabulary."""

from __future__ import annotations

import dataclasses
import threading
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class NormalizedProviderEvent:
    event_type: str
    provider: str
    text: str = ""
    tool_name: str | None = None
    tool_id: str | None = None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    def public(self, *, run_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": run_id,
            "provider": self.provider,
        }
        if self.text:
            payload["delta"] = self.text
        if self.tool_name:
            payload["tool"] = self.tool_name
        if self.tool_id:
            payload["tool_id"] = self.tool_id
        payload.update(self.metadata)
        return payload


class ProviderEventNormalizer:
    """Translate Codex and Claude JSONL into the shared run event vocabulary."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.assistant_text = ""
        self.claude_text_deltas = False
        self.active_tools: dict[str, str] = {}

    def normalize(
        self,
        provider: str,
        event: dict[str, Any],
    ) -> tuple[NormalizedProviderEvent, ...]:
        provider_id = str(provider).casefold()
        if provider_id == "claude":
            return self._claude(provider_id, event)
        if provider_id == "codex":
            return self._codex(provider_id, event)
        return ()

    def _codex(
        self,
        provider: str,
        event: dict[str, Any],
    ) -> tuple[NormalizedProviderEvent, ...]:
        event_type = str(event.get("type") or "")
        item = event.get("item")
        if not isinstance(item, dict):
            return ()
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or item.get("call_id") or "")
        if item_type == "agent_message" and event_type in {
            "item.updated",
            "item.completed",
        }:
            full_text = str(item.get("text") or "")
            delta = self._delta_from_full_text(full_text)
            return (
                (
                    NormalizedProviderEvent(
                        "assistant.delta",
                        provider,
                        text=delta,
                        metadata={
                            "source_event": event_type,
                            "provisional": True,
                        },
                    ),
                )
                if delta
                else ()
            )
        if item_type in {
            "command_execution",
            "mcp_tool_call",
            "tool_call",
        }:
            tool_name = str(
                item.get("name")
                or item.get("title")
                or item.get("command")
                or item_type
            )
            if event_type == "item.started":
                return (
                    NormalizedProviderEvent(
                        "tool.started",
                        provider,
                        tool_name=tool_name,
                        tool_id=item_id or None,
                    ),
                )
            if event_type in {"item.completed", "item.failed"}:
                return (
                    NormalizedProviderEvent(
                        "tool.completed",
                        provider,
                        tool_name=tool_name,
                        tool_id=item_id or None,
                        metadata={
                            "status": (
                                "failed"
                                if event_type == "item.failed"
                                else str(item.get("status") or "completed")
                            )
                        },
                    ),
                )
        return ()

    def _claude(
        self,
        provider: str,
        event: dict[str, Any],
    ) -> tuple[NormalizedProviderEvent, ...]:
        event_type = str(event.get("type") or "")
        if event_type == "stream_event":
            inner = event.get("event")
            if not isinstance(inner, dict):
                return ()
            inner_type = str(inner.get("type") or "")
            if inner_type == "content_block_delta":
                delta = inner.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = str(delta.get("text") or "")
                    if text:
                        with self.lock:
                            self.claude_text_deltas = True
                            self.assistant_text += text
                        return (
                            NormalizedProviderEvent(
                                "assistant.delta",
                                provider,
                                text=text,
                                metadata={
                                    "source_event": inner_type,
                                    "provisional": True,
                                },
                            ),
                        )
            if inner_type == "content_block_start":
                block = inner.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = str(block.get("id") or "")
                    tool_name = str(block.get("name") or "tool")
                    with self.lock:
                        if tool_id:
                            self.active_tools[tool_id] = tool_name
                    return (
                        NormalizedProviderEvent(
                            "tool.started",
                            provider,
                            tool_name=tool_name,
                            tool_id=tool_id or None,
                        ),
                    )
            return ()
        if event_type == "assistant":
            full_text = self._claude_message_text(event.get("message"))
            with self.lock:
                streamed = self.claude_text_deltas
            delta = (
                self._delta_from_full_text(full_text)
                if full_text and not streamed
                else ""
            )
            return (
                (
                    NormalizedProviderEvent(
                        "assistant.delta",
                        provider,
                        text=delta,
                        metadata={
                            "source_event": event_type,
                            "provisional": True,
                        },
                    ),
                )
                if delta
                else ()
            )
        if event_type == "user":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            completed: list[NormalizedProviderEvent] = []
            if isinstance(content, list):
                for block in content:
                    if (
                        not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                    ):
                        continue
                    tool_id = str(block.get("tool_use_id") or "")
                    with self.lock:
                        tool_name = self.active_tools.pop(
                            tool_id,
                            "tool",
                        )
                    completed.append(
                        NormalizedProviderEvent(
                            "tool.completed",
                            provider,
                            tool_name=tool_name,
                            tool_id=tool_id or None,
                            metadata={
                                "status": (
                                    "failed" if block.get("is_error") else "completed"
                                )
                            },
                        )
                    )
            return tuple(completed)
        return ()

    def _delta_from_full_text(self, full_text: str) -> str:
        text = str(full_text)
        if not text:
            return ""
        with self.lock:
            current = self.assistant_text
            if text == current:
                return ""
            if text.startswith(current):
                delta = text[len(current) :]
            else:
                delta = text
            self.assistant_text = text
            return delta

    @staticmethod
    def _claude_message_text(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        content = value.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        return "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )

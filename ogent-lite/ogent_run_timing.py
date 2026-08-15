#!/usr/bin/env python3
"""Privacy-safe local phase timing and focused OfficeCLI command auditing."""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from typing import Any, Callable

from ogent_app.domain.run import ScopeMode


MAX_TIMING_EVENTS = 256
MAX_SAFE_LABEL = 120
OFFICECLI_PATTERN = re.compile(r"(?i)(?:^|[\\/\"'\s])officecli(?:\.exe)?(?:\s|$)")
QUOTED_OR_BARE_ARGUMENT = r"(?:\"[^\"]+\"|'[^']+'|\S+)"
BROAD_READ_PATTERNS = (
    re.compile(
        rf"(?i)\bofficecli(?:\.exe)?\s+view\s+"
        rf"{QUOTED_OR_BARE_ARGUMENT}\s+text(?:\s|$)"
    ),
    re.compile(
        rf"(?i)\bofficecli(?:\.exe)?\s+get\s+"
        rf"{QUOTED_OR_BARE_ARGUMENT}\s+[\"']?/[\"']?(?:\s|$)"
    ),
    re.compile(
        rf"(?i)\bofficecli(?:\.exe)?\s+query\s+"
        rf"{QUOTED_OR_BARE_ARGUMENT}\s+[\"']?(?:\*|all)\b"
    ),
)


def _safe_label(value: Any) -> str:
    text = re.sub(
        r"[\x00-\x1f]+",
        " ",
        str(value or ""),
    )
    return re.sub(r"\s+", " ", text).strip()[:MAX_SAFE_LABEL]


def _numeric_usage(value: dict[str, Any] | None) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for key, item in (value or {}).items():
        if (
            isinstance(key, str)
            and re.fullmatch(
                r"(?:input|output|cached_input|cache_creation|cache_read)_tokens"
                r"|duration(?:_api)?_ms|num_turns|total_cost_usd",
                key,
            )
            and isinstance(item, (int, float))
            and not isinstance(item, bool)
        ):
            result[key] = item
    return result


@dataclasses.dataclass(frozen=True)
class TimingEvent:
    sequence: int
    phase: str
    elapsed_ms: int
    detail: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class RunTiming:
    """Collect ordered timing metadata without retaining prompts or document text."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        effort: str,
        context_mode: str,
        prompt_bytes: int,
        attachment_count: int,
        materialized_bytes: int,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.provider = _safe_label(provider)
        self.model = _safe_label(model)
        self.effort = _safe_label(effort)
        self.context_mode = (
            context_mode if context_mode in {"fresh", "resumed", "full", "delta"} else "fresh"
        )
        self.prompt_bytes = max(0, int(prompt_bytes))
        self.attachment_count = max(0, int(attachment_count))
        self.materialized_bytes = max(0, int(materialized_bytes))
        self.clock = clock
        self.started = clock()
        self.lock = threading.RLock()
        self.events: list[TimingEvent] = []
        self.tool_call_count = 0
        self.officecli_call_count = 0
        self._officecli_started: list[float] = []
        self._first_provider_event = False
        self._first_tool_request = False
        self._last_tool_result_ms: int | None = None
        self.usage: dict[str, int | float] = {}
        self.focused_scope_violation: str | None = None
        self.mark("request_accepted")

    def _elapsed_ms(self) -> int:
        return max(0, round((self.clock() - self.started) * 1000))

    def mark(self, phase: str, **detail: Any) -> TimingEvent:
        safe_detail: dict[str, Any] = {}
        for key, value in detail.items():
            if isinstance(value, bool):
                safe_detail[_safe_label(key)] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                safe_detail[_safe_label(key)] = value
            elif value is not None:
                safe_detail[_safe_label(key)] = _safe_label(value)
        with self.lock:
            event = TimingEvent(
                sequence=len(self.events) + 1,
                phase=_safe_label(phase),
                elapsed_ms=self._elapsed_ms(),
                detail=safe_detail,
            )
            if len(self.events) < MAX_TIMING_EVENTS:
                self.events.append(event)
            return event

    def observe_provider_event(
        self,
        provider: str,
        event: dict[str, Any],
        *,
        focused: bool = False,
        scope_mode: ScopeMode | str | None = None,
    ) -> None:
        """Observe structure only; never persist event payload or tool arguments."""
        if scope_mode is None:
            resolved_scope = (
                ScopeMode.SELECTED_ONLY
                if focused
                else ScopeMode.WHOLE_DOCUMENT
            )
        else:
            resolved_scope = ScopeMode(scope_mode)
        restricts_broad_reads = resolved_scope in {
            ScopeMode.SELECTED_ONLY,
            ScopeMode.LOCAL_REGION,
            ScopeMode.SPECIFIED_SECTIONS,
            ScopeMode.SPECIFIED_SHEETS,
            ScopeMode.SPECIFIED_SLIDES,
            ScopeMode.ATTACHMENTS_ONLY,
        }
        with self.lock:
            if not self._first_provider_event:
                self._first_provider_event = True
                self.mark("first_provider_event")

        tool_name, command, tool_started, tool_finished = self._tool_signal(
            provider,
            event,
        )
        if tool_started:
            with self.lock:
                self.tool_call_count += 1
                if not self._first_tool_request:
                    self._first_tool_request = True
                    self.mark(
                        "first_tool_request",
                        tool=_safe_label(tool_name or "tool"),
                    )
            if command and OFFICECLI_PATTERN.search(command):
                with self.lock:
                    self.officecli_call_count += 1
                    self._officecli_started.append(self.clock())
                self.mark("officecli_call_start")
                if restricts_broad_reads and any(
                    pattern.search(command) for pattern in BROAD_READ_PATTERNS
                ):
                    with self.lock:
                        self.focused_scope_violation = (
                            "A focused run attempted a broad whole-document semantic read."
                        )
                    self.mark("focused_scope_violation")
            elif tool_name and "officecli" in tool_name.casefold():
                with self.lock:
                    self.officecli_call_count += 1
                    self._officecli_started.append(self.clock())
                self.mark("officecli_call_start")
                normalized_command = (
                    f"officecli {command}" if command else ""
                )
                if restricts_broad_reads and normalized_command and any(
                    pattern.search(normalized_command)
                    for pattern in BROAD_READ_PATTERNS
                ):
                    with self.lock:
                        self.focused_scope_violation = (
                            "A focused run attempted a broad whole-document semantic read."
                        )
                    self.mark("focused_scope_violation")
        if tool_finished:
            duration_ms: int | None = None
            with self.lock:
                if self._officecli_started:
                    duration_ms = max(
                        0,
                        round((self.clock() - self._officecli_started.pop(0)) * 1000),
                    )
                self._last_tool_result_ms = self._elapsed_ms()
            if duration_ms is not None:
                self.mark("officecli_call_end", duration_ms=duration_ms)
            self.mark("last_tool_result")

    @staticmethod
    def _tool_signal(
        provider: str,
        event: dict[str, Any],
    ) -> tuple[str | None, str | None, bool, bool]:
        provider_id = provider.casefold()
        event_type = str(event.get("type") or "")
        if provider_id == "codex":
            item = event.get("item")
            if not isinstance(item, dict):
                return None, None, False, False
            item_type = str(item.get("type") or "")
            if item_type in {"command_execution", "command"}:
                command = str(item.get("command") or "")
                tool_name = item_type
            elif item_type == "mcp_tool_call":
                arguments = item.get("arguments")
                command = (
                    str(arguments.get("command") or "")
                    if isinstance(arguments, dict)
                    else None
                )
                tool_name = "__".join(
                    part
                    for part in (
                        str(item.get("server") or ""),
                        str(item.get("tool") or ""),
                    )
                    if part
                ) or item_type
            else:
                command = None
                tool_name = item_type
            started = event_type in {"item.started", "item_start"} and (
                command is not None or "tool" in item_type
            )
            finished = event_type in {"item.completed", "item.failed", "item_end"} and (
                command is not None or "tool" in item_type
            )
            return tool_name or None, command, started, finished
        if provider_id == "claude":
            if event_type == "stream_event":
                inner = event.get("event")
                if not isinstance(inner, dict):
                    return None, None, False, False
                if inner.get("type") == "content_block_start":
                    block = inner.get("content_block")
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        return str(block.get("name") or "tool"), None, True, False
                if inner.get("type") == "content_block_stop":
                    return None, None, False, True
            if event_type == "assistant":
                message = event.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list):
                    tool = next(
                        (
                            block
                            for block in content
                            if isinstance(block, dict)
                            and block.get("type") == "tool_use"
                        ),
                        None,
                    )
                    if isinstance(tool, dict):
                        return str(tool.get("name") or "tool"), None, True, False
            if event_type == "user":
                message = event.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list) and any(
                    isinstance(block, dict) and block.get("type") == "tool_result"
                    for block in content
                ):
                    return None, None, False, True
        return None, None, False, False

    def finish(
        self,
        *,
        outcome: str,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.usage = _numeric_usage(usage)
        self.mark("final_provider_result", outcome=outcome)
        self.mark("total")
        return self.summary()

    def summary(self) -> dict[str, Any]:
        with self.lock:
            return {
                "provider": self.provider,
                "model": self.model,
                "effort": self.effort,
                "context_mode": self.context_mode,
                "prompt_bytes": self.prompt_bytes,
                "attachment_count": self.attachment_count,
                "materialized_bytes": self.materialized_bytes,
                "tool_call_count": self.tool_call_count,
                "officecli_call_count": self.officecli_call_count,
                "usage": dict(self.usage),
                "focused_scope_violation": self.focused_scope_violation,
                "total_elapsed_ms": self._elapsed_ms(),
                "events": [item.public() for item in self.events],
            }

    def concise_line(self) -> str:
        summary = self.summary()
        return (
            "Timing: "
            f"total={summary['total_elapsed_ms']}ms; "
            f"context={self.context_mode}; prompt={self.prompt_bytes}B; "
            f"attachments={self.attachment_count}/{self.materialized_bytes}B; "
            f"tools={self.tool_call_count}; officecli={self.officecli_call_count}."
        )

    def to_json(self) -> str:
        return json.dumps(self.summary(), ensure_ascii=False, sort_keys=True)

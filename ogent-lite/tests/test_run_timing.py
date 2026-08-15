from __future__ import annotations

import sys
import unittest
from pathlib import Path


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_app.domain.run import ScopeMode  # noqa: E402
from ogent_run_timing import RunTiming  # noqa: E402


class MonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        self.value += 0.125
        return self.value


class RunTimingTests(unittest.TestCase):
    def timing(self) -> RunTiming:
        return RunTiming(
            provider="codex",
            model="gpt-5.6-sol",
            effort="automatic",
            context_mode="fresh",
            prompt_bytes=1234,
            attachment_count=2,
            materialized_bytes=4567,
            clock=MonotonicClock(),
        )

    def test_events_are_ordered_and_content_free(self) -> None:
        timing = self.timing()
        timing.mark("reference_preparation_start")
        timing.observe_provider_event(
            "codex",
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": (
                        'officecli get "C:\\Secret\\Client.docx" "/body/p[1]" --json'
                    ),
                    "output": "TOP SECRET DOCUMENT TEXT",
                },
            },
        )
        timing.observe_provider_event(
            "codex",
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": (
                        'officecli get "C:\\Secret\\Client.docx" "/body/p[1]" --json'
                    ),
                    "output": "TOP SECRET DOCUMENT TEXT",
                },
            },
        )
        result = timing.finish(
            outcome="completed",
            usage={"input_tokens": 25, "output_tokens": 5},
        )
        sequences = [event["sequence"] for event in result["events"]]
        elapsed = [event["elapsed_ms"] for event in result["events"]]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(elapsed, sorted(elapsed))
        serialized = timing.to_json()
        self.assertNotIn("Client.docx", serialized)
        self.assertNotIn("TOP SECRET", serialized)
        self.assertEqual(result["usage"]["input_tokens"], 25)
        self.assertEqual(result["officecli_call_count"], 1)

    def test_focused_broad_read_is_flagged_but_targeted_read_is_not(self) -> None:
        targeted = self.timing()
        targeted.observe_provider_event(
            "codex",
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": "officecli get report.docx /body/p[4] --json",
                },
            },
            focused=True,
        )
        self.assertIsNone(targeted.focused_scope_violation)

        broad = self.timing()
        broad.observe_provider_event(
            "codex",
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": "officecli view report.docx text",
                },
            },
            focused=True,
        )
        self.assertIn("broad", broad.focused_scope_violation or "")

    def test_whole_document_scope_allows_broad_read_even_with_selection(self) -> None:
        timing = self.timing()
        timing.observe_provider_event(
            "codex",
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": "officecli view report.docx text",
                },
            },
            scope_mode=ScopeMode.WHOLE_DOCUMENT,
        )

        self.assertIsNone(timing.focused_scope_violation)

    def test_claude_mcp_calls_are_counted_without_arguments(self) -> None:
        timing = self.timing()
        timing.observe_provider_event(
            "claude",
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "name": "mcp__officecli__officecli",
                        "input": {"command": "secret document content"},
                    },
                },
            },
        )
        timing.observe_provider_event(
            "claude",
            {
                "type": "stream_event",
                "event": {"type": "content_block_stop"},
            },
        )
        self.assertEqual(timing.tool_call_count, 1)
        self.assertEqual(timing.officecli_call_count, 1)
        self.assertNotIn("secret document content", timing.to_json())

    def test_codex_officecli_mcp_call_is_counted_and_scope_checked(self) -> None:
        timing = self.timing()
        timing.observe_provider_event(
            "codex",
            {
                "type": "item.started",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "officecli",
                    "tool": "officecli",
                    "arguments": {
                        "command": "view report.docx text",
                    },
                },
            },
            focused=True,
        )
        timing.observe_provider_event(
            "codex",
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "officecli",
                    "tool": "officecli",
                    "arguments": {
                        "command": "view report.docx text",
                    },
                },
            },
            focused=True,
        )
        self.assertEqual(timing.tool_call_count, 1)
        self.assertEqual(timing.officecli_call_count, 1)
        self.assertIn("broad", timing.focused_scope_violation or "")
        self.assertNotIn("report.docx", timing.to_json())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import threading
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import ogent
from ogent_app.application.provider_events import (
    AssistantStreamAccumulator,
    ProviderEventNormalizer,
)
from ogent_app.application.provider_transport import (
    ProviderTransportPolicy,
)
from ogent_app.ports.agent_provider import ProviderRunResult


class ProviderStreamingTests(unittest.TestCase):
    def test_warm_transport_requires_every_feature_and_isolation_gate(self) -> None:
        disabled = ProviderTransportPolicy(warm_enabled=False).decide(
            "codex",
            "fixture",
            transport_available=True,
            workspace_isolated=True,
            sessions_resumable=True,
        )
        unavailable = ProviderTransportPolicy(warm_enabled=True).decide(
            "codex",
            "fixture",
            transport_available=False,
            workspace_isolated=True,
            sessions_resumable=True,
        )
        enabled = ProviderTransportPolicy(warm_enabled=True).decide(
            "codex",
            "fixture",
            transport_available=True,
            workspace_isolated=True,
            sessions_resumable=True,
        )

        self.assertEqual(disabled.selected, "cold_process")
        self.assertEqual(unavailable.selected, "cold_process")
        self.assertEqual(enabled.selected, "warm_transport")

    def test_normalizer_maps_claude_text_and_tools(self) -> None:
        normalizer = ProviderEventNormalizer()
        started = normalizer.normalize(
            "claude",
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "inspect_document",
                    },
                },
            },
        )
        delta = normalizer.normalize(
            "claude",
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {
                        "type": "text_delta",
                        "text": "First answer text",
                    },
                },
            },
        )
        completed = normalizer.normalize(
            "claude",
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                        }
                    ]
                },
            },
        )

        self.assertEqual(started[0].event_type, "tool.started")
        self.assertEqual(delta[0].event_type, "assistant.delta")
        self.assertEqual(delta[0].text, "First answer text")
        self.assertEqual(completed[0].event_type, "tool.completed")

    def test_codex_delta_reaches_session_before_provider_returns(self) -> None:
        session = ogent.SessionState("streaming-fixture")
        session.run_id = "run-stream"
        session.run_status = "working"
        release_provider = threading.Event()
        provider_emitted = threading.Event()
        adapter_finished = threading.Event()
        provider = ogent.AGENT_PROVIDER_BY_ID["codex"]
        accumulator = AssistantStreamAccumulator(
            session,
            run_id="run-stream",
            provider="codex",
            expected_generation=session.conversation_generation,
        )

        def fake_run(request, **_kwargs):
            assert request.event_observer is not None
            request.event_observer(
                "codex",
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Visible before exit.",
                    },
                },
            )
            provider_emitted.set()
            self.assertTrue(release_provider.wait(timeout=5))
            return ProviderRunResult(
                0,
                None,
                "Visible before exit.",
                (),
                False,
                {},
            )

        def invoke_adapter() -> None:
            try:
                ogent._run_codex_once(
                    session,
                    "Work.",
                    Path.cwd(),
                    None,
                    "fixture",
                    "automatic",
                    "run-stream",
                    event_observer=accumulator.observe,
                )
            finally:
                adapter_finished.set()

        with mock.patch.object(provider, "run_agent", side_effect=fake_run):
            thread = threading.Thread(target=invoke_adapter, daemon=True)
            thread.start()
            self.assertTrue(provider_emitted.wait(timeout=2))
            self.assertFalse(adapter_finished.is_set())
            with session.lock:
                events = list(session.events)
                stream = dict(session.assistant_stream or {})
            self.assertTrue(any(event["type"] == "assistant.delta" for event in events))
            self.assertEqual(stream["text"], "Visible before exit.")
            self.assertTrue(thread.is_alive())
            release_provider.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(adapter_finished.is_set())

    def test_stop_cancels_owned_process_and_event_within_two_seconds(self) -> None:
        session = ogent.SessionState("stop-fixture")
        session.run_id = "run-stop"
        session.run_status = "working"
        session.run_complete.clear()
        cancellation = threading.Event()
        session.owned_cancel_events["run-stop"] = cancellation
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdin.buffer.read()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            ),
        )
        session.run_process = process

        started = time.perf_counter()
        try:
            self.assertTrue(ogent.stop_active_run(session))
            process.wait(timeout=1.8)
            elapsed = time.perf_counter() - started
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)

        self.assertTrue(cancellation.is_set())
        self.assertLess(elapsed, 2.0)
        self.assertIsNotNone(process.returncode)


if __name__ == "__main__":
    unittest.main()

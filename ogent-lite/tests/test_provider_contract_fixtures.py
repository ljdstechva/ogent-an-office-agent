from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from ogent_agent_providers import ClaudeProvider, CodexProvider


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "provider_streams"


class ProviderContractFixtureTests(unittest.TestCase):
    def test_sanitized_recorded_streams_keep_normalized_contract(self) -> None:
        providers = {
            "codex": CodexProvider(),
            "claude": ClaudeProvider(),
        }
        for path in sorted(FIXTURE_ROOT.glob("*.json")):
            with self.subTest(fixture=path.name):
                fixture = json.loads(path.read_text(encoding="utf-8"))
                provider = providers[str(fixture["provider"])]
                state = provider.new_stream_state()
                activities = [
                    activity
                    for event in fixture["events"]
                    if (
                        activity := provider.parse_stream_event(
                            state,
                            event,
                        )
                    )
                ]
                expected: dict[str, Any] = fixture["expected"]
                self.assertEqual(state.session_id, expected["session_id"])
                self.assertEqual(state.final_text, expected["final_text"])
                self.assertEqual(
                    state.error_message,
                    expected["error_message"],
                )
                self.assertEqual(
                    state.usage.get("output_tokens"),
                    expected["output_tokens"],
                )
                self.assertEqual(activities, expected["activities"])


if __name__ == "__main__":
    unittest.main()

"""Manual, secret-free discovery acceptance test for installed agent CLIs.

This file is intentionally excluded from unittest discovery. It invokes the
real authenticated CLIs, but Claude catalog and effort checks are constrained
to the zero-inference ``/model`` protocol enforced by the production adapter.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_agent_providers import (  # noqa: E402
    ClaudeProvider,
    CodexProvider,
    parse_claude_available_models,
    validate_claude_zero_usage,
)


def token_accounting(value: Any) -> dict[str, float]:
    found: dict[str, float] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).casefold()
            if (
                lowered.endswith("_tokens")
                or lowered.endswith("tokencount")
                or lowered in {"inputtokens", "outputtokens"}
            ) and isinstance(child, (int, float)) and not isinstance(child, bool):
                found[str(key)] = float(child)
            found.update(token_accounting(child))
    elif isinstance(value, list):
        for child in value:
            found.update(token_accounting(child))
    return found


def ready_environment(provider: Any) -> Any:
    environment = provider.inspect_environment()
    if environment.status != "ready" or not environment.authenticated:
        raise RuntimeError(
            environment.warning
            or f"{provider.label} is not installed and authenticated."
        )
    return environment


def main() -> int:
    codex = CodexProvider(discovery_timeout=30)
    claude = ClaudeProvider(discovery_timeout=30)
    try:
        codex_environment = ready_environment(codex)
        codex_catalog = codex.discover_catalog(codex_environment)

        claude_environment = ready_environment(claude)
        claude_catalog = claude.discover_catalog(claude_environment)
        raw_result = claude._run_discovery_command(  # noqa: SLF001
            claude._model_discovery_arguments(claude_environment),  # noqa: SLF001
            timeout=30,
        )
        raw_payload = json.loads(raw_result.stdout)
        validate_claude_zero_usage(
            raw_payload,
            exit_code=raw_result.returncode,
        )
        raw_models = parse_claude_available_models(raw_payload["result"])
        if tuple(model.id for model in claude_catalog.models) != raw_models:
            raise RuntimeError("Claude catalog did not match its zero-use /model list.")

        selected_model = claude_catalog.models[0]
        verification = claude.verify_model_efforts(
            claude_environment,
            selected_model.id,
        )
        tokens = {
            **token_accounting(raw_payload.get("usage")),
            **token_accounting(raw_payload.get("modelUsage")),
        }
        if not tokens or any(value != 0 for value in tokens.values()):
            raise RuntimeError("Claude token accounting was missing or nonzero.")

        print(
            json.dumps(
                {
                    "codex": {
                        "installed": codex_environment.installed,
                        "authenticated": codex_environment.authenticated,
                        "cliVersion": codex_environment.cli_version,
                        "capabilitySource": "Codex App Server model/list",
                        "visibleModels": len(codex_catalog.models),
                        "allEffortsFromPerModelRecords": all(
                            model.capability_source == "cli"
                            and model.efforts_verified
                            for model in codex_catalog.models
                        ),
                    },
                    "claude": {
                        "installed": claude_environment.installed,
                        "authenticated": claude_environment.authenticated,
                        "cliVersion": claude_environment.cli_version,
                        "capabilitySource": "local /model and --help",
                        "availableModels": len(claude_catalog.models),
                        "durationApiMs": raw_payload["duration_api_ms"],
                        "totalCostUsd": raw_payload["total_cost_usd"],
                        "tokenFields": tokens,
                        "selectedModelEffortsVerified": len(
                            verification.efforts
                        ),
                        "effortWarning": verification.warning,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        codex.cancel_discovery()
        claude.cancel_discovery()


if __name__ == "__main__":
    raise SystemExit(main())

"""Fast mode: documented low-latency provider mapping and smaller context."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_agent_catalog import (  # noqa: E402
    AUTOMATIC_EFFORT,
    CapabilityCache,
    CapabilityManager,
    ModelCapability,
    ProviderCatalog,
    SelectionValidationError,
)
from ogent_app.application.context_budget import ProviderContextBudget  # noqa: E402


def model(
    model_id: str,
    efforts: tuple[str, ...],
    *,
    default_effort: str | None = None,
    is_default: bool = False,
) -> ModelCapability:
    return ModelCapability(
        id=model_id,
        display_name=model_id,
        efforts=efforts,
        default_effort=default_effort,
        input_modalities=("textual",),
        is_default=is_default,
        capability_source="cli",
        efforts_verified=True,
    )


def catalog(
    provider_id: str,
    models: tuple[ModelCapability, ...],
) -> ProviderCatalog:
    return ProviderCatalog(
        provider_id=provider_id,
        label=provider_id.title(),
        installed=True,
        authenticated=True,
        cli_path=r"C:\fixture\cli.exe",
        cli_version="fixture 1",
        status="ready",
        models=models,
        refreshed_at="2026-08-15T00:00:00+00:00",
        stale=False,
        warning=None,
    )


class _Provider:
    supports_model_effort_verification = False

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.label = provider_id.title()

    def cancel_discovery(self) -> None:  # pragma: no cover - interface stub
        pass


class FastSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = CapabilityManager(
            (_Provider("codex"), _Provider("claude")),
            CapabilityCache(Path(self._tmp.name) / "cache.json"),
        )

    def test_codex_fast_prefers_mini_model_and_minimal_effort(self) -> None:
        self.manager.set_catalog_for_testing(
            catalog(
                "codex",
                (
                    model("gpt-6-sol", ("low", "medium", "high"), is_default=True),
                    model("gpt-6-sol-mini", ("minimal", "low", "medium")),
                ),
            )
        )
        selection = self.manager.resolve_fast_selection("codex", "gpt-6-sol", "high")
        self.assertEqual(selection.model, "gpt-6-sol-mini")
        self.assertEqual(selection.effort, "minimal")

    def test_claude_fast_prefers_haiku_low(self) -> None:
        self.manager.set_catalog_for_testing(
            catalog(
                "claude",
                (
                    model("claude-opus-5", ("low", "medium", "high"), is_default=True),
                    model("claude-haiku-4-5", ("low", "medium")),
                ),
            )
        )
        selection = self.manager.resolve_fast_selection("claude", "claude-opus-5", "high")
        self.assertEqual(selection.model, "claude-haiku-4-5")
        self.assertEqual(selection.effort, "low")

    def test_fast_without_low_latency_model_keeps_selection_lowers_effort(self) -> None:
        self.manager.set_catalog_for_testing(
            catalog(
                "codex",
                (model("gpt-6-sol", ("low", "medium", "high"), is_default=True),),
            )
        )
        selection = self.manager.resolve_fast_selection("codex", "gpt-6-sol", "high")
        self.assertEqual(selection.model, "gpt-6-sol")
        self.assertEqual(selection.effort, "low")

    def test_fast_without_documented_efforts_uses_model_default(self) -> None:
        self.manager.set_catalog_for_testing(
            catalog(
                "claude",
                (
                    model(
                        "claude-opus-5",
                        ("medium", "high"),
                        default_effort="medium",
                        is_default=True,
                    ),
                ),
            )
        )
        selection = self.manager.resolve_fast_selection("claude", "", "")
        self.assertEqual(selection.model, "claude-opus-5")
        self.assertEqual(selection.effort, "medium")

    def test_fast_effort_defaults_to_automatic_when_nothing_reported(self) -> None:
        self.manager.set_catalog_for_testing(
            catalog("codex", (model("gpt-6-sol", ()),))
        )
        selection = self.manager.resolve_fast_selection("codex", None, None)
        self.assertEqual(selection.effort, AUTOMATIC_EFFORT)

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(SelectionValidationError):
            self.manager.resolve_fast_selection("mystery", None, None)

    def test_empty_catalog_is_rejected(self) -> None:
        self.manager.set_catalog_for_testing(catalog("codex", ()))
        with self.assertRaises(SelectionValidationError):
            self.manager.resolve_fast_selection("codex", None, None)


class FastBudgetTests(unittest.TestCase):
    def _budget(self, input_tokens: int) -> ProviderContextBudget:
        return ProviderContextBudget(
            "codex",
            "gpt-6-sol",
            input_tokens,
            4_096,
            4_096,
            ("text",),
            False,
            True,
            "cli",
            True,
        )

    def test_fast_variant_halves_large_budgets(self) -> None:
        fast = self._budget(200_000).fast_variant()
        self.assertEqual(fast.input_context_tokens, 100_000)
        self.assertTrue(fast.source.endswith("+fast"))

    def test_fast_variant_never_goes_below_floor(self) -> None:
        fast = self._budget(40_000).fast_variant()
        self.assertEqual(fast.input_context_tokens, 32_768)

    def test_fast_variant_keeps_already_small_budgets(self) -> None:
        original = self._budget(32_768)
        self.assertIs(original.fast_variant(), original)


if __name__ == "__main__":
    unittest.main()

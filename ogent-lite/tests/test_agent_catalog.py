from __future__ import annotations

import dataclasses
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_agent_catalog import (  # noqa: E402
    AUTOMATIC_EFFORT,
    CapabilityCache,
    CapabilityManager,
    EffortVerificationResult,
    ModelCapability,
    ProviderCatalog,
    ProviderEnvironment,
    SelectionValidationError,
)


def fixture_model(
    model_id: str = "fixture-model/a",
    *,
    efforts: tuple[str, ...] = ("brief", "deep"),
    default_effort: str | None = "brief",
    is_default: bool = True,
    verified: bool = True,
) -> ModelCapability:
    return ModelCapability(
        id=model_id,
        display_name=f"Display {model_id}",
        efforts=efforts,
        default_effort=default_effort,
        input_modalities=("textual",),
        is_default=is_default,
        capability_source="cli",
        efforts_verified=verified,
    )


def fixture_catalog(
    executable: Path,
    *,
    version: str = "fixture-cli 7",
    stale: bool = False,
    models: tuple[ModelCapability, ...] | None = None,
) -> ProviderCatalog:
    return ProviderCatalog(
        provider_id="fixture",
        label="Fixture Agent",
        installed=True,
        authenticated=True,
        cli_path=str(executable),
        cli_version=version,
        status="ready",
        models=models or (fixture_model(),),
        refreshed_at="2026-07-27T00:00:00+00:00",
        stale=stale,
        warning=None,
    )


class StaticProvider:
    provider_id = "fixture"
    label = "Fixture Agent"
    supports_model_effort_verification = False

    def __init__(
        self,
        executable: Path,
        *,
        version: str = "fixture-cli 7",
    ) -> None:
        self.executable = executable
        self.version = version
        self.discovery_calls = 0

    def inspect_environment(self) -> ProviderEnvironment:
        return ProviderEnvironment(
            provider_id=self.provider_id,
            label=self.label,
            installed=True,
            authenticated=True,
            cli_path=str(self.executable),
            cli_version=self.version,
            command=(str(self.executable),),
            status="ready",
        )

    def discover_catalog(
        self,
        environment: ProviderEnvironment,
    ) -> ProviderCatalog:
        self.discovery_calls += 1
        return fixture_catalog(
            self.executable,
            version=self.version,
        )

    def verify_model_efforts(
        self,
        environment: ProviderEnvironment,
        model_id: str,
    ) -> EffortVerificationResult:
        raise AssertionError("This provider does not use lazy effort probes.")

    def cancel_discovery(self) -> None:
        return


class BlockingProvider(StaticProvider):
    def __init__(self, executable: Path) -> None:
        super().__init__(executable)
        self.entered = threading.Event()
        self.release = threading.Event()

    def inspect_environment(self) -> ProviderEnvironment:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test did not release provider inspection")
        return super().inspect_environment()


class LazyEffortProvider(StaticProvider):
    supports_model_effort_verification = True

    def __init__(self, executable: Path) -> None:
        super().__init__(executable)
        self.probe_calls = 0
        self.probe_finished = threading.Event()

    def discover_catalog(
        self,
        environment: ProviderEnvironment,
    ) -> ProviderCatalog:
        self.discovery_calls += 1
        return fixture_catalog(
            self.executable,
            models=(
                fixture_model(
                    efforts=(),
                    default_effort=None,
                    verified=False,
                ),
            ),
        )

    def verify_model_efforts(
        self,
        environment: ProviderEnvironment,
        model_id: str,
    ) -> EffortVerificationResult:
        self.probe_calls += 1
        self.probe_finished.set()
        return EffortVerificationResult(efforts=())


class CapabilityCacheTests(unittest.TestCase):
    def test_cache_round_trip_is_stale_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "agent-capabilities.json"
            executable = root / "fixture.exe"
            cache = CapabilityCache(path)
            catalog = fixture_catalog(executable)

            cache.store(catalog)
            loaded = cache.load(
                catalog.provider_id,
                str(executable),
                str(catalog.cli_version),
            )

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertTrue(loaded.stale)
            self.assertEqual(loaded.status, "cached")
            self.assertEqual(loaded.models, catalog.models)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("email", raw.casefold())
            self.assertNotIn("organization", raw.casefold())
            self.assertNotIn("authentication", raw.casefold())
            self.assertNotIn("secret-marker", raw)

    def test_cache_write_uses_atomic_sibling_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "agent-capabilities.json"
            cache = CapabilityCache(path)
            catalog = fixture_catalog(root / "fixture.exe")

            with mock.patch(
                "ogent_agent_catalog.os.replace",
                wraps=os.replace,
            ) as replacement:
                cache.store(catalog)

            replacement.assert_called_once()
            source, target = replacement.call_args.args
            self.assertEqual(Path(target), path)
            self.assertEqual(Path(source).parent, path.parent)
            self.assertNotEqual(Path(source), path)
            self.assertTrue(path.is_file())

    def test_cache_invalidates_changed_version_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = CapabilityCache(root / "agent-capabilities.json")
            first_path = root / "first" / "fixture.exe"
            catalog = fixture_catalog(first_path)
            cache.store(catalog)

            self.assertIsNone(cache.load("fixture", str(first_path), "fixture-cli 8"))
            self.assertIsNone(
                cache.load(
                    "fixture",
                    str(root / "second" / "fixture.exe"),
                    "fixture-cli 7",
                )
            )

    def test_cache_expires_after_six_hour_freshness_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            now = [1000.0]
            cache = CapabilityCache(
                root / "agent-capabilities.json",
                freshness_seconds=60,
                clock=lambda: now[0],
            )
            catalog = fixture_catalog(root / "fixture.exe")
            cache.store(catalog)
            now[0] += 61

            self.assertIsNone(
                cache.load(
                    "fixture",
                    str(catalog.cli_path),
                    str(catalog.cli_version),
                )
            )


class CapabilityManagerTests(unittest.TestCase):
    def test_duplicate_background_refresh_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = BlockingProvider(root / "fixture.exe")
            finished = threading.Event()
            manager_box: list[CapabilityManager] = []

            def on_change() -> None:
                if manager_box and not manager_box[0].snapshot()["refreshing"]:
                    finished.set()

            manager = CapabilityManager(
                (provider,),
                CapabilityCache(root / "cache.json"),
                on_change=on_change,
            )
            manager_box.append(manager)

            self.assertTrue(manager.refresh_async("fixture"))
            self.assertTrue(provider.entered.wait(timeout=2))
            self.assertFalse(manager.refresh_async("fixture"))
            provider.release.set()
            self.assertTrue(finished.wait(timeout=5))
            self.assertEqual(provider.discovery_calls, 1)
            self.assertEqual(manager.get_catalog("fixture").status, "ready")
            manager.shutdown()

    def test_validation_rejects_unknown_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = CapabilityManager(
                (StaticProvider(Path(temp_dir) / "fixture.exe"),),
                CapabilityCache(Path(temp_dir) / "cache.json"),
            )

            with self.assertRaisesRegex(
                SelectionValidationError,
                "Unknown AI provider",
            ):
                manager.validate_selection(
                    "missing",
                    "fixture-model/a",
                    AUTOMATIC_EFFORT,
                )

    def test_validation_rejects_stale_and_removed_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = CapabilityManager(
                (StaticProvider(root / "fixture.exe"),),
                CapabilityCache(root / "cache.json"),
            )
            manager.set_catalog_for_testing(
                fixture_catalog(root / "fixture.exe", stale=True)
            )
            with self.assertRaisesRegex(
                SelectionValidationError,
                "usable live model catalog",
            ):
                manager.validate_selection(
                    "fixture",
                    "fixture-model/a",
                    AUTOMATIC_EFFORT,
                )

            manager.set_catalog_for_testing(fixture_catalog(root / "fixture.exe"))
            with self.assertRaisesRegex(
                SelectionValidationError,
                "no longer reported",
            ):
                manager.validate_selection(
                    "fixture",
                    "removed-model",
                    AUTOMATIC_EFFORT,
                )

    def test_validation_rejects_invalid_effort_and_accepts_automatic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = CapabilityManager(
                (StaticProvider(root / "fixture.exe"),),
                CapabilityCache(root / "cache.json"),
            )
            manager.set_catalog_for_testing(fixture_catalog(root / "fixture.exe"))

            with self.assertRaisesRegex(
                SelectionValidationError,
                "effort is not available",
            ):
                manager.validate_selection(
                    "fixture",
                    "fixture-model/a",
                    "invented-effort",
                )
            selection = manager.validate_selection(
                "fixture",
                "fixture-model/a",
                AUTOMATIC_EFFORT,
            )
            self.assertEqual(selection.effort, AUTOMATIC_EFFORT)

    def test_snapshot_marks_cached_catalog_non_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = CapabilityManager(
                (StaticProvider(root / "fixture.exe"),),
                CapabilityCache(root / "cache.json"),
            )
            manager.set_catalog_for_testing(
                dataclasses.replace(
                    fixture_catalog(root / "fixture.exe"),
                    stale=True,
                    status="cached",
                )
            )

            provider = manager.snapshot()["providers"][0]

            self.assertTrue(provider["stale"])
            self.assertFalse(provider["live"])

    def test_live_refresh_does_not_promote_cached_effort_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = LazyEffortProvider(root / "fixture.exe")
            cache = CapabilityCache(root / "cache.json")
            cache.store(
                fixture_catalog(
                    root / "fixture.exe",
                    models=(fixture_model(verified=True),),
                )
            )
            manager = CapabilityManager((provider,), cache)

            catalog = manager.refresh_now("fixture")

            self.assertEqual(catalog.status, "ready")
            self.assertFalse(catalog.stale)
            self.assertEqual(catalog.models[0].efforts, ())
            self.assertFalse(catalog.models[0].efforts_verified)
            manager.shutdown()

    def test_completed_empty_effort_probe_is_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = LazyEffortProvider(root / "fixture.exe")
            completed = threading.Event()
            manager_box: list[CapabilityManager] = []

            def on_change() -> None:
                if (
                    manager_box
                    and ("fixture", "fixture-model/a")
                    in manager_box[0].completed_model_probes
                ):
                    completed.set()

            manager = CapabilityManager(
                (provider,),
                CapabilityCache(root / "cache.json"),
                on_change=on_change,
            )
            manager_box.append(manager)
            manager.refresh_now("fixture")

            self.assertTrue(
                manager.ensure_model_efforts_async(
                    "fixture",
                    "fixture-model/a",
                )
            )
            self.assertTrue(completed.wait(timeout=2))
            self.assertFalse(
                manager.ensure_model_efforts_async(
                    "fixture",
                    "fixture-model/a",
                )
            )
            self.assertEqual(provider.probe_calls, 1)
            manager.shutdown()

    def test_global_effort_fallback_remains_explicitly_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider = LazyEffortProvider(root / "fixture.exe")
            manager = CapabilityManager(
                (provider,),
                CapabilityCache(root / "cache.json"),
            )
            manager.refresh_now("fixture")
            current_environment = manager.environments["fixture"]
            verification = EffortVerificationResult(
                efforts=("cli-mode-a", "cli-mode-b"),
                warning="CLI-valid; model-specific support unverified.",
                use_global_unverified=True,
            )

            with mock.patch.object(
                provider,
                "verify_model_efforts",
                return_value=verification,
            ):
                manager._probe_worker(
                    provider,
                    current_environment,
                    "fixture-model/a",
                )

            public_provider = manager.snapshot()["providers"][0]
            public_model = public_provider["models"][0]
            self.assertEqual(
                public_model["efforts"],
                ["cli-mode-a", "cli-mode-b"],
            )
            self.assertFalse(public_model["effortsVerified"])
            self.assertIn("unverified", public_provider["warning"])
            self.assertIn(
                ("fixture", "fixture-model/a"),
                manager.completed_model_probes,
            )
            manager.shutdown()


class StaticCatalogGuardTests(unittest.TestCase):
    @staticmethod
    def _production_frontend() -> str:
        source_root = OGENT_DIR / "web" / "src"
        sources = [
            OGENT_DIR / "web" / "index.html",
            OGENT_DIR / "web" / "shell.html",
            *sorted(
                path
                for path in source_root.rglob("*")
                if path.suffix in {".ts", ".tsx", ".css"} and ".test." not in path.name
            ),
        ]
        return "\n".join(path.read_text(encoding="utf-8") for path in sources)

    def test_production_has_no_static_model_or_effort_catalog(self) -> None:
        production = "\n".join(
            (OGENT_DIR / name).read_text(encoding="utf-8")
            for name in (
                "ogent.py",
                "ogent_agent_catalog.py",
                "ogent_agent_providers.py",
            )
        )
        self.assertIsNone(
            re.search(
                r"\b(?:ALLOWED_MODELS|ALLOWED_REASONING|"
                r"DEFAULT_MODEL|DEFAULT_REASONING)\b",
                production,
            )
        )
        agent_settings = (
            OGENT_DIR / "web" / "src" / "components" / "chat" / "AgentSettings.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "(selection.provider?.models ?? []).map",
            agent_settings,
        )
        self.assertIn(
            "capabilities.providers.map",
            agent_settings,
        )
        self.assertNotRegex(
            agent_settings,
            re.compile(r'<option\s+value="(?:gpt|claude|o[0-9])', re.I),
        )

    def test_mobile_layout_stacks_without_horizontal_clipping(self) -> None:
        production = self._production_frontend()

        self.assertIn("@media (max-width: 760px) {", production)
        self.assertIn("flex-direction: column;", production)
        self.assertIn(".splitter {", production)
        self.assertIn("display: none;", production)
        self.assertIn("overflow-x: hidden;", production)
        self.assertIn("overflow-y: auto;", production)

    def test_saved_ready_provider_wins_after_refresh_fallback(self) -> None:
        selection = (
            OGENT_DIR / "web" / "src" / "hooks" / "useAgentSelection.ts"
        ).read_text(encoding="utf-8")
        self.assertLess(
            selection.index("stored.provider &&"),
            selection.index("providers.find("),
        )
        self.assertIn(
            'provider.live && provider.status === "ready"',
            selection,
        )


if __name__ == "__main__":
    unittest.main()

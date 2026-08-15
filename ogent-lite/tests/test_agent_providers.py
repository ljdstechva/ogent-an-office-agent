from __future__ import annotations

import importlib.util
import json
import queue
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_app.domain.run import ScopeMode  # noqa: E402
from ogent_agent_catalog import (  # noqa: E402
    AUTOMATIC_EFFORT,
    CatalogDiscoveryError,
    ModelCapability,
    ProviderEnvironment,
)
from ogent_agent_providers import (  # noqa: E402
    CLIResolution,
    ClaudeProvider,
    ClaudeStreamState,
    CodexProvider,
    CompatibilityError,
    InferenceDetectedError,
    ProviderRunRequest,
    ProviderRunResult,
    build_claude_command,
    build_codex_command,
    parse_claude_available_models,
    parse_claude_current_effort,
    parse_claude_effort_choices,
    parse_codex_app_models,
    parse_codex_debug_models,
    validate_claude_zero_usage,
)


def zero_usage_payload(result: str) -> dict[str, Any]:
    return {
        "is_error": False,
        "duration_api_ms": 0,
        "total_cost_usd": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "result": result,
    }


def environment(
    provider_id: str,
    *,
    version: str = "fixture-version",
) -> ProviderEnvironment:
    return ProviderEnvironment(
        provider_id=provider_id,
        label="Fixture",
        installed=True,
        authenticated=True,
        cli_path=str(Path.cwd() / f"{provider_id}-fixture.exe"),
        cli_version=version,
        command=(f"{provider_id}-fixture",),
        status="ready",
    )


class QueueLineStream:
    def __init__(self) -> None:
        self.items: queue.Queue[str | None] = queue.Queue()

    def feed(self, line: str) -> None:
        self.items.put(line)

    def close(self) -> None:
        self.items.put(None)

    def __iter__(self) -> "QueueLineStream":
        return self

    def __next__(self) -> str:
        value = self.items.get()
        if value is None:
            raise StopIteration
        return value


class BlockingRunStream:
    def __init__(self) -> None:
        self.items: queue.Queue[str | None] = queue.Queue()

    def readline(self) -> str:
        value = self.items.get()
        return "" if value is None else value

    def close(self) -> None:
        self.items.put(None)


class CapturingRunStdin:
    def __init__(self) -> None:
        self.value = ""
        self.closed = False

    def write(self, value: str) -> int:
        self.value += value
        return len(value)

    def flush(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


class SilentRunProcess:
    def __init__(self) -> None:
        self.stdin = CapturingRunStdin()
        self.stdout = BlockingRunStream()
        self.stderr = BlockingRunStream()
        self.returncode: int | None = None
        self.pid = 45000

    def stop(self) -> None:
        if self.returncode is None:
            self.returncode = 124
            self.stdout.close()
            self.stderr.close()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("silent-run", timeout)
        return self.returncode


class FakeAppServerStdin:
    def __init__(self, process: "FakeAppServerProcess") -> None:
        self.process = process
        self.buffer = ""

    def write(self, value: str) -> int:
        self.buffer += value
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line:
                self.process.receive(json.loads(line))
        return len(value)

    def flush(self) -> None:
        return

    def close(self) -> None:
        self.process.finish()


class FakeAppServerProcess:
    next_pid = 41000

    def __init__(
        self,
        pages: list[list[dict[str, Any]]],
        *,
        malformed: bool = False,
        silent: bool = False,
    ) -> None:
        self.pages = pages
        self.malformed = malformed
        self.silent = silent
        self.stdout = QueueLineStream()
        self.stderr = QueueLineStream()
        self.stderr.close()
        self.stdin = FakeAppServerStdin(self)
        self.returncode: int | None = None
        self.pid = self.next_pid
        FakeAppServerProcess.next_pid += 1
        self.model_requests = 0

    def receive(self, request: dict[str, Any]) -> None:
        if self.silent:
            return
        if request.get("method") == "initialize":
            if self.malformed:
                self.stdout.feed("{malformed\n")
            else:
                self.stdout.feed(json.dumps({"id": request["id"], "result": {}}) + "\n")
            return
        if request.get("method") != "model/list":
            return
        index = self.model_requests
        self.model_requests += 1
        next_cursor = f"cursor-{index + 1}" if index + 1 < len(self.pages) else None
        self.stdout.feed(
            json.dumps(
                {
                    "id": request["id"],
                    "result": {
                        "data": self.pages[index],
                        "nextCursor": next_cursor,
                    },
                }
            )
            + "\n"
        )

    def finish(self) -> None:
        if self.returncode is None:
            self.returncode = 0
            self.stdout.close()

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fixture-app-server", timeout)
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.close()


class CodexCatalogTests(unittest.TestCase):
    def test_multiple_visible_models_and_per_model_capabilities(self) -> None:
        models = parse_codex_app_models(
            [
                {
                    "id": "model-one",
                    "displayName": "Model One",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "quick"},
                        {"reasoningEffort": "thorough"},
                    ],
                    "defaultReasoningEffort": "thorough",
                    "inputModalities": ["text", "image"],
                    "isDefault": True,
                    "hidden": False,
                },
                {
                    "model": "model-two",
                    "displayName": "Model Two",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "balanced"},
                    ],
                    "inputModalities": ["text"],
                    "isDefault": False,
                },
                {
                    "id": "hidden-entry",
                    "displayName": "Hidden",
                    "hidden": True,
                },
            ]
        )

        self.assertEqual([item.id for item in models], ["model-one", "model-two"])
        self.assertEqual(models[0].efforts, ("quick", "thorough"))
        self.assertEqual(models[1].efforts, ("balanced",))
        self.assertEqual(models[0].default_effort, "thorough")
        self.assertTrue(models[0].is_default)
        self.assertEqual(models[0].input_modalities, ("text", "image"))
        self.assertTrue(models[0].efforts_verified)

    def test_app_server_pagination_preserves_cli_order(self) -> None:
        pages = [
            [
                {
                    "id": "page-one",
                    "displayName": "Page One",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "alpha"},
                    ],
                }
            ],
            [
                {
                    "id": "page-two",
                    "displayName": "Page Two",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "beta"},
                    ],
                }
            ],
        ]
        process = FakeAppServerProcess(pages)
        provider = CodexProvider(
            discovery_timeout=1,
            popen_factory=lambda *_args, **_kwargs: process,
        )

        models = provider._app_server_catalog(environment("codex"))

        self.assertEqual([model.id for model in models], ["page-one", "page-two"])
        self.assertEqual(process.model_requests, 2)
        self.assertEqual(process.returncode, 0)

    def test_app_server_timeout_is_bounded_and_cleaned(self) -> None:
        process = FakeAppServerProcess([], silent=True)
        provider = CodexProvider(
            discovery_timeout=0.05,
            popen_factory=lambda *_args, **_kwargs: process,
        )

        with self.assertRaisesRegex(
            CatalogDiscoveryError,
            "timed out",
        ):
            provider._app_server_catalog(environment("codex"))

        self.assertIsNotNone(process.returncode)
        self.assertFalse(provider.discovery_processes)

    def test_app_server_malformed_json_is_rejected(self) -> None:
        process = FakeAppServerProcess([], malformed=True)
        provider = CodexProvider(
            discovery_timeout=1,
            popen_factory=lambda *_args, **_kwargs: process,
        )

        with self.assertRaisesRegex(CompatibilityError, "malformed JSON"):
            provider._app_server_catalog(environment("codex"))

    def test_app_server_failure_falls_back_to_dynamic_debug_catalog(self) -> None:
        provider = CodexProvider()
        fallback_models = (
            ModelCapability(
                id="fallback-model",
                display_name="Fallback Model",
                efforts=("one",),
                default_effort="one",
                input_modalities=("text",),
                is_default=False,
                capability_source="cli",
                efforts_verified=True,
            ),
        )
        with (
            mock.patch.object(
                provider,
                "_app_server_catalog",
                side_effect=CompatibilityError("model/list unavailable"),
            ),
            mock.patch.object(
                provider,
                "_debug_catalog",
                return_value=fallback_models,
            ),
        ):
            catalog = provider.discover_catalog(environment("codex"))

        self.assertEqual(catalog.models, fallback_models)

    def test_debug_catalog_includes_only_list_visibility(self) -> None:
        models = parse_codex_debug_models(
            {
                "models": [
                    {
                        "slug": "listed-model",
                        "display_name": "Listed",
                        "visibility": "list",
                        "default_reasoning_level": "normal",
                        "supported_reasoning_levels": [
                            {"effort": "normal"},
                        ],
                        "input_modalities": ["text", "image"],
                    },
                    {
                        "slug": "hidden-model",
                        "display_name": "Hidden",
                        "visibility": "hide",
                        "supported_reasoning_levels": [],
                    },
                ]
            }
        )

        self.assertEqual([model.id for model in models], ["listed-model"])
        self.assertEqual(models[0].efforts, ("normal",))

    def test_missing_and_unauthenticated_codex_are_distinct(self) -> None:
        provider = CodexProvider()
        with mock.patch.object(provider, "resolve_cli", return_value=None):
            missing = provider.inspect_environment()
        self.assertFalse(missing.installed)
        self.assertEqual(missing.status, "not_installed")

        resolution = CLIResolution(
            command=("fixture-codex",),
            executable_path=str(Path.cwd() / "fixture-codex.exe"),
        )
        with (
            mock.patch.object(
                provider,
                "resolve_cli",
                return_value=resolution,
            ),
            mock.patch.object(
                provider,
                "_version",
                return_value="fixture-version",
            ),
            mock.patch.object(
                provider,
                "_run_discovery_command",
                return_value=subprocess.CompletedProcess([], 1, "", ""),
            ),
        ):
            signed_out = provider.inspect_environment()
        self.assertTrue(signed_out.installed)
        self.assertFalse(signed_out.authenticated)
        self.assertEqual(signed_out.status, "auth_required")


class ClaudeCatalogTests(unittest.TestCase):
    def test_available_aliases_preserve_cli_punctuation_and_exclude_description(
        self,
    ) -> None:
        aliases = parse_claude_available_models(
            "Current model: Fixture (effort: focused)\n"
            "Available:\n"
            "family-one, family/two, family[wide], name.with-dot,\n"
            "or a full model ID."
        )

        self.assertEqual(
            aliases,
            (
                "family-one",
                "family/two",
                "family[wide]",
                "name.with-dot",
            ),
        )

    def test_account_restricted_list_is_not_expanded(self) -> None:
        aliases = parse_claude_available_models(
            "Available: account-only, or a full model ID."
        )
        self.assertEqual(aliases, ("account-only",))

    def test_wrapped_help_effort_choices_are_parsed(self) -> None:
        choices = parse_claude_effort_choices(
            "Usage: fixture\n"
            "  --effort <level>  Effort for this session\n"
            "                    (brief, focused,\n"
            "                     exhaustive)\n"
            "  --model <name>     Model alias\n"
        )
        self.assertEqual(choices, ("brief", "focused", "exhaustive"))

    def test_malformed_model_and_help_text_fail_closed(self) -> None:
        with self.assertRaisesRegex(CompatibilityError, "Available"):
            parse_claude_available_models("Current model: Fixture")
        self.assertEqual(
            parse_claude_effort_choices(
                "  --effort <level>  No parseable choices here\n"
                "  --model <name>  Model\n"
            ),
            (),
        )

    def test_zero_usage_rejects_api_time_cost_and_tokens(self) -> None:
        cases = (
            ("duration_api_ms", 1),
            ("total_cost_usd", 0.01),
            ("input_tokens", 1),
            ("output_tokens", 2),
        )
        for field, value in cases:
            with self.subTest(field=field):
                payload = zero_usage_payload("Available: fixture, or a full model ID.")
                if field.endswith("_tokens"):
                    payload["usage"][field] = value
                else:
                    payload[field] = value
                with self.assertRaises(InferenceDetectedError):
                    validate_claude_zero_usage(payload, exit_code=0)

    def test_zero_usage_requires_successful_json_accounting(self) -> None:
        payload = zero_usage_payload("Available: fixture, or a full model ID.")
        payload.pop("duration_api_ms")
        with self.assertRaisesRegex(CompatibilityError, "omitted"):
            validate_claude_zero_usage(payload, exit_code=0)
        with self.assertRaisesRegex(CompatibilityError, "exited"):
            validate_claude_zero_usage(
                zero_usage_payload("Available: fixture."),
                exit_code=2,
            )

    def test_zero_usage_requires_and_checks_camel_case_cache_accounting(
        self,
    ) -> None:
        payload = zero_usage_payload("Available: fixture, or a full model ID.")
        payload["usage"] = {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheCreationInputTokens": 0,
            "cacheReadInputTokens": 0,
        }
        self.assertIs(
            validate_claude_zero_usage(payload, exit_code=0),
            payload,
        )

        payload["usage"]["cacheReadInputTokens"] = 1
        with self.assertRaises(InferenceDetectedError):
            validate_claude_zero_usage(payload, exit_code=0)

        for field in (
            "inputTokens",
            "outputTokens",
            "cacheCreationInputTokens",
            "cacheReadInputTokens",
        ):
            with self.subTest(missing=field):
                missing_payload = zero_usage_payload(
                    "Available: fixture, or a full model ID."
                )
                missing_payload["usage"] = {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "cacheReadInputTokens": 0,
                }
                missing_payload["usage"].pop(field)
                with self.assertRaisesRegex(CompatibilityError, "omitted"):
                    validate_claude_zero_usage(
                        missing_payload,
                        exit_code=0,
                    )

    def test_current_effort_is_extracted_exactly(self) -> None:
        self.assertEqual(
            parse_claude_current_effort(
                "Current model: Fixture (effort: deep-focus)\nAvailable: fixture."
            ),
            "deep-focus",
        )

    def test_per_model_effort_probe_accepts_exact_and_rejects_normalized(
        self,
    ) -> None:
        provider = ClaudeProvider()
        current_environment = environment("claude")
        key = (
            str(current_environment.cli_path),
            str(current_environment.cli_version),
        )
        provider.effort_candidates[key] = (
            "brief",
            "focused",
            "extended",
        )

        def completed(
            args: list[str], **_kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            requested = args[args.index("--effort") + 1]
            effective = "focused" if requested == "extended" else requested
            payload = zero_usage_payload(
                f"Current model: Fixture (effort: {effective})\n"
                "Available: fixture, or a full model ID."
            )
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(payload),
                "",
            )

        with mock.patch.object(
            provider,
            "_run_discovery_command",
            side_effect=completed,
        ):
            result = provider.verify_model_efforts(
                current_environment,
                "fixture",
            )

        self.assertEqual(result.efforts, ("brief", "focused"))
        self.assertNotIn("extended", result.efforts)

    def test_unsupported_probe_uses_only_global_cli_choices_with_warning(
        self,
    ) -> None:
        provider = ClaudeProvider()
        current_environment = environment("claude")
        key = (
            str(current_environment.cli_path),
            str(current_environment.cli_version),
        )
        provider.effort_candidates[key] = ("mode-a", "mode-b")
        with mock.patch.object(
            provider,
            "_run_discovery_command",
            return_value=subprocess.CompletedProcess([], 0, "not-json", ""),
        ):
            result = provider.verify_model_efforts(
                current_environment,
                "fixture",
            )

        self.assertEqual(result.efforts, ("mode-a", "mode-b"))
        self.assertTrue(result.use_global_unverified)
        self.assertIn("unverified", str(result.warning))

    def test_inference_during_probe_stops_later_batches(self) -> None:
        provider = ClaudeProvider()
        current_environment = environment("claude")
        key = (
            str(current_environment.cli_path),
            str(current_environment.cli_version),
        )
        provider.effort_candidates[key] = (
            "mode-a",
            "mode-b",
            "mode-c",
            "mode-d",
        )
        calls: list[str] = []

        def completed(
            args: list[str], **_kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            requested = args[args.index("--effort") + 1]
            calls.append(requested)
            payload = zero_usage_payload(
                f"Current model: Fixture (effort: {requested})\nAvailable: fixture."
            )
            payload["usage"]["output_tokens"] = 1
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(payload),
                "",
            )

        with mock.patch.object(
            provider,
            "_run_discovery_command",
            side_effect=completed,
        ):
            result = provider.verify_model_efforts(
                current_environment,
                "fixture",
            )

        self.assertTrue(result.inference_detected)
        self.assertEqual(set(calls), {"mode-a", "mode-b"})

    def test_missing_and_unauthenticated_claude_are_distinct(self) -> None:
        provider = ClaudeProvider()
        with mock.patch.object(provider, "resolve_cli", return_value=None):
            missing = provider.inspect_environment()
        self.assertFalse(missing.installed)
        self.assertEqual(missing.status, "not_installed")

        resolution = CLIResolution(
            command=("fixture-claude",),
            executable_path=str(Path.cwd() / "fixture-claude.exe"),
        )
        auth_result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"loggedIn": False, "email": "not-returned@example.test"}),
            "",
        )
        with (
            mock.patch.object(
                provider,
                "resolve_cli",
                return_value=resolution,
            ),
            mock.patch.object(
                provider,
                "_version",
                return_value="fixture-version",
            ),
            mock.patch.object(
                provider,
                "_run_discovery_command",
                return_value=auth_result,
            ),
        ):
            signed_out = provider.inspect_environment()
        self.assertTrue(signed_out.installed)
        self.assertFalse(signed_out.authenticated)
        self.assertNotIn("example.test", str(signed_out))


class ProviderCommandAndStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolution = CLIResolution(
            command=("fixture-agent",),
            executable_path=str(Path.cwd() / "fixture-agent.exe"),
        )

    def test_automatic_omits_effort_for_both_providers(self) -> None:
        codex = build_codex_command(
            self.resolution,
            ProviderRunRequest(
                prompt="Work.",
                working_directory=Path.cwd(),
                model="catalog-model",
                effort=AUTOMATIC_EFFORT,
                session_id=None,
                new_session_id=None,
                persistent=True,
            ),
        )
        claude = build_claude_command(
            self.resolution,
            ProviderRunRequest(
                prompt="Work.",
                working_directory=Path.cwd(),
                model="catalog-alias",
                effort=AUTOMATIC_EFFORT,
                session_id=None,
                new_session_id=str(uuid.uuid4()),
                persistent=True,
            ),
        )

        self.assertNotIn("model_reasoning_effort", " ".join(codex))
        self.assertNotIn("--effort", claude)

    def test_large_prompts_use_stdin_instead_of_windows_command_line(self) -> None:
        prompt = "Long request " + ("x" * 120_000)
        codex_request = ProviderRunRequest(
            prompt=prompt,
            working_directory=Path.cwd(),
            model="catalog-model",
            effort=AUTOMATIC_EFFORT,
            session_id=None,
            new_session_id=None,
            persistent=False,
        )
        claude_request = ProviderRunRequest(
            prompt=prompt,
            working_directory=Path.cwd(),
            model="catalog-alias",
            effort=AUTOMATIC_EFFORT,
            session_id=None,
            new_session_id=None,
            persistent=False,
        )

        codex = build_codex_command(self.resolution, codex_request)
        claude = build_claude_command(self.resolution, claude_request)

        self.assertNotIn(prompt, codex)
        self.assertNotIn(prompt, claude)
        self.assertEqual(codex[-1], "-")
        self.assertLess(sum(len(argument) + 1 for argument in codex), 8_192)

        process = SilentRunProcess()
        provider = CodexProvider(popen_factory=lambda *_args, **_kwargs: process)
        timed_request = __import__("dataclasses").replace(
            codex_request,
            first_event_timeout=0.01,
            inactivity_timeout=2,
            total_timeout=3,
        )
        with (
            mock.patch.object(
                provider,
                "resolve_cli",
                return_value=self.resolution,
            ),
            mock.patch(
                "ogent_agent_providers.terminate_process_tree",
                side_effect=lambda _process: process.stop(),
            ),
        ):
            provider.run_agent(
                timed_request,
                on_process=lambda _process: None,
                on_activity=lambda _stream, _text: None,
                should_stop=lambda: False,
            )

        self.assertEqual(process.stdin.value, prompt)
        self.assertTrue(process.stdin.closed)

    def test_codex_ephemeral_run_ignores_user_config_and_defaults_read_only(
        self,
    ) -> None:
        command = build_codex_command(
            self.resolution,
            ProviderRunRequest(
                prompt="Work.",
                working_directory=Path.cwd(),
                model="catalog-model",
                effort=AUTOMATIC_EFFORT,
                session_id=None,
                new_session_id=None,
                persistent=False,
                office_document=Path.cwd() / "active.docx",
            ),
        )
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertEqual(
            command[command.index("--ask-for-approval") + 1],
            "never",
        )
        self.assertEqual(command[command.index("-s") + 1], "read-only")
        self.assertIn("mcp_servers.officecli.required=true", command)
        self.assertIn(
            (
                'mcp_servers.officecli.enabled_tools=["load_document_skill",'
                '"inspect_document","read_nodes","query_nodes",'
                '"apply_atomic_batch","validate_document","refresh_fields",'
                '"officecli"]'
            ),
            command,
        )
        self.assertIn(
            'mcp_servers.officecli.default_tools_approval_mode="approve"',
            command,
        )
        self.assertTrue(
            any("ogent_officecli_mcp.py" in argument for argument in command)
        )
        self.assertTrue(any("attachments_only" in argument for argument in command))
        self.assertNotIn("danger-full-access", command)

    def test_codex_rejects_danger_full_access(self) -> None:
        request = ProviderRunRequest(
            prompt="Work.",
            working_directory=Path.cwd(),
            model="catalog-model",
            effort=AUTOMATIC_EFFORT,
            session_id=None,
            new_session_id=None,
            persistent=False,
            sandbox="danger-full-access",
        )
        with self.assertRaisesRegex(ValueError, "Unsupported Codex sandbox"):
            build_codex_command(self.resolution, request)

    def test_claude_first_resume_and_reference_session_arguments(self) -> None:
        session_id = str(uuid.uuid4())
        base = dict(
            prompt="Work.",
            working_directory=Path.cwd(),
            model="catalog-alias",
            effort=AUTOMATIC_EFFORT,
        )
        first = build_claude_command(
            self.resolution,
            ProviderRunRequest(
                **base,
                session_id=None,
                new_session_id=session_id,
                persistent=True,
            ),
        )
        resumed = build_claude_command(
            self.resolution,
            ProviderRunRequest(
                **base,
                session_id=session_id,
                new_session_id=None,
                persistent=True,
            ),
        )
        ephemeral = build_claude_command(
            self.resolution,
            ProviderRunRequest(
                **base,
                session_id=None,
                new_session_id=None,
                persistent=False,
                extra_directories=(Path.cwd() / "temporary-reference",),
            ),
        )

        self.assertEqual(first[first.index("--session-id") + 1], session_id)
        self.assertNotIn("--resume", first)
        self.assertEqual(resumed[resumed.index("--resume") + 1], session_id)
        self.assertNotIn("--session-id", resumed)
        self.assertIn("--no-session-persistence", ephemeral)
        self.assertNotIn("--resume", ephemeral)
        self.assertNotIn("--session-id", ephemeral)

    def test_claude_uses_minimal_tools_without_permission_bypass(self) -> None:
        command = build_claude_command(
            self.resolution,
            ProviderRunRequest(
                prompt="Work.",
                working_directory=Path.cwd(),
                model="catalog-alias",
                effort=AUTOMATIC_EFFORT,
                session_id=None,
                new_session_id=str(uuid.uuid4()),
                persistent=True,
                office_document=Path.cwd() / "active.docx",
            ),
        )
        rendered = " ".join(command)
        self.assertIn("--verbose", command)
        self.assertNotIn("--safe-mode", command)
        self.assertEqual(
            command[command.index("--setting-sources") + 1],
            "",
        )
        self.assertIn("--strict-mcp-config", command)
        mcp_config = json.loads(command[command.index("--mcp-config") + 1])
        officecli_server = mcp_config["mcpServers"]["officecli"]
        self.assertEqual(officecli_server["type"], "stdio")
        self.assertEqual(officecli_server["command"], sys.executable)
        self.assertIn("ogent_officecli_mcp.py", officecli_server["args"][0])
        self.assertEqual(officecli_server["args"][1], "--document")
        self.assertEqual(
            officecli_server["args"][2],
            str(Path.cwd() / "active.docx"),
        )
        self.assertEqual(
            officecli_server["args"][3:5],
            ["--scope-mode", "attachments_only"],
        )
        self.assertNotIn("--allow-mutations", officecli_server["args"])
        self.assertNotIn("Bash", rendered)
        self.assertNotIn("Read", rendered)

        mutation_command = build_claude_command(
            self.resolution,
            ProviderRunRequest(
                prompt="Edit.",
                working_directory=Path.cwd(),
                model="catalog-alias",
                effort=AUTOMATIC_EFFORT,
                session_id=None,
                new_session_id=str(uuid.uuid4()),
                persistent=True,
                office_document=Path.cwd() / "active.docx",
                allow_document_mutation=True,
                scope_mode=ScopeMode.SELECTED_ONLY,
                allowed_document_paths=("/body/p[2]",),
            ),
        )
        mutation_config = json.loads(
            mutation_command[mutation_command.index("--mcp-config") + 1]
        )
        self.assertIn(
            "--allow-mutations",
            mutation_config["mcpServers"]["officecli"]["args"],
        )
        self.assertIn(
            "/body/p[2]",
            mutation_config["mcpServers"]["officecli"]["args"],
        )
        self.assertEqual(
            command[command.index("--allowedTools") + 1],
            (
                "mcp__officecli__load_document_skill,"
                "mcp__officecli__inspect_document,"
                "mcp__officecli__read_nodes,"
                "mcp__officecli__query_nodes,"
                "mcp__officecli__apply_atomic_batch,"
                "mcp__officecli__validate_document,"
                "mcp__officecli__refresh_fields,"
                "mcp__officecli__officecli"
            ),
        )
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("bypassPermissions", command)
        self.assertNotIn("--bare", command)

    def test_claude_stream_does_not_duplicate_partial_and_final_text(self) -> None:
        provider = ClaudeProvider()
        state = ClaudeStreamState()
        provider.parse_stream_event(
            state,
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Done."},
                },
            },
        )
        provider.parse_stream_event(
            state,
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Done."}]},
                "session_id": "session-fixture",
            },
        )
        provider.parse_stream_event(
            state,
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Done.",
                "session_id": "session-fixture",
                "usage": {"output_tokens": 3},
            },
        )

        self.assertEqual(state.final_text, "Done.")
        self.assertEqual(state.session_id, "session-fixture")
        self.assertEqual(state.usage["output_tokens"], 3)

    def test_first_event_timeout_is_actionable_and_terminates_process(
        self,
    ) -> None:
        process = SilentRunProcess()
        provider = CodexProvider(popen_factory=lambda *_args, **_kwargs: process)
        request = ProviderRunRequest(
            prompt="Work.",
            working_directory=Path.cwd(),
            model="catalog-model",
            effort=AUTOMATIC_EFFORT,
            session_id=None,
            new_session_id=None,
            persistent=False,
            first_event_timeout=0.01,
            inactivity_timeout=2,
            total_timeout=3,
        )
        phases: list[tuple[str, dict[str, Any]]] = []
        request = __import__("dataclasses").replace(
            request,
            phase_observer=lambda phase, detail: phases.append((phase, detail)),
        )
        with (
            mock.patch.object(
                provider,
                "resolve_cli",
                return_value=self.resolution,
            ),
            mock.patch(
                "ogent_agent_providers.terminate_process_tree",
                side_effect=lambda _process: process.stop(),
            ),
        ):
            result = provider.run_agent(
                request,
                on_process=lambda _process: None,
                on_activity=lambda _stream, _text: None,
                should_stop=lambda: False,
            )

        self.assertEqual(result.exit_code, 124)
        self.assertIn("produced no event", result.error_message or "")
        self.assertTrue(any(phase == "provider_timeout" for phase, _detail in phases))
        self.assertIsNotNone(process.poll())

    def test_structured_claude_error_is_captured(self) -> None:
        provider = ClaudeProvider()
        state = ClaudeStreamState()
        activity = provider.parse_stream_event(
            state,
            {
                "type": "error",
                "error": {"type": "permission_error", "message": "Denied"},
            },
        )
        self.assertEqual(state.error_message, "Denied")
        self.assertEqual(activity, "Denied")


OGENT_PATH = OGENT_DIR / "ogent.py"
OGENT_SPEC = importlib.util.spec_from_file_location(
    "ogent_session_under_test",
    OGENT_PATH,
)
if OGENT_SPEC is None or OGENT_SPEC.loader is None:
    raise RuntimeError(f"Could not load {OGENT_PATH}")
ogent = importlib.util.module_from_spec(OGENT_SPEC)
OGENT_SPEC.loader.exec_module(ogent)


class ProviderSessionTests(unittest.TestCase):
    def test_codex_and_claude_session_ids_never_cross(self) -> None:
        session = ogent.SessionState("fixture")
        session.codex_thread_id = "codex-thread"
        session.codex_model_id = "codex-model"
        session.claude_session_id = "claude-session"
        session.claude_model_id = "claude-model"

        self.assertEqual(session.codex_thread_id, "codex-thread")
        self.assertEqual(session.claude_session_id, "claude-session")
        self.assertNotEqual(session.codex_thread_id, session.claude_session_id)

    def test_two_documents_have_independent_provider_sessions(self) -> None:
        first = ogent.SessionState("first")
        second = ogent.SessionState("second")
        first.codex_thread_id = "thread-first"
        second.codex_thread_id = "thread-second"
        first.claude_session_id = "session-first"
        second.claude_session_id = "session-second"

        self.assertNotEqual(first.codex_thread_id, second.codex_thread_id)
        self.assertNotEqual(first.claude_session_id, second.claude_session_id)

    def test_model_change_uses_fresh_context_without_native_thread_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = ogent.SessionState("fixture")
            session.codex_thread_id = "old-thread"
            session.codex_model_id = "old-model"
            session.run_id = "run-id"
            session.run_status = "starting"
            session.run_complete.clear()
            captured: dict[str, Any] = {}

            def fake_codex(
                _session: Any,
                _prompt: str,
                _working: Path,
                thread_id: str | None,
                model: str,
                _effort: str,
                _run_id: str,
                **_kwargs: Any,
            ) -> tuple[int, str, str, list[str]]:
                captured["thread_id"] = thread_id
                captured["model"] = model
                return 0, "new-thread", "Done.", []

            timing = ogent.RunTiming(
                provider="codex",
                model="new-model",
                effort=AUTOMATIC_EFFORT,
                context_mode="fresh",
                prompt_bytes=0,
                attachment_count=0,
                materialized_bytes=0,
            )
            with (
                mock.patch.object(
                    ogent,
                    "ensure_watch",
                    return_value=None,
                ),
                mock.patch.object(
                    ogent,
                    "_run_codex_once",
                    side_effect=fake_codex,
                ),
                mock.patch.object(
                    ogent,
                    "run_quiet",
                    return_value=subprocess.CompletedProcess([], 0, "{}", ""),
                ),
            ):
                ogent._agent_worker(
                    session,
                    "Work.",
                    root / "working.docx",
                    root / "source.docx",
                    "codex",
                    "new-model",
                    AUTOMATIC_EFFORT,
                    "run-id",
                    [],
                    None,
                    None,
                    timing,
                )

            self.assertIsNone(captured["thread_id"])
            self.assertEqual(captured["model"], "new-model")
            self.assertEqual(session.codex_thread_id, "old-thread")
            self.assertEqual(session.codex_model_id, "old-model")

    def test_failed_and_stopped_codex_runs_discard_discovered_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = ogent.SessionState("fixture")
            provider = ogent.AGENT_PROVIDER_BY_ID["codex"]
            results = (
                ProviderRunResult(
                    exit_code=1,
                    session_id="failed-thread",
                    final_text=None,
                    stderr_tail=("failed",),
                    resumable=False,
                    usage={},
                ),
                ProviderRunResult(
                    exit_code=130,
                    session_id="stopped-thread",
                    final_text=None,
                    stderr_tail=(),
                    resumable=False,
                    usage={},
                ),
            )

            with mock.patch.object(
                provider,
                "run_agent",
                side_effect=results,
            ):
                for index, expected_code in enumerate((1, 130)):
                    run_id = f"run-{index}"
                    session.run_id = run_id
                    result = ogent._run_codex_once(
                        session,
                        "Work.",
                        root,
                        None,
                        "fixture-model",
                        AUTOMATIC_EFFORT,
                        run_id,
                    )
                    self.assertEqual(result[0], expected_code)
                    self.assertIsNone(result[1])

    def test_failed_or_stopped_worker_preserves_prior_codex_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for exit_code, stopped in ((1, False), (130, True)):
                with self.subTest(exit_code=exit_code, stopped=stopped):
                    session = ogent.SessionState(f"fixture-{exit_code}")
                    session.codex_thread_id = "prior-thread"
                    session.codex_model_id = "prior-model"
                    session.run_id = "run-id"
                    session.run_status = "starting"
                    session.run_complete.clear()

                    def fake_codex(
                        *_args: Any,
                        **_kwargs: Any,
                    ) -> tuple[int, str, None, list[str]]:
                        if stopped:
                            session.stop_requested = True
                        return exit_code, "unusable-thread", None, ["failed"]

                    with (
                        mock.patch.object(
                            ogent,
                            "ensure_watch",
                            return_value=None,
                        ),
                        mock.patch.object(
                            ogent,
                            "_run_codex_once",
                            side_effect=fake_codex,
                        ),
                    ):
                        timing = ogent.RunTiming(
                            provider="codex",
                            model="new-model",
                            effort=AUTOMATIC_EFFORT,
                            context_mode="fresh",
                            prompt_bytes=0,
                            attachment_count=0,
                            materialized_bytes=0,
                        )
                        ogent._agent_worker(
                            session,
                            "Work.",
                            root / "working.docx",
                            root / "source.docx",
                            "codex",
                            "new-model",
                            AUTOMATIC_EFFORT,
                            "run-id",
                            [],
                            None,
                            None,
                            timing,
                        )

                    self.assertEqual(session.codex_thread_id, "prior-thread")
                    self.assertEqual(session.codex_model_id, "prior-model")

    def test_every_claude_run_is_ephemeral_and_provider_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session = ogent.SessionState("fixture")
            session.run_id = "reference-run"
            provider = ogent.AGENT_PROVIDER_BY_ID["claude"]
            requests: list[ProviderRunRequest] = []

            def fake_run(
                request: ProviderRunRequest,
                **_kwargs: Any,
            ) -> ProviderRunResult:
                requests.append(request)
                return ProviderRunResult(
                    exit_code=0,
                    session_id=None,
                    final_text="Done.",
                    stderr_tail=(),
                    resumable=False,
                    usage={},
                )

            with mock.patch.object(provider, "run_agent", side_effect=fake_run):
                first = ogent._run_claude_once(
                    session,
                    "Reference prompt",
                    root,
                    None,
                    "fixture-model",
                    AUTOMATIC_EFFORT,
                    "reference-run",
                    ephemeral=True,
                    additional_directories=[root / "temporary-run"],
                )
                session.run_id = "normal-run"
                second = ogent._run_claude_once(
                    session,
                    "Normal prompt",
                    root,
                    None,
                    "fixture-model",
                    AUTOMATIC_EFFORT,
                    "normal-run",
                    ephemeral=False,
                    additional_directories=[],
                )

            self.assertIsNone(first[1])
            self.assertFalse(requests[0].persistent)
            self.assertIsNone(requests[0].session_id)
            self.assertIsNone(requests[0].new_session_id)
            self.assertEqual(
                requests[0].extra_directories,
                (root / "temporary-run",),
            )
            self.assertFalse(requests[1].persistent)
            self.assertIsNone(requests[1].session_id)
            self.assertIsNone(requests[1].new_session_id)
            self.assertEqual(requests[1].extra_directories, ())
            self.assertIsNone(second[1])

    def test_stop_targets_the_active_provider_process(self) -> None:
        session = ogent.SessionState("fixture")
        process = object()
        session.run_process = process  # type: ignore[assignment]
        session.run_status = "working"
        session.run_id = "run-id"
        with mock.patch.object(ogent, "terminate_process_tree") as terminate:
            stopped = ogent.stop_active_run(session)
        self.assertTrue(stopped)
        terminate.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()

"""Provider adapters for Codex CLI and Claude Code.

All provider-specific CLI resolution, discovery, command construction, stream
parsing, session extraction, and process cleanup lives in this module.
"""

from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import dataclasses
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ogent_agent_catalog import (
    AUTOMATIC_EFFORT,
    AgentCatalogError,
    CatalogDiscoveryError,
    EffortVerificationResult,
    ModelCapability,
    ProviderCatalog,
    ProviderEnvironment,
    normalize_executable_path,
    utc_now_iso,
)


APP_CLIENT_VERSION = "0.9.0"
DEFAULT_DISCOVERY_TIMEOUT = 20.0
DEFAULT_AGENT_TOOL_ALLOWLIST = (
    "Bash",
    "Read",
)
DEFAULT_AGENT_ALLOWED_TOOLS = (
    "Bash(officecli *)",
    "Read",
    "mcp__officecli__officecli",
)

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS_CHILD_FLAGS = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def provider_command_env() -> dict[str, str]:
    environment = os.environ.copy()
    environment["OFFICECLI_NO_AUTO_RESIDENT"] = "1"
    environment["OFFICECLI_RESIDENT_FLUSH"] = "each"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def terminate_process_tree(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()


@dataclasses.dataclass(frozen=True)
class CLIResolution:
    command: tuple[str, ...]
    executable_path: str


@dataclasses.dataclass(frozen=True)
class ProviderRunRequest:
    prompt: str
    working_directory: Path
    model: str
    effort: str
    session_id: str | None
    new_session_id: str | None
    persistent: bool
    image_paths: tuple[Path, ...] = ()
    sandbox: str = "danger-full-access"
    writable_directories: tuple[Path, ...] = ()
    extra_directories: tuple[Path, ...] = ()


@dataclasses.dataclass(frozen=True)
class ProviderRunResult:
    exit_code: int
    session_id: str | None
    final_text: str | None
    stderr_tail: tuple[str, ...]
    resumable: bool
    usage: dict[str, Any]
    error_message: str | None = None


@dataclasses.dataclass
class StreamState:
    session_id: str | None = None
    final_text: str | None = None
    error_message: str | None = None
    usage: dict[str, Any] = dataclasses.field(default_factory=dict)


class InferenceDetectedError(ValueError):
    pass


class CompatibilityError(ValueError):
    pass


def _first_nonempty_line(text: str) -> str | None:
    return next((line.strip() for line in text.splitlines() if line.strip()), None)


def _safe_detail(text: str, *, maximum: int = 500) -> str:
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text).strip()
    return clean[:maximum]


def _npm_javascript_resolution(
    command_name: str,
    package_parts: tuple[str, ...],
    entry_parts: tuple[str, ...],
) -> CLIResolution | None:
    command_path = shutil.which(f"{command_name}.cmd")
    node_path = shutil.which("node.exe") or shutil.which("node")
    if not command_path or not node_path:
        return None
    script = Path(command_path).parent.joinpath(
        "node_modules",
        *package_parts,
        *entry_parts,
    )
    if not script.is_file():
        return None
    return CLIResolution(
        command=(node_path, str(script)),
        executable_path=normalize_executable_path(str(script)),
    )


def _native_resolution(command_name: str) -> CLIResolution | None:
    candidates = (
        f"{command_name}.exe",
        command_name,
    )
    for candidate in candidates:
        path = shutil.which(candidate)
        if not path:
            continue
        suffix = Path(path).suffix.casefold()
        if suffix in {".cmd", ".bat", ".ps1"}:
            continue
        return CLIResolution(
            command=(path,),
            executable_path=normalize_executable_path(path),
        )
    return None


def resolve_codex_cli() -> CLIResolution | None:
    npm = _npm_javascript_resolution(
        "codex",
        ("@openai", "codex"),
        ("bin", "codex.js"),
    )
    return npm or _native_resolution("codex")


def resolve_claude_cli() -> CLIResolution | None:
    native = _native_resolution("claude")
    if native is not None:
        return native
    return _npm_javascript_resolution(
        "claude",
        ("@anthropic-ai", "claude-code"),
        ("cli.js",),
    )


def parse_codex_app_models(raw_models: Any) -> tuple[ModelCapability, ...]:
    if not isinstance(raw_models, list):
        raise CompatibilityError("Codex model/list returned an invalid model list.")
    models: list[ModelCapability] = []
    seen: set[str] = set()
    for raw in raw_models:
        if not isinstance(raw, dict) or bool(raw.get("hidden")):
            continue
        model_id = str(raw.get("id") or raw.get("model") or "").strip()
        if not model_id or model_id in seen:
            continue
        display_name = str(raw.get("displayName") or model_id).strip() or model_id
        raw_efforts = raw.get("supportedReasoningEfforts") or []
        if not isinstance(raw_efforts, list):
            raise CompatibilityError(
                "Codex returned invalid reasoning capabilities."
            )
        efforts: list[str] = []
        for item in raw_efforts:
            if isinstance(item, dict):
                effort = str(item.get("reasoningEffort") or "").strip()
            else:
                effort = ""
            if effort and effort not in efforts:
                efforts.append(effort)
        modalities = raw.get("inputModalities") or []
        if not isinstance(modalities, list):
            modalities = []
        default_effort = str(raw.get("defaultReasoningEffort") or "").strip()
        models.append(
            ModelCapability(
                id=model_id,
                display_name=display_name,
                efforts=tuple(efforts),
                default_effort=default_effort or None,
                input_modalities=tuple(
                    str(item).strip()
                    for item in modalities
                    if str(item).strip()
                ),
                is_default=bool(raw.get("isDefault")),
                capability_source="cli",
                efforts_verified=True,
            )
        )
        seen.add(model_id)
    if not models:
        raise CompatibilityError("Codex did not report any visible models.")
    return tuple(models)


def parse_codex_debug_models(payload: Any) -> tuple[ModelCapability, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise CompatibilityError("Codex debug models returned invalid JSON.")
    models: list[ModelCapability] = []
    seen: set[str] = set()
    for raw in payload["models"]:
        if not isinstance(raw, dict) or raw.get("visibility") != "list":
            continue
        model_id = str(raw.get("slug") or "").strip()
        if not model_id or model_id in seen:
            continue
        raw_efforts = raw.get("supported_reasoning_levels") or []
        if not isinstance(raw_efforts, list):
            raise CompatibilityError(
                "Codex debug models returned invalid reasoning capabilities."
            )
        efforts = tuple(
            dict.fromkeys(
                str(item.get("effort") or "").strip()
                for item in raw_efforts
                if isinstance(item, dict)
                and str(item.get("effort") or "").strip()
            )
        )
        modalities = raw.get("input_modalities") or []
        if not isinstance(modalities, list):
            modalities = []
        models.append(
            ModelCapability(
                id=model_id,
                display_name=(
                    str(raw.get("display_name") or model_id).strip() or model_id
                ),
                efforts=efforts,
                default_effort=(
                    str(raw.get("default_reasoning_level") or "").strip() or None
                ),
                input_modalities=tuple(
                    str(item).strip()
                    for item in modalities
                    if str(item).strip()
                ),
                is_default=bool(raw.get("is_default")),
                capability_source="cli",
                efforts_verified=True,
            )
        )
        seen.add(model_id)
    if not models:
        raise CompatibilityError(
            "Codex debug models did not report any listed models."
        )
    return tuple(models)


def parse_claude_available_models(result_text: str) -> tuple[str, ...]:
    match = re.search(r"\bAvailable\s*:\s*(.+)", result_text, re.IGNORECASE | re.DOTALL)
    if match is None:
        raise CompatibilityError("Claude /model output has no Available section.")
    available = match.group(1)
    available = re.split(
        r"(?:,\s*)?\bor\s+(?:an?\s+)?full\s+model\s+ID\b",
        available,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    available = available.strip().rstrip(".")
    identifiers: list[str] = []
    for raw in re.split(r",", available):
        identifier = re.sub(r"\s+", "", raw.strip())
        identifier = re.sub(r"^or(?=[A-Za-z0-9])", "", identifier)
        if not identifier:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.:/\[\]-]+", identifier):
            raise CompatibilityError(
                "Claude /model returned an unrecognized model identifier."
            )
        if identifier not in identifiers:
            identifiers.append(identifier)
    if not identifiers:
        raise CompatibilityError("Claude /model returned no available models.")
    return tuple(identifiers)


def parse_claude_current_effort(result_text: str) -> str | None:
    current_line = next(
        (
            line
            for line in result_text.splitlines()
            if re.search(r"\bCurrent\s+model\s*:", line, re.IGNORECASE)
        ),
        "",
    )
    match = re.search(r"\(\s*effort\s*:\s*([^)]+?)\s*\)", current_line)
    return match.group(1).strip() if match else None


def parse_claude_effort_choices(help_text: str) -> tuple[str, ...]:
    lines = help_text.splitlines()
    block: list[str] = []
    collecting = False
    for line in lines:
        if "--effort" in line and re.search(r"--effort\s+<[^>]+>", line):
            collecting = True
            block.append(line.strip())
            continue
        if not collecting:
            continue
        if re.match(r"^\s{0,4}-[-A-Za-z]", line):
            break
        if line.strip():
            block.append(line.strip())
    if not block:
        return ()
    text = " ".join(block)
    groups = re.findall(r"\(([^()]*)\)", text)
    for group in reversed(groups):
        choices = [item.strip() for item in group.split(",")]
        if (
            len(choices) >= 1
            and all(
                choice
                and re.fullmatch(r"[A-Za-z0-9_.:/\[\]-]+", choice)
                for choice in choices
            )
        ):
            return tuple(dict.fromkeys(choices))
    choice_match = re.search(
        r"\bchoices?\s*:\s*([A-Za-z0-9_.:/\[\]\s,-]+)",
        text,
        re.IGNORECASE,
    )
    if choice_match:
        choices = [
            item.strip()
            for item in choice_match.group(1).split(",")
            if item.strip()
        ]
        if choices and all(
            re.fullmatch(r"[A-Za-z0-9_.:/\[\]-]+", item)
            for item in choices
        ):
            return tuple(dict.fromkeys(choices))
    return ()


def _numeric_zero(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _walk_usage_values(value: Any) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if (
                normalized.endswith("tokens")
                or normalized.endswith("tokencount")
            ):
                items.append((str(key), child))
            items.extend(_walk_usage_values(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk_usage_values(child))
    return items


def _usage_token_category(key: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    if "cachecreation" in normalized or "cachewrite" in normalized:
        return "cache_creation"
    if "cacheread" in normalized or "cachedinput" in normalized:
        return "cache_read"
    if "output" in normalized:
        return "output"
    if "input" in normalized:
        return "input"
    return None


def validate_claude_zero_usage(
    payload: Any,
    *,
    exit_code: int,
) -> dict[str, Any]:
    if exit_code != 0:
        raise CompatibilityError(
            f"Claude /model exited with code {exit_code}."
        )
    if not isinstance(payload, dict):
        raise CompatibilityError("Claude /model did not return a JSON object.")
    if payload.get("is_error") is not False:
        raise CompatibilityError("Claude /model reported an error.")
    required_zero = (
        ("duration_api_ms", payload.get("duration_api_ms")),
        ("total_cost_usd", payload.get("total_cost_usd")),
    )
    missing = [name for name, value in required_zero if value is None]
    if missing:
        raise CompatibilityError(
            "Claude /model omitted zero-usage accounting fields."
        )
    nonzero = [
        name for name, value in required_zero if not _numeric_zero(value)
    ]
    token_values = _walk_usage_values(payload.get("usage") or {})
    token_values.extend(_walk_usage_values(payload.get("modelUsage") or {}))
    observed_categories = {
        category
        for name, _value in token_values
        if (category := _usage_token_category(name)) is not None
    }
    required_categories = {
        "input",
        "output",
        "cache_creation",
        "cache_read",
    }
    if not required_categories.issubset(observed_categories):
        raise CompatibilityError(
            "Claude /model omitted input/output/cache token accounting."
        )
    nonzero.extend(
        name for name, value in token_values if not _numeric_zero(value)
    )
    if nonzero:
        raise InferenceDetectedError(
            "Claude catalog discovery performed inference or reported "
            "nonzero usage; the result was rejected."
        )
    result = payload.get("result")
    if not isinstance(result, str) or not result.strip():
        raise CompatibilityError("Claude /model returned no result text.")
    return payload


def _build_codex_from_request(
    resolution: CLIResolution,
    request: ProviderRunRequest,
) -> list[str]:
    if request.sandbox not in {
        "read-only",
        "workspace-write",
        "danger-full-access",
    }:
        raise ValueError(f"Unsupported Codex sandbox: {request.sandbox}")
    effort_arguments: list[str] = []
    if request.effort != AUTOMATIC_EFFORT:
        effort_arguments = [
            "-c",
            f"model_reasoning_effort={json.dumps(request.effort)}",
        ]
    image_arguments = [
        argument
        for path in request.image_paths
        for argument in ("-i", str(path))
    ]
    if request.session_id:
        return [
            *resolution.command,
            "exec",
            "resume",
            "-m",
            request.model,
            *effort_arguments,
            "--json",
            "--skip-git-repo-check",
            *image_arguments,
            request.session_id,
            request.prompt,
        ]
    command = [
        *resolution.command,
        "exec",
        "-m",
        request.model,
        *effort_arguments,
        "-s",
        request.sandbox,
        "--color",
        "never",
        "--json",
        "--skip-git-repo-check",
    ]
    for directory in request.writable_directories:
        command.extend(["--add-dir", str(directory)])
    return [*command, *image_arguments, "--", request.prompt]


def _build_claude_from_request(
    resolution: CLIResolution,
    request: ProviderRunRequest,
) -> list[str]:
    command = [
        *resolution.command,
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--model",
        request.model,
    ]
    if request.effort != AUTOMATIC_EFFORT:
        command.extend(["--effort", request.effort])
    command.extend(
        [
            "--permission-mode",
            "dontAsk",
            "--tools",
            ",".join(DEFAULT_AGENT_TOOL_ALLOWLIST),
            "--allowedTools",
            ",".join(DEFAULT_AGENT_ALLOWED_TOOLS),
        ]
    )
    for directory in request.extra_directories:
        command.extend(["--add-dir", str(directory)])
    if not request.persistent:
        command.append("--no-session-persistence")
    elif request.session_id:
        command.extend(["--resume", request.session_id])
    else:
        if not request.new_session_id:
            raise ValueError("A new Claude session requires a UUID.")
        uuid.UUID(request.new_session_id)
        command.extend(["--session-id", request.new_session_id])
    command.append(request.prompt)
    return command


class BaseAgentProvider:
    provider_id = ""
    label = ""
    supports_model_effort_verification = False

    def __init__(
        self,
        *,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.discovery_timeout = discovery_timeout
        self.popen_factory = popen_factory
        self.process_lock = threading.RLock()
        self.discovery_processes: set[subprocess.Popen[str]] = set()

    def resolve_cli(self) -> CLIResolution | None:
        raise NotImplementedError

    def build_command(self, request: ProviderRunRequest) -> list[str]:
        raise NotImplementedError

    def new_stream_state(self) -> StreamState:
        return StreamState()

    def parse_stream_event(
        self,
        state: StreamState,
        event: dict[str, Any],
    ) -> str | None:
        raise NotImplementedError

    def _track_discovery(self, process: subprocess.Popen[str]) -> None:
        with self.process_lock:
            self.discovery_processes.add(process)

    def _untrack_discovery(self, process: subprocess.Popen[str]) -> None:
        with self.process_lock:
            self.discovery_processes.discard(process)

    def cancel_discovery(self) -> None:
        with self.process_lock:
            processes = tuple(self.discovery_processes)
        for process in processes:
            terminate_process_tree(process)

    def cancel_process(self, process: subprocess.Popen[str] | None) -> None:
        terminate_process_tree(process)

    def _run_discovery_command(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        process = self.popen_factory(
            args,
            env=provider_command_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._track_discovery(process)
        try:
            try:
                stdout, stderr = process.communicate(
                    timeout=timeout or self.discovery_timeout
                )
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
                process.communicate()
                raise CatalogDiscoveryError(
                    "Capability discovery timed out."
                ) from None
            return subprocess.CompletedProcess(
                args=args,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            self._untrack_discovery(process)

    def _version(self, resolution: CLIResolution) -> str:
        result = self._run_discovery_command(
            [*resolution.command, "--version"],
            timeout=min(self.discovery_timeout, 10),
        )
        version = _first_nonempty_line(result.stdout or result.stderr)
        if result.returncode != 0 or not version:
            raise CatalogDiscoveryError(
                "The CLI version check failed.",
                status="catalog_error",
            )
        return _safe_detail(version, maximum=200)

    def run_agent(
        self,
        request: ProviderRunRequest,
        *,
        on_process: Callable[[subprocess.Popen[str]], None],
        on_activity: Callable[[str, str], None],
        should_stop: Callable[[], bool],
    ) -> ProviderRunResult:
        resolution = self.resolve_cli()
        if resolution is None:
            raise RuntimeError(f"{self.label} CLI is not installed.")
        args = self.build_command_with_resolution(resolution, request)
        process = self.popen_factory(
            args,
            cwd=str(request.working_directory),
            env=provider_command_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            creationflags=WINDOWS_CHILD_FLAGS if os.name == "nt" else 0,
        )
        on_process(process)
        if should_stop():
            terminate_process_tree(process)
        assert process.stdout is not None
        assert process.stderr is not None
        output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def reader(pipe: Any, stream: str) -> None:
            for raw in iter(pipe.readline, ""):
                output_queue.put((stream, raw.rstrip("\r\n")))
            output_queue.put((stream, None))

        stdout_thread = threading.Thread(
            target=reader,
            args=(process.stdout, "stdout"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=reader,
            args=(process.stderr, "stderr"),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        state = self.new_stream_state()
        stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        closed = 0
        while closed < 2:
            stream, line = output_queue.get()
            if line is None:
                closed += 1
                continue
            if not line:
                continue
            if stream == "stderr":
                stderr_tail.append(line)
                on_activity("stderr", line)
                continue
            try:
                event = json.loads(line)
            except ValueError:
                on_activity(self.provider_id, line)
                continue
            if not isinstance(event, dict):
                on_activity(self.provider_id, line)
                continue
            activity = self.parse_stream_event(state, event)
            if activity:
                on_activity(self.provider_id, activity)
        exit_code = process.wait()
        if state.error_message and exit_code == 0:
            exit_code = 1
        return ProviderRunResult(
            exit_code=exit_code,
            session_id=state.session_id,
            final_text=state.final_text,
            stderr_tail=tuple(stderr_tail),
            resumable=bool(request.persistent and state.session_id and exit_code == 0),
            usage=dict(state.usage),
            error_message=state.error_message,
        )

    def build_command_with_resolution(
        self,
        resolution: CLIResolution,
        request: ProviderRunRequest,
    ) -> list[str]:
        raise NotImplementedError


class CodexProvider(BaseAgentProvider):
    provider_id = "codex"
    label = "Codex"

    def resolve_cli(self) -> CLIResolution | None:
        return resolve_codex_cli()

    def inspect_environment(self) -> ProviderEnvironment:
        resolution = self.resolve_cli()
        if resolution is None:
            return ProviderEnvironment(
                provider_id=self.provider_id,
                label=self.label,
                installed=False,
                authenticated=False,
                cli_path=None,
                cli_version=None,
                command=(),
                status="not_installed",
                warning="Codex CLI is not installed.",
            )
        try:
            version = self._version(resolution)
        except CatalogDiscoveryError as exc:
            return ProviderEnvironment(
                provider_id=self.provider_id,
                label=self.label,
                installed=True,
                authenticated=False,
                cli_path=resolution.executable_path,
                cli_version=None,
                command=resolution.command,
                status=exc.status,
                warning=str(exc),
            )
        auth = self._run_discovery_command(
            [*resolution.command, "login", "status"],
            timeout=min(self.discovery_timeout, 10),
        )
        authenticated = auth.returncode == 0
        return ProviderEnvironment(
            provider_id=self.provider_id,
            label=self.label,
            installed=True,
            authenticated=authenticated,
            cli_path=resolution.executable_path,
            cli_version=version,
            command=resolution.command,
            status="ready" if authenticated else "auth_required",
            warning=(
                None
                if authenticated
                else "Sign in with `codex login`, then refresh."
            ),
        )

    def _app_server_catalog(
        self,
        environment: ProviderEnvironment,
    ) -> tuple[ModelCapability, ...]:
        process = self.popen_factory(
            [
                *environment.command,
                "app-server",
                "--listen",
                "stdio://",
            ],
            env=provider_command_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            creationflags=WINDOWS_CHILD_FLAGS if os.name == "nt" else 0,
        )
        self._track_discovery(process)
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        responses: queue.Queue[dict[str, Any] | Exception | None] = queue.Queue()
        stderr_tail: collections.deque[str] = collections.deque(maxlen=10)

        def stdout_reader() -> None:
            for raw in process.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError as exc:
                    responses.put(
                        CompatibilityError(
                            f"Codex App Server returned malformed JSON: {exc}"
                        )
                    )
                    continue
                if not isinstance(value, dict):
                    responses.put(
                        CompatibilityError(
                            "Codex App Server returned a non-object response."
                        )
                    )
                    continue
                responses.put(value)
            responses.put(None)

        def stderr_reader() -> None:
            for raw in process.stderr:
                if raw.strip():
                    stderr_tail.append(raw.strip())

        threading.Thread(target=stdout_reader, daemon=True).start()
        threading.Thread(target=stderr_reader, daemon=True).start()
        deadline = time.monotonic() + self.discovery_timeout

        def send(payload: dict[str, Any]) -> None:
            process.stdin.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            process.stdin.flush()

        def wait_for(identifier: int) -> dict[str, Any]:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CatalogDiscoveryError(
                        "Codex App Server model discovery timed out."
                    )
                try:
                    item = responses.get(timeout=remaining)
                except queue.Empty:
                    raise CatalogDiscoveryError(
                        "Codex App Server model discovery timed out."
                    ) from None
                if item is None:
                    raise CompatibilityError(
                        "Codex App Server closed before returning the catalog."
                    )
                if isinstance(item, Exception):
                    raise item
                if item.get("id") != identifier:
                    continue
                if item.get("error"):
                    raise CompatibilityError(
                        "Codex App Server does not expose model/list."
                    )
                return item

        try:
            send(
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "ogent",
                            "title": "Ogent",
                            "version": APP_CLIENT_VERSION,
                        }
                    },
                }
            )
            wait_for(1)
            send({"method": "initialized", "params": {}})
            request_id = 2
            cursor: str | None = None
            seen_cursors: set[str] = set()
            raw_models: list[Any] = []
            while True:
                params: dict[str, Any] = {
                    "limit": 100,
                    "includeHidden": False,
                }
                if cursor is not None:
                    params["cursor"] = cursor
                send(
                    {
                        "method": "model/list",
                        "id": request_id,
                        "params": params,
                    }
                )
                response = wait_for(request_id)
                result = response.get("result")
                if not isinstance(result, dict) or not isinstance(
                    result.get("data"), list
                ):
                    raise CompatibilityError(
                        "Codex App Server returned an invalid model/list result."
                    )
                raw_models.extend(result["data"])
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    break
                cursor = str(next_cursor).strip()
                if not cursor or cursor in seen_cursors:
                    raise CompatibilityError(
                        "Codex App Server returned an invalid pagination cursor."
                    )
                seen_cursors.add(cursor)
                request_id += 1
            return parse_codex_app_models(raw_models)
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
            self._untrack_discovery(process)

    def _debug_catalog(
        self,
        environment: ProviderEnvironment,
    ) -> tuple[ModelCapability, ...]:
        result = self._run_discovery_command(
            [*environment.command, "debug", "models"],
            timeout=self.discovery_timeout,
        )
        if result.returncode != 0:
            raise CompatibilityError(
                f"Codex debug models exited with code {result.returncode}."
            )
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise CompatibilityError(
                f"Codex debug models returned malformed JSON: {exc}"
            ) from exc
        return parse_codex_debug_models(payload)

    def discover_catalog(
        self,
        environment: ProviderEnvironment,
    ) -> ProviderCatalog:
        app_error: Exception | None = None
        try:
            models = self._app_server_catalog(environment)
        except Exception as exc:
            app_error = exc
            try:
                models = self._debug_catalog(environment)
            except Exception as fallback_exc:
                detail = _safe_detail(str(fallback_exc))
                if app_error:
                    detail = (
                        f"{_safe_detail(str(app_error))} "
                        f"Fallback failed: {detail}"
                    ).strip()
                raise CatalogDiscoveryError(
                    "This CLI version does not expose a compatible catalog. "
                    + detail
                ) from fallback_exc
        return ProviderCatalog(
            provider_id=self.provider_id,
            label=self.label,
            installed=True,
            authenticated=True,
            cli_path=environment.cli_path,
            cli_version=environment.cli_version,
            status="ready",
            models=models,
            refreshed_at=utc_now_iso(),
            stale=False,
            warning=None,
        )

    def verify_model_efforts(
        self,
        environment: ProviderEnvironment,
        model_id: str,
    ) -> EffortVerificationResult:
        raise RuntimeError("Codex efforts are supplied per model by model/list.")

    def build_command_with_resolution(
        self,
        resolution: CLIResolution,
        request: ProviderRunRequest,
    ) -> list[str]:
        return _build_codex_from_request(resolution, request)

    def parse_stream_event(
        self,
        state: StreamState,
        event: dict[str, Any],
    ) -> str | None:
        event_type = str(event.get("type", ""))
        item = event.get("item")
        if event_type == "thread.started" and event.get("thread_id"):
            state.session_id = str(event["thread_id"])
            return "Codex context started."
        if event_type == "turn.started":
            return "Codex is working."
        if event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                state.usage.update(usage)
            output = state.usage.get("output_tokens")
            return (
                "Codex turn completed"
                + (f" ({output} output tokens)." if output is not None else ".")
            )
        if isinstance(item, dict):
            item_type = item.get("type")
            if (
                event_type == "item.completed"
                and item_type == "agent_message"
            ):
                text = str(item.get("text") or "").strip()
                if text:
                    state.final_text = text
            if item_type == "command_execution":
                command = item.get("command") or item.get("title") or "tool command"
                status = item.get("status") or event_type
                return f"{status}: {command}"
            if item_type == "reasoning":
                return "Codex reasoning step complete."
            if item_type == "error":
                message = str(item.get("message") or "")
                if "Skill descriptions were shortened" in message:
                    return None
                state.error_message = message or state.error_message
                return message or "Codex reported an error."
        if event_type == "error":
            message = str(event.get("message") or "Codex reported an error.")
            state.error_message = message
            return message
        return None


class ClaudeStreamState(StreamState):
    def __init__(self) -> None:
        super().__init__()
        self.partial_text: list[str] = []
        self.assistant_text: str | None = None


class ClaudeProvider(BaseAgentProvider):
    provider_id = "claude"
    label = "Claude Code"
    supports_model_effort_verification = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.effort_lock = threading.RLock()
        self.probe_semaphore = threading.BoundedSemaphore(2)
        self.effort_candidates: dict[tuple[str, str], tuple[str, ...]] = {}
        self.inference_blocked: set[tuple[str, str]] = set()

    def resolve_cli(self) -> CLIResolution | None:
        return resolve_claude_cli()

    def inspect_environment(self) -> ProviderEnvironment:
        resolution = self.resolve_cli()
        if resolution is None:
            return ProviderEnvironment(
                provider_id=self.provider_id,
                label=self.label,
                installed=False,
                authenticated=False,
                cli_path=None,
                cli_version=None,
                command=(),
                status="not_installed",
                warning="Claude Code is not installed.",
            )
        try:
            version = self._version(resolution)
        except CatalogDiscoveryError as exc:
            return ProviderEnvironment(
                provider_id=self.provider_id,
                label=self.label,
                installed=True,
                authenticated=False,
                cli_path=resolution.executable_path,
                cli_version=None,
                command=resolution.command,
                status=exc.status,
                warning=str(exc),
            )
        auth = self._run_discovery_command(
            [*resolution.command, "auth", "status", "--json"],
            timeout=min(self.discovery_timeout, 10),
        )
        authenticated = False
        if auth.returncode == 0:
            try:
                payload = json.loads(auth.stdout)
                authenticated = (
                    isinstance(payload, dict)
                    and payload.get("loggedIn") is True
                )
            except ValueError:
                authenticated = False
        return ProviderEnvironment(
            provider_id=self.provider_id,
            label=self.label,
            installed=True,
            authenticated=authenticated,
            cli_path=resolution.executable_path,
            cli_version=version,
            command=resolution.command,
            status="ready" if authenticated else "auth_required",
            warning=(
                None
                if authenticated
                else "Sign in with `claude auth login`, then refresh."
            ),
        )

    @staticmethod
    def _model_discovery_arguments(
        environment: ProviderEnvironment,
    ) -> list[str]:
        return [
            *environment.command,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--max-turns",
            "1",
            "--max-budget-usd",
            "0.000001",
            "--safe-mode",
            "/model",
        ]

    def _discover_models(
        self,
        environment: ProviderEnvironment,
    ) -> tuple[ModelCapability, ...]:
        result = self._run_discovery_command(
            self._model_discovery_arguments(environment),
            timeout=self.discovery_timeout,
        )
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise CompatibilityError(
                f"Claude /model returned malformed JSON: {exc}"
            ) from exc
        try:
            validate_claude_zero_usage(payload, exit_code=result.returncode)
        except InferenceDetectedError:
            if environment.cli_path and environment.cli_version:
                with self.effort_lock:
                    self.inference_blocked.add(
                        (environment.cli_path, environment.cli_version)
                    )
            raise
        identifiers = parse_claude_available_models(payload["result"])
        return tuple(
            ModelCapability(
                id=identifier,
                display_name=identifier,
                efforts=(),
                default_effort=None,
                input_modalities=(),
                is_default=False,
                capability_source="cli",
                efforts_verified=False,
            )
            for identifier in identifiers
        )

    def _discover_global_efforts(
        self,
        environment: ProviderEnvironment,
    ) -> tuple[str, ...]:
        result = self._run_discovery_command(
            [*environment.command, "--help"],
            timeout=min(self.discovery_timeout, 10),
        )
        if result.returncode != 0:
            return ()
        return parse_claude_effort_choices(result.stdout)

    def discover_catalog(
        self,
        environment: ProviderEnvironment,
    ) -> ProviderCatalog:
        key = (environment.cli_path or "", environment.cli_version or "")
        with self.effort_lock:
            self.inference_blocked.discard(key)
        try:
            models = self._discover_models(environment)
        except InferenceDetectedError as exc:
            raise CatalogDiscoveryError(
                str(exc),
                status="catalog_error",
            ) from exc
        except Exception as exc:
            raise CatalogDiscoveryError(
                "This CLI version does not expose a compatible catalog. "
                + _safe_detail(str(exc))
            ) from exc
        efforts = self._discover_global_efforts(environment)
        with self.effort_lock:
            self.effort_candidates[key] = efforts
        warning = None
        if not efforts:
            warning = (
                "No model-specific effort control; using CLI default. "
                "This CLI help output did not expose compatible effort choices."
            )
        return ProviderCatalog(
            provider_id=self.provider_id,
            label=self.label,
            installed=True,
            authenticated=True,
            cli_path=environment.cli_path,
            cli_version=environment.cli_version,
            status="ready",
            models=models,
            refreshed_at=utc_now_iso(),
            stale=False,
            warning=warning,
        )

    @staticmethod
    def _effort_probe_arguments(
        environment: ProviderEnvironment,
        model_id: str,
        effort: str,
    ) -> list[str]:
        return [
            *environment.command,
            "--model",
            model_id,
            "--effort",
            effort,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--max-turns",
            "1",
            "--max-budget-usd",
            "0.000001",
            "--safe-mode",
            "/model",
        ]

    def _probe_effort(
        self,
        environment: ProviderEnvironment,
        model_id: str,
        effort: str,
    ) -> tuple[str, str]:
        with self.probe_semaphore:
            result = self._run_discovery_command(
                self._effort_probe_arguments(environment, model_id, effort),
                timeout=self.discovery_timeout,
            )
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return "unsupported", effort
        try:
            validate_claude_zero_usage(payload, exit_code=result.returncode)
        except InferenceDetectedError:
            return "inference", effort
        except CompatibilityError:
            return "rejected", effort
        effective = parse_claude_current_effort(payload["result"])
        if effective is None:
            return "unsupported", effort
        warnings = payload.get("warnings")
        warning_text = " ".join(
            (
                str(warnings or ""),
                result.stderr or "",
                payload["result"],
            )
        )
        has_warning = bool(
            warnings
            or result.stderr.strip()
            or re.search(
                r"\b(?:warning|downgrad|normaliz)\w*\b",
                warning_text,
                re.IGNORECASE,
            )
        )
        if effective == effort and not has_warning:
            return "accepted", effort
        return "rejected", effort

    def verify_model_efforts(
        self,
        environment: ProviderEnvironment,
        model_id: str,
    ) -> EffortVerificationResult:
        key = (environment.cli_path or "", environment.cli_version or "")
        with self.effort_lock:
            candidates = self.effort_candidates.get(key, ())
            blocked = key in self.inference_blocked
        if blocked:
            return EffortVerificationResult(
                efforts=(),
                warning=(
                    "No model-specific effort control; using CLI default. "
                    "A zero-inference probe reported nonzero usage."
                ),
                inference_detected=True,
            )
        if not candidates:
            return EffortVerificationResult(
                efforts=(),
                warning=(
                    "No model-specific effort control; using CLI default."
                ),
            )
        accepted: set[str] = set()
        rejected = 0
        unsupported = 0
        for offset in range(0, len(candidates), 2):
            batch = candidates[offset : offset + 2]
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="ogent-claude-probe",
            ) as executor:
                futures = [
                    executor.submit(
                        self._probe_effort,
                        environment,
                        model_id,
                        effort,
                    )
                    for effort in batch
                ]
                results = [future.result() for future in futures]
            if any(status == "inference" for status, _ in results):
                with self.effort_lock:
                    self.inference_blocked.add(key)
                return EffortVerificationResult(
                    efforts=(),
                    warning=(
                        "No model-specific effort control; using CLI default. "
                        "A zero-inference effort probe reported nonzero usage."
                    ),
                    inference_detected=True,
                )
            for status, effort in results:
                if status == "accepted":
                    accepted.add(effort)
                elif status == "unsupported":
                    unsupported += 1
                else:
                    rejected += 1
        ordered = tuple(
            effort for effort in candidates if effort in accepted
        )
        if ordered:
            return EffortVerificationResult(efforts=ordered)
        if unsupported == len(candidates) and not rejected:
            return EffortVerificationResult(
                efforts=candidates,
                warning=(
                    "CLI-valid; model-specific support unverified."
                ),
                use_global_unverified=True,
            )
        return EffortVerificationResult(
            efforts=(),
            warning="No model-specific effort control; using CLI default.",
        )

    def build_command_with_resolution(
        self,
        resolution: CLIResolution,
        request: ProviderRunRequest,
    ) -> list[str]:
        return _build_claude_from_request(resolution, request)

    def new_stream_state(self) -> StreamState:
        return ClaudeStreamState()

    @staticmethod
    def _message_text(message: Any) -> str | None:
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if isinstance(content, str):
            return content.strip() or None
        if not isinstance(content, list):
            return None
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "".join(parts).strip()
        return text or None

    def parse_stream_event(
        self,
        state: StreamState,
        event: dict[str, Any],
    ) -> str | None:
        assert isinstance(state, ClaudeStreamState)
        event_type = str(event.get("type") or "")
        session_id = event.get("session_id") or event.get("sessionId")
        if session_id:
            state.session_id = str(session_id)
        if event_type == "system":
            if event.get("subtype") == "init":
                return "Claude context started."
            return "Claude is preparing the session."
        if event_type == "stream_event":
            inner = event.get("event")
            if not isinstance(inner, dict):
                return None
            inner_type = str(inner.get("type") or "")
            if inner_type == "content_block_start":
                block = inner.get("content_block")
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        return (
                            "Claude is using "
                            + str(block.get("name") or "a tool")
                            + "."
                        )
                    if block.get("type") in {"thinking", "reasoning"}:
                        return "Claude is reasoning."
            if inner_type == "content_block_delta":
                delta = inner.get("delta")
                if isinstance(delta, dict):
                    if delta.get("type") == "text_delta":
                        text = str(delta.get("text") or "")
                        if text:
                            state.partial_text.append(text)
                    elif delta.get("type") in {
                        "thinking_delta",
                        "reasoning_delta",
                    }:
                        return "Claude is reasoning."
            if inner_type == "message_start":
                message = inner.get("message")
                if isinstance(message, dict) and isinstance(
                    message.get("usage"), dict
                ):
                    state.usage.update(message["usage"])
            if inner_type == "message_delta":
                usage = inner.get("usage")
                if isinstance(usage, dict):
                    state.usage.update(usage)
            return None
        if event_type == "assistant":
            text = self._message_text(event.get("message"))
            if text:
                state.assistant_text = text
            message = event.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    tool_names = [
                        str(block.get("name") or "a tool")
                        for block in content
                        if isinstance(block, dict)
                        and block.get("type") == "tool_use"
                    ]
                    if tool_names:
                        return f"Claude requested {tool_names[-1]}."
            return None
        if event_type == "user":
            message = event.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list) and any(
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    for block in content
                ):
                    return "Claude received a tool result."
            return None
        if event_type == "result":
            usage = event.get("usage")
            if isinstance(usage, dict):
                state.usage.update(usage)
            for key in (
                "total_cost_usd",
                "duration_api_ms",
                "duration_ms",
                "num_turns",
            ):
                value = event.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    state.usage[key] = value
            result_text = event.get("result")
            if isinstance(result_text, str) and result_text.strip():
                state.final_text = result_text.strip()
            elif state.assistant_text:
                state.final_text = state.assistant_text
            elif state.partial_text:
                state.final_text = "".join(state.partial_text).strip() or None
            if event.get("is_error") is True or event.get("subtype") in {
                "error",
                "failed",
            }:
                state.error_message = (
                    state.final_text
                    or str(event.get("error") or "Claude reported an error.")
                )
                return state.error_message
            output = state.usage.get("output_tokens")
            return (
                "Claude turn completed"
                + (f" ({output} output tokens)." if output is not None else ".")
            )
        if event_type == "error":
            error = event.get("error")
            if isinstance(error, dict):
                message = str(
                    error.get("message")
                    or error.get("type")
                    or "Claude reported an error."
                )
            else:
                message = str(error or event.get("message") or "Claude reported an error.")
            state.error_message = message
            return message
        return None


def build_default_providers() -> tuple[BaseAgentProvider, ...]:
    return (CodexProvider(), ClaudeProvider())


def build_codex_command(*args: Any, **kwargs: Any) -> list[str]:
    """Build Codex arguments for either normalized or legacy Ogent callers."""

    if (
        len(args) == 2
        and isinstance(args[0], CLIResolution)
        and isinstance(args[1], ProviderRunRequest)
    ):
        return _build_codex_from_request(args[0], args[1])
    if len(args) < 4:
        raise TypeError("Legacy Codex command construction requires four arguments.")
    prompt = str(args[0])
    session_id = args[1]
    model = str(args[2])
    effort = str(args[3])
    image_paths = args[4] if len(args) > 4 else None
    resolution = resolve_codex_cli()
    if resolution is None:
        raise AgentCatalogError(
            "Codex CLI is not installed.",
            code="not_installed",
        )
    request = ProviderRunRequest(
        prompt=prompt,
        working_directory=Path.cwd(),
        model=model,
        effort=effort,
        session_id=str(session_id) if session_id else None,
        new_session_id=None,
        persistent=True,
        image_paths=tuple(image_paths or ()),
        sandbox=str(kwargs.get("sandbox", "danger-full-access")),
        writable_directories=tuple(
            kwargs.get("writable_directories") or ()
        ),
    )
    return _build_codex_from_request(resolution, request)


def build_claude_command(*args: Any, **kwargs: Any) -> list[str]:
    """Build Claude arguments for normalized or stdin-based legacy callers."""

    if (
        len(args) == 2
        and isinstance(args[0], CLIResolution)
        and isinstance(args[1], ProviderRunRequest)
    ):
        return _build_claude_from_request(args[0], args[1])
    if len(args) < 2:
        raise TypeError("Legacy Claude command construction requires model and effort.")
    model = str(args[0])
    effort = str(args[1])
    session_id = kwargs.get("session_id")
    resume = bool(kwargs.get("resume"))
    ephemeral = bool(kwargs.get("ephemeral"))
    additional_directories = tuple(
        Path(item) for item in (kwargs.get("additional_directories") or ())
    )
    resolution = resolve_claude_cli()
    if resolution is None:
        raise AgentCatalogError(
            "Claude Code is not installed.",
            code="not_installed",
        )
    command = [
        *resolution.command,
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--model",
        model,
    ]
    if effort != AUTOMATIC_EFFORT:
        command.extend(["--effort", effort])
    command.extend(
        [
            "--permission-mode",
            "dontAsk",
            "--tools",
            ",".join(DEFAULT_AGENT_TOOL_ALLOWLIST),
            "--allowedTools",
            ",".join(DEFAULT_AGENT_ALLOWED_TOOLS),
        ]
    )
    for directory in additional_directories:
        command.extend(["--add-dir", str(directory)])
    if ephemeral:
        command.append("--no-session-persistence")
    elif resume:
        if not session_id:
            raise ValueError("A resumed Claude run requires a session ID.")
        command.extend(["--resume", str(session_id)])
    else:
        if not session_id:
            raise ValueError("A new Claude run requires a session ID.")
        uuid.UUID(str(session_id))
        command.extend(["--session-id", str(session_id)])
    return command


def activity_from_codex_event(event: dict[str, Any]) -> str | None:
    state = StreamState()
    return CodexProvider().parse_stream_event(state, event)


def claude_session_id(event: dict[str, Any]) -> str | None:
    value = event.get("session_id") or event.get("sessionId")
    if value:
        return str(value)
    if isinstance(event.get("message"), dict):
        value = event["message"].get("session_id")
        if value:
            return str(value)
    return None


def claude_final_text(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type") or "")
    if event_type == "result":
        result = event.get("result")
        return result.strip() if isinstance(result, str) and result.strip() else None
    if event_type == "assistant":
        return ClaudeProvider._message_text(event.get("message"))
    return None


def activity_from_claude_event(event: dict[str, Any]) -> str | None:
    state = ClaudeStreamState()
    return ClaudeProvider().parse_stream_event(state, event)

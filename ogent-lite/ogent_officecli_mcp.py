#!/usr/bin/env python3
"""Least-privilege OfficeCLI MCP gateway for one Ogent document."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from ogent_app.domain.run import ScopeMode
from ogent_app.infrastructure.officecli.mcp_entrypoint import (
    parse_arguments as parse_gateway_arguments,
    run_stdio_gateway,
)
from ogent_app.infrastructure.officecli.mcp_tools import tool_definitions


APP_VERSION = "1.0.0"
MAX_COMMAND_CHARACTERS = 256 * 1024
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SUPPORTED_DOCUMENT_SUFFIXES = {".docx", ".xlsx", ".pptx"}
SAFE_DOCUMENT_VERBS = {
    "add",
    "add-part",
    "batch",
    "dump",
    "get",
    "move",
    "query",
    "raw",
    "raw-set",
    "refresh",
    "remove",
    "set",
    "swap",
    "validate",
    "view",
}
READ_ONLY_DOCUMENT_VERBS = {
    "dump",
    "get",
    "query",
    "raw",
    "validate",
    "view",
}
SAFE_VIEW_MODES = {
    "annotated",
    "forms",
    "issues",
    "outline",
    "stats",
    "text",
}
RESTRICTED_SCOPE_MODES = {
    ScopeMode.SELECTED_ONLY,
    ScopeMode.LOCAL_REGION,
    ScopeMode.SPECIFIED_SECTIONS,
    ScopeMode.SPECIFIED_SHEETS,
    ScopeMode.SPECIFIED_SLIDES,
}
FORBIDDEN_ARGUMENTS = {
    "--best-effort",
    "--browser",
    "--force",
    "--input",
    "--out",
    "-o",
}
SAFE_SKILL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)^[A-Z]:[\\/]")
EXTERNAL_FILE_PREFIXES = (
    "file:",
    "image:",
    "preview:",
    "source:",
)
EXTERNAL_FILE_SUFFIXES = {
    ".bmp",
    ".csv",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".pptx",
    ".svg",
    ".tif",
    ".tiff",
    ".tsv",
    ".txt",
    ".webp",
    ".xlsx",
    ".xml",
}


class OfficeCLIGatewayError(RuntimeError):
    """A validation or execution failure safe to return through MCP."""


def normalized_option_name(argument: str) -> str:
    """Return the option name represented by one command-line token."""

    lowered = argument.casefold()
    if lowered.startswith("--"):
        return lowered.split("=", 1)[0]
    if lowered.startswith("-o"):
        # System.CommandLine accepts both ``-o=value`` and attached ``-ovalue``.
        return "-o"
    return lowered


@dataclasses.dataclass(frozen=True)
class GatewayResult:
    exit_code: int
    text: str
    audit_id: str | None = None


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def split_command(command: str) -> list[str]:
    """Split an OfficeCLI command without invoking a command shell."""
    if "\x00" in command or "\r" in command or "\n" in command:
        raise OfficeCLIGatewayError("OfficeCLI commands must be one line.")
    if not command.strip():
        raise OfficeCLIGatewayError("The OfficeCLI command is empty.")
    if len(command) > MAX_COMMAND_CHARACTERS:
        raise OfficeCLIGatewayError("The OfficeCLI command is too large.")
    if os.name != "nt":
        try:
            return shlex.split(command, posix=True)
        except ValueError as exc:
            raise OfficeCLIGatewayError(
                "The OfficeCLI command has invalid quoting."
            ) from exc

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    pointer = command_line_to_argv(command, ctypes.byref(argc))
    if not pointer:
        raise OfficeCLIGatewayError("The OfficeCLI command could not be parsed.")
    try:
        return [pointer[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(pointer)


def iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_string_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_string_values(item)


def iter_batch_stable_paths(value: Any) -> Iterable[str]:
    if isinstance(value, list):
        for item in value:
            yield from iter_batch_stable_paths(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if (
                str(key).casefold()
                in {"path", "source", "target", "from", "to"}
                and isinstance(item, str)
                and item.strip().startswith("/")
            ):
                yield item
            else:
                yield from iter_batch_stable_paths(item)


class OfficeCLIGate:
    """Validate commands and execute OfficeCLI without a shell."""

    def __init__(
        self,
        document: Path | None,
        *,
        read_roots: Iterable[Path] = (),
        executable: Path | None = None,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        allow_mutations: bool = False,
        scope_mode: ScopeMode | str = ScopeMode.ATTACHMENTS_ONLY,
        allowed_document_paths: Iterable[str] = (),
        audit_log: Path | None = None,
        run_id: str | None = None,
        document_revision: int | None = None,
        skill_name: str | None = None,
        skill_sha256: str | None = None,
        initial_package_sha256: str | None = None,
    ) -> None:
        self.document = (
            self._validate_document(Path(document)) if document is not None else None
        )
        self.read_roots = tuple(
            self._validate_read_root(Path(root)) for root in read_roots
        )
        resolved_executable = executable or self._find_officecli()
        self.executable = Path(resolved_executable).resolve(strict=True)
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.runner = runner
        self.allow_mutations = bool(allow_mutations)
        self.scope_mode = ScopeMode(scope_mode)
        self.allowed_document_paths = tuple(
            self._normalize_stable_path(path)
            for path in allowed_document_paths
        )
        self.run_id = str(run_id or "")
        self.document_revision = (
            max(0, int(document_revision))
            if document_revision is not None
            else None
        )
        self.skill_name = str(skill_name or "") or None
        self.skill_sha256 = str(skill_sha256 or "") or None
        self.initial_package_sha256 = (
            str(initial_package_sha256 or "") or None
        )
        self.audit_lock = threading.RLock()
        self.audit_log = (
            self._validate_audit_log(Path(audit_log))
            if audit_log is not None
            else None
        )
        if (
            self.initial_package_sha256 is not None
            and self._hash_file(self.document) != self.initial_package_sha256
        ):
            raise OfficeCLIGatewayError(
                "The active document changed after capability bootstrap."
            )

    def _validate_audit_log(self, path: Path) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", self.run_id):
            raise OfficeCLIGatewayError(
                "A gateway audit log requires a valid run identifier."
            )
        resolved = path.resolve(strict=False)
        parent = resolved.parent.resolve(strict=True)
        if (
            path.exists()
            and path.is_symlink()
            or parent.is_symlink()
            or path.name != f"{self.run_id}.jsonl"
            or not any(
                path_is_within(parent, root)
                for root in (
                    *self.read_roots,
                    *(
                        (self.document.parent,)
                        if self.document is not None
                        else ()
                    ),
                )
            )
        ):
            raise OfficeCLIGatewayError(
                "The OfficeCLI audit path is outside this run."
            )
        return resolved

    @staticmethod
    def _find_officecli() -> Path:
        executable = shutil.which("officecli")
        if not executable:
            raise OfficeCLIGatewayError("OfficeCLI is not installed.")
        return Path(executable)

    @staticmethod
    def _validate_document(path: Path) -> Path:
        resolved = path.resolve(strict=True)
        if (
            path.is_symlink()
            or not resolved.is_file()
            or resolved.suffix.casefold() not in SUPPORTED_DOCUMENT_SUFFIXES
        ):
            raise OfficeCLIGatewayError(
                "The active Office document failed validation."
            )
        return resolved

    @staticmethod
    def _validate_read_root(path: Path) -> Path:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_dir():
            raise OfficeCLIGatewayError(
                "An OfficeCLI read root failed validation."
            )
        return resolved

    def _resolve_document_argument(self, value: str) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute() and self.document is not None:
            candidate = self.document.parent / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise OfficeCLIGatewayError(
                "The requested Office document does not exist."
            ) from exc
        if self.document is None or resolved != self.document:
            raise OfficeCLIGatewayError(
                "OfficeCLI is restricted to the active Ogent document."
            )
        if candidate.is_symlink() or not resolved.is_file():
            raise OfficeCLIGatewayError(
                "The active Office document path is unsafe."
            )
        return resolved

    def _external_path_candidate(self, value: str) -> Path | None:
        candidate = value.strip().strip("\"'")
        lowered = candidate.casefold()
        explicitly_file_scoped = False
        for prefix in EXTERNAL_FILE_PREFIXES:
            if lowered.startswith(prefix):
                candidate = candidate[len(prefix) :].strip().strip("\"'")
                explicitly_file_scoped = True
                break
        if WINDOWS_ABSOLUTE_PATH.match(candidate) or candidate.startswith("\\\\"):
            return Path(candidate)
        if os.name != "nt" and candidate.startswith("/"):
            if re.match(
                r"^/(?:body|slide\[|sheet\[|[A-Za-z0-9 _.-]+/[A-Z]+\d+)",
                candidate,
                flags=re.IGNORECASE,
            ):
                return None
            return Path(candidate)
        if candidate.startswith("/"):
            # Ogent's Windows OfficeCLI paths are rooted selectors such as
            # /body/p[1], /slide[1]/shape[2], and /Sheet1/B2:C3.
            return None
        path = Path(candidate)
        looks_relative = (
            explicitly_file_scoped
            or candidate.startswith((".", ".."))
            or "\\" in candidate
            or path.suffix.casefold() in EXTERNAL_FILE_SUFFIXES
        )
        if looks_relative:
            base = self.document.parent if self.document is not None else Path.cwd()
            return base / path
        return None

    def _validate_external_path(self, value: str) -> None:
        candidate = self._external_path_candidate(value)
        if candidate is None:
            return
        resolved = candidate.resolve(strict=False)
        if self.document is not None and resolved == self.document:
            return
        if any(path_is_within(resolved, root) for root in self.read_roots):
            return
        raise OfficeCLIGatewayError(
            "OfficeCLI command paths are restricted to the active document "
            "and this run's read-only references."
        )

    def _validate_argument_paths(self, arguments: list[str]) -> None:
        for index, value in enumerate(arguments):
            if "=" in value:
                self._validate_external_path(value.split("=", 1)[1])
            else:
                self._validate_external_path(value)
            if value.casefold() == "--commands" and index + 1 < len(arguments):
                try:
                    commands = json.loads(arguments[index + 1])
                except (TypeError, ValueError) as exc:
                    raise OfficeCLIGatewayError(
                        "The OfficeCLI batch command JSON is invalid."
                    ) from exc
                for nested_value in iter_string_values(commands):
                    self._validate_external_path(nested_value)

    @staticmethod
    def _normalize_stable_path(value: str) -> str:
        path = str(value).strip()
        if (
            not path.startswith("/")
            or path == "/"
            or len(path) > 2_048
            or any(character in path for character in "\x00\r\n")
        ):
            raise OfficeCLIGatewayError(
                "A scoped OfficeCLI stable path is invalid."
            )
        return path.rstrip("/")

    def _stable_path_is_allowed(self, value: str) -> bool:
        try:
            candidate = self._normalize_stable_path(value).casefold()
        except OfficeCLIGatewayError:
            return False
        return any(
            candidate == allowed.casefold()
            or candidate.startswith(f"{allowed.casefold()}/")
            for allowed in self.allowed_document_paths
        )

    def _validate_scope(self, verb: str, arguments: list[str]) -> None:
        if self.scope_mode is ScopeMode.WHOLE_DOCUMENT:
            return
        if self.scope_mode is ScopeMode.ATTACHMENTS_ONLY:
            raise OfficeCLIGatewayError(
                "OfficeCLI document access is disabled for an attachments-only run."
            )
        if self.scope_mode not in RESTRICTED_SCOPE_MODES:
            raise OfficeCLIGatewayError("The OfficeCLI scope mode is unsupported.")
        if verb == "validate":
            return
        if verb == "view" and len(arguments) >= 3:
            if arguments[2].casefold() == "stats":
                return
            raise OfficeCLIGatewayError(
                "Whole-document OfficeCLI views are not allowed in this scoped run."
            )
        if not self.allowed_document_paths:
            raise OfficeCLIGatewayError(
                "This scoped run has no authorized document paths."
            )

        selectors: list[str] = []
        if verb == "batch":
            try:
                command_index = arguments.index("--commands")
                commands = json.loads(arguments[command_index + 1])
            except (ValueError, IndexError, TypeError) as exc:
                raise OfficeCLIGatewayError(
                    "A scoped OfficeCLI batch requires inline command JSON."
                ) from exc
            selectors.extend(iter_batch_stable_paths(commands))
        else:
            selectors.extend(
                value
                for value in arguments[2:]
                if value.strip().startswith("/")
            )
        if not selectors:
            raise OfficeCLIGatewayError(
                "Broad OfficeCLI access is not allowed in this scoped run."
            )
        forbidden = next(
            (
                selector
                for selector in selectors
                if not self._stable_path_is_allowed(selector)
            ),
            None,
        )
        if forbidden is not None:
            raise OfficeCLIGatewayError(
                "OfficeCLI access is outside this run's authorized stable paths."
            )

    def prepare(self, command: str) -> list[str]:
        arguments = split_command(command)
        if not arguments:
            raise OfficeCLIGatewayError("The OfficeCLI command is empty.")
        verb = arguments[0].casefold()
        option_names = {
            normalized_option_name(argument) for argument in arguments[1:]
        }
        forbidden = sorted(option_names & FORBIDDEN_ARGUMENTS)
        if forbidden:
            raise OfficeCLIGatewayError(
                f"OfficeCLI option {forbidden[0]} is not allowed in Ogent."
            )

        if verb in {"--version", "version"}:
            if len(arguments) != 1:
                raise OfficeCLIGatewayError(
                    "The OfficeCLI version command takes no arguments."
                )
            return [str(self.executable), "--version"]
        if verb in {"help", "--help"}:
            return [str(self.executable), *arguments]
        if verb == "load_skill":
            if (
                len(arguments) != 2
                or not SAFE_SKILL_NAME.fullmatch(arguments[1])
            ):
                raise OfficeCLIGatewayError(
                    "The OfficeCLI skill name is invalid."
                )
            return [str(self.executable), *arguments]
        if verb not in SAFE_DOCUMENT_VERBS:
            raise OfficeCLIGatewayError(
                f"OfficeCLI command {verb or '(empty)'} is not allowed in Ogent."
            )
        if not self.allow_mutations and verb not in READ_ONLY_DOCUMENT_VERBS:
            raise OfficeCLIGatewayError(
                f"OfficeCLI command {verb} is not allowed in a read-only run."
            )
        if len(arguments) < 2:
            raise OfficeCLIGatewayError(
                "The OfficeCLI command has no active document argument."
            )
        active_document = self._resolve_document_argument(arguments[1])
        arguments[1] = str(active_document)
        if verb == "view":
            if (
                len(arguments) < 3
                or arguments[2].casefold() not in SAFE_VIEW_MODES
            ):
                raise OfficeCLIGatewayError(
                    "Only text, structure, forms, and issues view modes are "
                    "allowed through Ogent."
                )
        self._validate_argument_paths(arguments[2:])
        self._validate_scope(verb, arguments)
        return [str(self.executable), *arguments]

    @staticmethod
    def _hash_file(path: Path | None) -> str | None:
        if path is None or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _audit_argument_shape(arguments: list[str]) -> dict[str, Any]:
        verb = arguments[1].casefold() if len(arguments) > 1 else ""
        selectors = [
            value
            for value in arguments[3:]
            if value.startswith("/") and len(value) <= 2_048
        ]
        options = sorted(
            {
                normalized_option_name(value)
                for value in arguments[2:]
                if value.startswith("-")
            }
        )
        batch_count = None
        if "--commands" in arguments:
            index = arguments.index("--commands")
            if index + 1 < len(arguments):
                with contextlib.suppress(ValueError, TypeError):
                    commands = json.loads(arguments[index + 1])
                    if isinstance(commands, list):
                        batch_count = len(commands)
                        selectors.extend(iter_batch_stable_paths(commands))
        return {
            "verb": verb,
            "selectors": list(dict.fromkeys(selectors))[:500],
            "options": options[:100],
            "batch_operation_count": batch_count,
        }

    @staticmethod
    def _mutation_category(verb: str) -> str:
        if verb == "validate":
            return "validation"
        if verb == "refresh":
            return "refresh"
        if verb in READ_ONLY_DOCUMENT_VERBS or verb in {
            "--version",
            "version",
            "help",
            "--help",
            "load_skill",
        }:
            return "read"
        return "mutation"

    def _append_audit(
        self,
        *,
        audit_id: str,
        arguments: list[str],
        started_at: str,
        ended_at: str,
        exit_code: int | None,
        output: str,
        pre_package_sha256: str | None,
        post_package_sha256: str | None,
        error: str | None = None,
    ) -> None:
        if self.audit_log is None:
            return
        verb = arguments[1].casefold() if len(arguments) > 1 else "unknown"
        encoded_output = output.encode("utf-8")
        payload = {
            "id": audit_id,
            "run_id": self.run_id,
            "operation": verb,
            "skill_name": self.skill_name,
            "skill_sha256": self.skill_sha256,
            "document_revision": self.document_revision,
            "package_sha256": pre_package_sha256,
            "post_package_sha256": post_package_sha256,
            "started_at": started_at,
            "ended_at": ended_at,
            "exit_status": exit_code,
            "mutation_category": self._mutation_category(verb),
            "arguments": self._audit_argument_shape(arguments),
            "result": {
                "success": exit_code == 0,
                "error": error,
                "package_changed": (
                    pre_package_sha256 is not None
                    and post_package_sha256 is not None
                    and pre_package_sha256 != post_package_sha256
                ),
            },
            "output_sha256": hashlib.sha256(encoded_output).hexdigest(),
            "output_bytes": len(encoded_output),
        }
        line = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.audit_lock:
            with self.audit_log.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"{line}\n")
                stream.flush()
                os.fsync(stream.fileno())

    @staticmethod
    def _command_string(arguments: list[str]) -> str:
        return (
            subprocess.list2cmdline(arguments)
            if os.name == "nt"
            else shlex.join(arguments)
        )

    def execute_typed(
        self,
        operation: str,
        parameters: dict[str, Any],
    ) -> GatewayResult:
        name = str(operation)
        if name == "load_document_skill":
            skill = str(parameters.get("skill") or "")
            return self.execute(self._command_string(["load_skill", skill]))
        if self.document is None:
            raise OfficeCLIGatewayError(
                "This typed operation requires an active Office document."
            )
        document = str(self.document)
        if name == "inspect_document":
            mode = str(parameters.get("mode") or "stats")
            return self.execute(
                self._command_string(["view", document, mode, "--json"])
            )
        if name == "read_nodes":
            paths = parameters.get("paths")
            if not isinstance(paths, list) or not paths:
                raise OfficeCLIGatewayError("read_nodes requires stable paths.")
            depth = max(0, min(8, int(parameters.get("depth") or 2)))
            if len(paths) == 1:
                return self.execute(
                    self._command_string(
                        [
                            "get",
                            document,
                            str(paths[0]),
                            "--depth",
                            str(depth),
                            "--json",
                        ]
                    )
                )
            results = [
                self.execute(
                    self._command_string(
                        [
                            "get",
                            document,
                            str(path),
                            "--depth",
                            str(depth),
                            "--json",
                        ]
                    )
                )
                for path in paths
            ]
            return GatewayResult(
                exit_code=max(result.exit_code for result in results),
                text="\n".join(result.text for result in results),
            )
        if name == "query_nodes":
            selector = str(parameters.get("selector") or "")
            return self.execute(
                self._command_string(
                    ["query", document, selector, "--json"]
                )
            )
        if name == "apply_atomic_batch":
            commands = parameters.get("commands")
            if not isinstance(commands, list) or not commands:
                raise OfficeCLIGatewayError(
                    "apply_atomic_batch requires command objects."
                )
            return self.execute(
                self._command_string(
                    [
                        "batch",
                        document,
                        "--commands",
                        json.dumps(
                            commands,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        "--json",
                    ]
                )
            )
        if name == "validate_document":
            return self.execute(
                self._command_string(["validate", document, "--json"])
            )
        if name == "refresh_fields":
            return self.execute(
                self._command_string(["refresh", document, "--json"])
            )
        raise OfficeCLIGatewayError(f"Unknown typed operation: {name}.")

    def execute(self, command: str) -> GatewayResult:
        arguments = self.prepare(command)
        audit_id = uuid.uuid4().hex
        started_at = dt.datetime.now(dt.timezone.utc).isoformat()
        pre_package_sha256 = self._hash_file(self.document)
        environment = os.environ.copy()
        environment["OFFICECLI_NO_AUTO_RESIDENT"] = "1"
        environment["OFFICECLI_RESIDENT_FLUSH"] = "each"
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            result = self.runner(
                arguments,
                cwd=str(self.document.parent) if self.document else None,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                shell=False,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            ended_at = dt.datetime.now(dt.timezone.utc).isoformat()
            self._append_audit(
                audit_id=audit_id,
                arguments=arguments,
                started_at=started_at,
                ended_at=ended_at,
                exit_code=None,
                output="",
                pre_package_sha256=pre_package_sha256,
                post_package_sha256=self._hash_file(self.document),
                error="timeout",
            )
            raise OfficeCLIGatewayError(
                "OfficeCLI exceeded the Ogent command timeout."
            ) from exc
        output = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part and part.strip()
        )
        if not output:
            output = (
                "OfficeCLI completed without output."
                if result.returncode == 0
                else f"OfficeCLI exited with code {result.returncode}."
            )
        self._append_audit(
            audit_id=audit_id,
            arguments=arguments,
            started_at=started_at,
            ended_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            exit_code=result.returncode,
            output=output,
            pre_package_sha256=pre_package_sha256,
            post_package_sha256=self._hash_file(self.document),
        )
        return GatewayResult(
            exit_code=result.returncode,
            text=output,
            audit_id=audit_id if self.audit_log is not None else None,
        )


class OfficeCLIMCPServer:
    """Minimal MCP stdio server exposing one path-gated OfficeCLI tool."""

    def __init__(self, gate: OfficeCLIGate) -> None:
        self.gate = gate

    @staticmethod
    def _success(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(
        request_id: Any,
        code: int,
        message: str,
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = str(request.get("method") or "")
        if request_id is None and method.startswith("notifications/"):
            return None
        if method == "initialize":
            parameters = request.get("params")
            requested_version = (
                parameters.get("protocolVersion")
                if isinstance(parameters, dict)
                else None
            )
            return self._success(
                request_id,
                {
                    "protocolVersion": requested_version or "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "ogent-officecli",
                        "version": APP_VERSION,
                    },
                },
            )
        if method == "ping":
            return self._success(request_id, {})
        if method in {"resources/list", "prompts/list"}:
            key = "resources" if method.startswith("resources") else "prompts"
            return self._success(request_id, {key: []})
        if method == "tools/list":
            return self._success(
                request_id,
                {"tools": tool_definitions(
                    allow_mutations=self.gate.allow_mutations
                )},
            )
        if method == "tools/call":
            parameters = request.get("params")
            if not isinstance(parameters, dict):
                return self._error(request_id, -32602, "Invalid tool parameters.")
            tool_name = str(parameters.get("name") or "")
            known_tools = {
                item["name"]
                for item in tool_definitions(
                    allow_mutations=self.gate.allow_mutations
                )
            }
            if tool_name not in known_tools:
                return self._error(request_id, -32602, "Unknown tool.")
            arguments = parameters.get("arguments")
            if not isinstance(arguments, dict):
                return self._success(
                    request_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": "Tool arguments must be a JSON object.",
                            }
                        ],
                        "isError": True,
                    },
                )
            try:
                if tool_name == "officecli":
                    command = arguments.get("command")
                    if not isinstance(command, str):
                        raise OfficeCLIGatewayError(
                            "The officecli command must be one string."
                        )
                    result = self.gate.execute(command)
                else:
                    result = self.gate.execute_typed(
                        tool_name,
                        arguments,
                    )
                return self._success(
                    request_id,
                    {
                        "content": [{"type": "text", "text": result.text}],
                        "isError": result.exit_code != 0,
                    },
                )
            except OfficeCLIGatewayError as exc:
                return self._success(
                    request_id,
                    {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                )
        return self._error(request_id, -32601, "Method not found.")

    def run(self) -> int:
        for raw_line in sys.stdin:
            if not raw_line.strip():
                continue
            try:
                request = json.loads(raw_line)
                if not isinstance(request, dict):
                    raise ValueError("request is not an object")
                response = self.handle(request)
            except (TypeError, ValueError) as exc:
                response = self._error(None, -32700, f"Parse error: {exc}")
            if response is not None:
                sys.stdout.write(
                    json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                sys.stdout.flush()
        return 0


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    return parse_gateway_arguments(
        argv,
        default_timeout_seconds=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )


def main(argv: list[str] | None = None) -> int:
    return run_stdio_gateway(
        argv,
        default_timeout_seconds=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        gate_factory=OfficeCLIGate,
        server_factory=OfficeCLIMCPServer,
        gateway_error=OfficeCLIGatewayError,
    )


if __name__ == "__main__":
    raise SystemExit(main())

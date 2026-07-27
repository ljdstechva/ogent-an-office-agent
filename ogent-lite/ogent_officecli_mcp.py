#!/usr/bin/env python3
"""Least-privilege OfficeCLI MCP gateway for one Ogent document."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable


APP_VERSION = "0.10.0"
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
SAFE_VIEW_MODES = {
    "annotated",
    "forms",
    "issues",
    "outline",
    "stats",
    "text",
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
        return [str(self.executable), *arguments]

    def execute(self, command: str) -> GatewayResult:
        arguments = self.prepare(command)
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
        return GatewayResult(exit_code=result.returncode, text=output)


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
                {
                    "tools": [
                        {
                            "name": "officecli",
                            "title": "Restricted OfficeCLI",
                            "description": (
                                "Run one OfficeCLI command against only the "
                                "active Ogent document."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "command": {
                                        "type": "string",
                                        "description": (
                                            "OfficeCLI arguments without the "
                                            "leading officecli executable."
                                        ),
                                    }
                                },
                                "required": ["command"],
                                "additionalProperties": False,
                            },
                            "annotations": {
                                "readOnlyHint": False,
                                "destructiveHint": False,
                                "idempotentHint": False,
                                "openWorldHint": False,
                            },
                        }
                    ]
                },
            )
        if method == "tools/call":
            parameters = request.get("params")
            if not isinstance(parameters, dict):
                return self._error(request_id, -32602, "Invalid tool parameters.")
            if parameters.get("name") != "officecli":
                return self._error(request_id, -32602, "Unknown tool.")
            arguments = parameters.get("arguments")
            command = (
                arguments.get("command")
                if isinstance(arguments, dict)
                else None
            )
            if not isinstance(command, str):
                return self._success(
                    request_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "The officecli command parameter must be "
                                    "one command string."
                                ),
                            }
                        ],
                        "isError": True,
                    },
                )
            try:
                result = self.gate.execute(command)
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
    parser = argparse.ArgumentParser(
        description="Restricted OfficeCLI MCP gateway for Ogent",
    )
    parser.add_argument("--document", type=Path)
    parser.add_argument(
        "--read-root",
        action="append",
        default=[],
        type=Path,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(AttributeError, OSError):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with contextlib.suppress(AttributeError, OSError):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    arguments = parse_arguments(argv)
    try:
        gate = OfficeCLIGate(
            arguments.document,
            read_roots=arguments.read_root,
            timeout_seconds=arguments.timeout_seconds,
        )
    except (OSError, OfficeCLIGatewayError) as exc:
        with contextlib.suppress(OSError):
            sys.stderr.write(f"Ogent OfficeCLI gateway failed: {exc}\n")
            sys.stderr.flush()
        return 2
    return OfficeCLIMCPServer(gate).run()


if __name__ == "__main__":
    raise SystemExit(main())

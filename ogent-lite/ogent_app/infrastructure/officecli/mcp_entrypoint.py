"""Command-line parsing and stdio startup for the restricted OfficeCLI MCP."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Any, Callable

from ogent_app.domain.run import ScopeMode


def parse_arguments(
    argv: list[str] | None,
    *,
    default_timeout_seconds: int,
) -> argparse.Namespace:
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
        default=default_timeout_seconds,
    )
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help="Explicitly permit OfficeCLI document mutation operations.",
    )
    parser.add_argument(
        "--scope-mode",
        choices=[scope.value for scope in ScopeMode],
        default=ScopeMode.ATTACHMENTS_ONLY.value,
    )
    parser.add_argument(
        "--allowed-path",
        action="append",
        default=[],
        help="Stable OfficeCLI path authorized for this run.",
    )
    parser.add_argument("--audit-log", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--document-revision", type=int)
    parser.add_argument("--skill-name")
    parser.add_argument("--skill-sha256")
    parser.add_argument("--initial-package-sha256")
    return parser.parse_args(argv)


def run_stdio_gateway(
    argv: list[str] | None,
    *,
    default_timeout_seconds: int,
    gate_factory: Callable[..., Any],
    server_factory: Callable[[Any], Any],
    gateway_error: type[BaseException],
) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(AttributeError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    arguments = parse_arguments(
        argv,
        default_timeout_seconds=default_timeout_seconds,
    )
    try:
        gate = gate_factory(
            arguments.document,
            read_roots=arguments.read_root,
            timeout_seconds=arguments.timeout_seconds,
            allow_mutations=arguments.allow_mutations,
            scope_mode=arguments.scope_mode,
            allowed_document_paths=arguments.allowed_path,
            audit_log=arguments.audit_log,
            run_id=arguments.run_id,
            document_revision=arguments.document_revision,
            skill_name=arguments.skill_name,
            skill_sha256=arguments.skill_sha256,
            initial_package_sha256=arguments.initial_package_sha256,
        )
    except (OSError, gateway_error) as exc:
        with contextlib.suppress(OSError):
            sys.stderr.write(f"Ogent OfficeCLI gateway failed: {exc}\n")
            sys.stderr.flush()
        return 2
    return int(server_factory(gate).run())

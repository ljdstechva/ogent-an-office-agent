"""Neutral contracts shared by provider catalog and runtime adapters."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Protocol

from ogent_app.domain.run import ScopeMode


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
    office_document: Path | None = None
    allow_document_mutation: bool = False
    scope_mode: ScopeMode = ScopeMode.ATTACHMENTS_ONLY
    allowed_document_paths: tuple[str, ...] = ()
    run_id: str | None = None
    audit_log_path: Path | None = None
    document_revision: int | None = None
    capability_skill_name: str | None = None
    capability_skill_sha256: str | None = None
    initial_package_sha256: str | None = None
    image_paths: tuple[Path, ...] = ()
    sandbox: str = "read-only"
    writable_directories: tuple[Path, ...] = ()
    extra_directories: tuple[Path, ...] = ()
    event_observer: Callable[[str, dict[str, Any]], None] | None = dataclasses.field(
        default=None,
        compare=False,
        repr=False,
    )
    phase_observer: Callable[[str, dict[str, Any]], None] | None = dataclasses.field(
        default=None,
        compare=False,
        repr=False,
    )
    first_event_timeout: float = 90.0
    inactivity_timeout: float = 300.0
    total_timeout: float = 1800.0


@dataclasses.dataclass(frozen=True)
class ProviderRunResult:
    exit_code: int
    session_id: str | None
    final_text: str | None
    stderr_tail: tuple[str, ...]
    resumable: bool
    usage: dict[str, Any]
    error_message: str | None = None


class AgentProviderPort(Protocol):
    """Structural provider interface without catalog-to-adapter imports."""

    provider_id: str
    label: str
    supports_model_effort_verification: bool

    def resolve_cli(self) -> CLIResolution | None: ...

    def inspect_environment(self) -> Any: ...

    def discover_catalog(self, environment: Any) -> Any: ...

    def verify_model_efforts(
        self,
        environment: Any,
        model_id: str,
    ) -> Any: ...

    def run_agent(
        self,
        request: ProviderRunRequest,
        *,
        on_process: Callable[[Any], None],
        on_activity: Callable[[str, str], None],
        should_stop: Callable[[], bool],
    ) -> ProviderRunResult: ...

    def cancel_discovery(self) -> None: ...

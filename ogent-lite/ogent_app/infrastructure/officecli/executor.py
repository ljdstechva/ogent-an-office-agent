"""No-shell OfficeCLI process execution with bounded output."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from ogent_app.infrastructure.fault_injection import FaultInjector, FaultPoint

MAX_OUTPUT_CHARACTERS = 16 * 1024 * 1024
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class OfficeCliExecutionError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class OfficeCliExecution:
    arguments: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    ended_at: str


class OfficeCliExecutor:
    def __init__(
        self,
        *,
        executable: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 120.0,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        found = executable or (
            Path(value) if (value := shutil.which("officecli")) else None
        )
        if found is None:
            raise OfficeCliExecutionError("OfficeCLI is not installed.")
        self.executable = Path(found).resolve(strict=True)
        self.runner = runner
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.fault_injector = fault_injector
        self._version: str | None = None
        self._version_lock = threading.RLock()

    @staticmethod
    def _timestamp() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat()

    def execute(
        self,
        arguments: list[str] | tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> OfficeCliExecution:
        command = [str(self.executable), *(str(item) for item in arguments)]
        if self.fault_injector is not None:
            if self.fault_injector.consume(FaultPoint.OFFICECLI_CRASH):
                raise OfficeCliExecutionError(
                    "OfficeCLI could not complete the requested operation."
                )
            if _is_mutating(arguments) and self.fault_injector.consume(
                FaultPoint.WORD_LOCK
            ):
                raise OfficeCliExecutionError(
                    "The Office document is locked by another process."
                )
        environment = os.environ.copy()
        environment["OFFICECLI_NO_AUTO_RESIDENT"] = "1"
        environment["OFFICECLI_RESIDENT_FLUSH"] = "each"
        environment["PYTHONIOENCODING"] = "utf-8"
        started = self._timestamp()
        try:
            result = self.runner(
                command,
                cwd=str(cwd) if cwd is not None else None,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=(
                    self.timeout_seconds
                    if timeout_seconds is None
                    else max(1.0, float(timeout_seconds))
                ),
                shell=False,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OfficeCliExecutionError(
                "OfficeCLI could not complete the requested operation."
            ) from exc
        stdout = str(result.stdout or "")
        stderr = str(result.stderr or "")
        if len(stdout) + len(stderr) > MAX_OUTPUT_CHARACTERS:
            raise OfficeCliExecutionError(
                "OfficeCLI returned more output than Ogent can safely inspect."
            )
        return OfficeCliExecution(
            arguments=tuple(command),
            exit_code=int(result.returncode),
            stdout=stdout,
            stderr=stderr,
            started_at=started,
            ended_at=self._timestamp(),
        )

    def version(self) -> str:
        with self._version_lock:
            if self._version is not None:
                return self._version
            result = self.execute(["--version"], timeout_seconds=20)
            value = next(
                (line.strip() for line in result.stdout.splitlines() if line.strip()),
                "",
            )
            if result.exit_code != 0 or not value:
                raise OfficeCliExecutionError("OfficeCLI version verification failed.")
            self._version = value
            return value


def _is_mutating(arguments: list[str] | tuple[str, ...]) -> bool:
    if not arguments:
        return False
    read_only = {
        "--version",
        "dump",
        "get",
        "query",
        "validate",
        "view",
    }
    return str(arguments[0]).casefold() not in read_only

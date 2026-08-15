"""Cross-platform termination for process trees launched by Ogent."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from ogent_app.ports.process_supervisor import OwnedProcess


def _unsupported_process_group(*_args: Any) -> Any:
    raise OSError("Process groups are unavailable on this platform.")


class SubprocessOwnedProcessSupervisor:
    """Terminate a known owned process and its descendants without a shell."""

    def __init__(
        self,
        *,
        platform_name: str = os.name,
        runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
        get_process_group: Callable[[int], int] | None = None,
        kill_process_group: Callable[[int, int], None] | None = None,
        create_no_window: int | None = None,
    ) -> None:
        self.platform_name = platform_name
        self.runner = runner
        self.get_process_group = get_process_group or getattr(
            os,
            "getpgid",
            _unsupported_process_group,
        )
        self.kill_process_group = kill_process_group or getattr(
            os,
            "killpg",
            _unsupported_process_group,
        )
        self.create_no_window = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if create_no_window is None
            else int(create_no_window)
        )

    def terminate(
        self,
        process: OwnedProcess | None,
        *,
        grace_seconds: float = 5.0,
    ) -> None:
        if process is None or process.poll() is not None:
            return
        if self.platform_name == "nt":
            command: Sequence[str] = (
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
            )
            self.runner(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=self.create_no_window,
                check=False,
            )
        else:
            with contextlib.suppress(ProcessLookupError):
                self.kill_process_group(
                    self.get_process_group(process.pid),
                    signal.SIGTERM,
                )
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=max(0.0, float(grace_seconds)))
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()


DEFAULT_OWNED_PROCESS_SUPERVISOR = SubprocessOwnedProcessSupervisor()

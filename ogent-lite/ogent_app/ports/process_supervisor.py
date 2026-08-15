"""Port for terminating only child processes owned by Ogent."""

from __future__ import annotations

from typing import Protocol


class OwnedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class OwnedProcessSupervisor(Protocol):
    def terminate(
        self,
        process: OwnedProcess | None,
        *,
        grace_seconds: float = 5.0,
    ) -> None: ...

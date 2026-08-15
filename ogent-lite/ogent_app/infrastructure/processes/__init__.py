"""Owned child-process lifecycle adapters."""

from .owned_process_supervisor import (
    DEFAULT_OWNED_PROCESS_SUPERVISOR,
    SubprocessOwnedProcessSupervisor,
)

__all__ = [
    "DEFAULT_OWNED_PROCESS_SUPERVISOR",
    "SubprocessOwnedProcessSupervisor",
]

"""Deterministic OfficeCLI execution adapters."""

from .executor import (
    OfficeCliExecution,
    OfficeCliExecutionError,
    OfficeCliExecutor,
)
from .gateway import TypedOfficeCliGateway
from .skill_registry import SkillRegistry

__all__ = [
    "OfficeCliExecution",
    "OfficeCliExecutionError",
    "OfficeCliExecutor",
    "SkillRegistry",
    "TypedOfficeCliGateway",
]

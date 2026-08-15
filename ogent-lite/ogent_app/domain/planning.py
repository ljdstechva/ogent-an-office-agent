"""Deterministic, serializable run plans and resumable step state."""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Iterable, Mapping
from typing import Any

from .run import RunMode, ScopeMode


class RunComplexity(str, enum.Enum):
    FAST_PATH = "fast_path"
    STRUCTURED = "structured"


class RunStepState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStepState.COMPLETED,
            RunStepState.FAILED,
            RunStepState.CANCELLED,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class RunStep:
    step_id: str
    sequence: int
    description: str
    target_node_ids: tuple[str, ...] = ()
    mutates: bool = False
    tool: str | None = None
    proof: str = ""
    dependencies: tuple[str, ...] = ()
    estimated_work_units: int = 1

    def __post_init__(self) -> None:
        identifier = str(self.step_id).strip()
        description = str(self.description).strip()
        proof = str(self.proof).strip()
        if not identifier or len(identifier) > 128:
            raise ValueError("A run step requires a bounded identifier.")
        if int(self.sequence) < 1:
            raise ValueError("A run step sequence must be positive.")
        if not description or len(description) > 2_000:
            raise ValueError("A run step requires a bounded description.")
        if not proof or len(proof) > 2_000:
            raise ValueError("A run step requires a success proof.")
        if int(self.estimated_work_units) < 1:
            raise ValueError("Run step work units must be positive.")
        object.__setattr__(self, "step_id", identifier)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "proof", proof)
        object.__setattr__(
            self,
            "target_node_ids",
            _unique_strings(self.target_node_ids, maximum=10_000),
        )
        object.__setattr__(
            self,
            "dependencies",
            _unique_strings(self.dependencies, maximum=1_000),
        )
        if self.tool is not None:
            tool = str(self.tool).strip()
            object.__setattr__(self, "tool", tool or None)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.step_id,
            "sequence": self.sequence,
            "description": self.description,
            "target_node_ids": list(self.target_node_ids),
            "mutates": self.mutates,
            "tool": self.tool,
            "proof": self.proof,
            "dependencies": list(self.dependencies),
            "estimated_work_units": self.estimated_work_units,
        }

    @classmethod
    def from_public(cls, value: Mapping[str, Any]) -> RunStep:
        return cls(
            step_id=str(value.get("id") or value.get("step_id") or ""),
            sequence=int(value.get("sequence") or 0),
            description=str(value.get("description") or ""),
            target_node_ids=tuple(value.get("target_node_ids") or ()),
            mutates=bool(value.get("mutates")),
            tool=(str(value["tool"]) if value.get("tool") is not None else None),
            proof=str(value.get("proof") or ""),
            dependencies=tuple(value.get("dependencies") or ()),
            estimated_work_units=int(value.get("estimated_work_units") or 1),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class RunPlan:
    goal: str
    mode: RunMode
    scope: ScopeMode
    steps: tuple[RunStep, ...]
    dependencies: dict[str, tuple[str, ...]]
    target_node_ids: tuple[str, ...]
    expected_mutations: tuple[str, ...]
    verification_assertions: tuple[str, ...]
    coverage_requirement: dict[str, Any]
    estimated_work_units: int
    complexity: RunComplexity
    schema_version: int = 1

    def __post_init__(self) -> None:
        goal = str(self.goal).strip()
        if not goal or len(goal) > 1_000_000:
            raise ValueError("A run plan requires a bounded goal.")
        if not self.steps:
            raise ValueError("A run plan requires at least one step.")
        ordered = tuple(sorted(self.steps, key=lambda item: item.sequence))
        identifiers = [step.step_id for step in ordered]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Run step identifiers must be unique.")
        if [step.sequence for step in ordered] != list(range(1, len(ordered) + 1)):
            raise ValueError("Run steps must use contiguous one-based sequences.")
        positions = {step.step_id: index for index, step in enumerate(ordered)}
        normalized_dependencies = {
            step.step_id: tuple(step.dependencies) for step in ordered
        }
        supplied_dependencies = {
            str(key): _unique_strings(value, maximum=1_000)
            for key, value in dict(self.dependencies).items()
        }
        for step_id, dependencies in supplied_dependencies.items():
            if step_id not in positions:
                raise ValueError(f"Dependency target {step_id!r} is not a run step.")
            if dependencies != normalized_dependencies[step_id]:
                raise ValueError(f"Dependency declarations disagree for {step_id!r}.")
        for step in ordered:
            for dependency in step.dependencies:
                if dependency not in positions:
                    raise ValueError(
                        f"Run step dependency {dependency!r} does not exist."
                    )
                if positions[dependency] >= positions[step.step_id]:
                    raise ValueError(
                        "Run step dependencies must refer to an earlier step."
                    )
        work_units = sum(step.estimated_work_units for step in ordered)
        if int(self.estimated_work_units) != work_units:
            raise ValueError("Run plan work units must equal the sum of its steps.")
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "steps", ordered)
        object.__setattr__(self, "dependencies", normalized_dependencies)
        object.__setattr__(
            self,
            "target_node_ids",
            _unique_strings(self.target_node_ids, maximum=10_000),
        )
        object.__setattr__(
            self,
            "expected_mutations",
            _unique_strings(self.expected_mutations, maximum=10_000),
        )
        object.__setattr__(
            self,
            "verification_assertions",
            _unique_strings(
                self.verification_assertions,
                maximum=10_000,
            ),
        )
        object.__setattr__(
            self,
            "coverage_requirement",
            dict(self.coverage_requirement),
        )

    @property
    def structured(self) -> bool:
        return self.complexity is RunComplexity.STRUCTURED

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "mode": self.mode.value,
            "scope": self.scope.value,
            "complexity": self.complexity.value,
            "steps": [step.public() for step in self.steps],
            "dependencies": {
                step_id: list(dependencies)
                for step_id, dependencies in self.dependencies.items()
            },
            "target_node_ids": list(self.target_node_ids),
            "expected_mutations": list(self.expected_mutations),
            "verification_assertions": list(self.verification_assertions),
            "coverage_requirement": dict(self.coverage_requirement),
            "estimated_work_units": self.estimated_work_units,
        }

    @classmethod
    def from_public(cls, value: Mapping[str, Any]) -> RunPlan:
        steps = tuple(
            RunStep.from_public(item)
            for item in value.get("steps", ())
            if isinstance(item, Mapping)
        )
        raw_dependencies = value.get("dependencies")
        dependencies = (
            {str(key): tuple(items) for key, items in raw_dependencies.items()}
            if isinstance(raw_dependencies, Mapping)
            else {step.step_id: step.dependencies for step in steps}
        )
        return cls(
            goal=str(value.get("goal") or ""),
            mode=RunMode(str(value.get("mode"))),
            scope=ScopeMode(str(value.get("scope"))),
            steps=steps,
            dependencies=dependencies,
            target_node_ids=tuple(value.get("target_node_ids") or ()),
            expected_mutations=tuple(value.get("expected_mutations") or ()),
            verification_assertions=tuple(value.get("verification_assertions") or ()),
            coverage_requirement=dict(value.get("coverage_requirement") or {}),
            estimated_work_units=int(value.get("estimated_work_units") or 0),
            complexity=RunComplexity(
                str(value.get("complexity") or RunComplexity.FAST_PATH.value)
            ),
            schema_version=int(value.get("schema_version") or 1),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class RunStepRecord:
    step: RunStep
    state: RunStepState
    checkpoint: dict[str, Any] = dataclasses.field(default_factory=dict)
    verification: dict[str, Any] = dataclasses.field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    error_code: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            **self.step.public(),
            "state": self.state.value,
            "checkpoint": dict(self.checkpoint),
            "verification": dict(self.verification),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error_code": self.error_code,
        }


def _unique_strings(
    values: Iterable[object],
    *,
    maximum: int,
) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        if len(text) > 4_000:
            raise ValueError("A run plan string is too large.")
        result.append(text)
        seen.add(text)
        if len(result) > maximum:
            raise ValueError("A run plan collection is too large.")
    return tuple(result)

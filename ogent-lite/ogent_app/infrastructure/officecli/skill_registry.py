"""Version-keyed, integrity-checked OfficeCLI skill cache."""

from __future__ import annotations

import threading

from ogent_app.domain.capability import SkillPolicy
from ogent_app.infrastructure.sqlite import SkillPolicyRepository

from .executor import OfficeCliExecutionError, OfficeCliExecutor


class SkillRegistry:
    def __init__(
        self,
        executor: OfficeCliExecutor,
        repository: SkillPolicyRepository,
    ) -> None:
        self.executor = executor
        self.repository = repository
        self.lock = threading.RLock()

    def resolve(self, skill_name: str) -> SkillPolicy:
        name = str(skill_name).strip().casefold()
        if name not in {"word", "excel", "pptx"}:
            raise OfficeCliExecutionError(
                f"Unsupported OfficeCLI document skill: {name or '(empty)'}."
            )
        version = self.executor.version()
        with self.lock:
            cached = self.repository.get(version, name)
            if cached is not None:
                return cached
            result = self.executor.execute(
                ["load_skill", name],
                timeout_seconds=45,
            )
            policy = result.stdout
            if result.exit_code != 0 or not policy.strip():
                raise OfficeCliExecutionError(
                    f"OfficeCLI could not load the {name} skill."
                )
            return self.repository.put(version, name, policy)

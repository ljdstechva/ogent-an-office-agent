"""Feature-gated warm transport evaluation with cold-process fallback."""

from __future__ import annotations

import dataclasses
import os
from typing import Any


WARM_TRANSPORT_ENV = "OGENT_ENABLE_WARM_PROVIDER_TRANSPORT"


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderTransportDecision:
    provider: str
    model: str
    requested_warm: bool
    selected: str
    reason: str
    workspace_isolated: bool

    def public(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class ProviderTransportPolicy:
    """Select warm transport only when capability and isolation are proven."""

    def __init__(self, *, warm_enabled: bool = False) -> None:
        self.warm_enabled = bool(warm_enabled)

    @classmethod
    def from_environment(cls) -> ProviderTransportPolicy:
        value = os.environ.get(WARM_TRANSPORT_ENV, "").strip().casefold()
        return cls(warm_enabled=value in {"1", "true", "yes", "on"})

    def decide(
        self,
        provider: str,
        model: str,
        *,
        transport_available: bool,
        workspace_isolated: bool,
        sessions_resumable: bool,
    ) -> ProviderTransportDecision:
        if not self.warm_enabled:
            selected = "cold_process"
            reason = "feature_flag_disabled"
        elif not transport_available:
            selected = "cold_process"
            reason = "warm_transport_unavailable"
        elif not sessions_resumable:
            selected = "cold_process"
            reason = "provider_resume_not_verified"
        elif not workspace_isolated:
            selected = "cold_process"
            reason = "workspace_isolation_not_verified"
        else:
            selected = "warm_transport"
            reason = "feature_flag_and_safety_gates_passed"
        return ProviderTransportDecision(
            str(provider),
            str(model),
            self.warm_enabled,
            selected,
            reason,
            bool(workspace_isolated),
        )

"""Provider-aware input budgeting and immutable context projections."""

from __future__ import annotations

import dataclasses
from typing import Any

from ogent_app.domain.document_intelligence import IndexStatus
from ogent_app.domain.run import ScopeMode


DEFAULT_INPUT_CONTEXT_TOKENS = 32_768
DEFAULT_OUTPUT_RESERVE_TOKENS = 4_096
DEFAULT_TOOL_RESULT_RESERVE_TOKENS = 4_096
MAX_DOCUMENT_CONTEXT_CHARACTERS = 96_000
MIN_DOCUMENT_CONTEXT_CHARACTERS = 4_000
MAX_INITIAL_WHOLE_DOCUMENT_CONTEXT_CHARACTERS = 16_000
FAST_INPUT_CONTEXT_FLOOR_TOKENS = 32_768


@dataclasses.dataclass(frozen=True, slots=True)
class ProviderContextBudget:
    provider: str
    model: str
    input_context_tokens: int
    output_reserve_tokens: int
    tool_result_reserve_tokens: int
    supported_modalities: tuple[str, ...]
    sessions_resumable: bool
    partial_text_deltas: bool
    source: str
    reliable: bool

    def __post_init__(self) -> None:
        if self.input_context_tokens < 8_192:
            raise ValueError("Provider input context is implausibly small.")
        if self.output_reserve_tokens < 1_024:
            raise ValueError("Provider output reserve is too small.")
        if self.tool_result_reserve_tokens < 0:
            raise ValueError("Provider tool-result reserve cannot be negative.")
        if self.available_input_tokens <= 0:
            raise ValueError("Provider context reserves consume the input budget.")

    def fast_variant(self) -> "ProviderContextBudget":
        """Smaller retrieved-context budget for Fast mode (never below floors)."""
        reduced = max(
            FAST_INPUT_CONTEXT_FLOOR_TOKENS,
            self.output_reserve_tokens + self.tool_result_reserve_tokens + 8_192,
            self.input_context_tokens // 2,
        )
        if reduced >= self.input_context_tokens:
            return self
        return dataclasses.replace(
            self,
            input_context_tokens=reduced,
            source=f"{self.source}+fast",
        )

    @property
    def available_input_tokens(self) -> int:
        return (
            self.input_context_tokens
            - self.output_reserve_tokens
            - self.tool_result_reserve_tokens
        )

    def document_character_budget(
        self,
        *,
        fixed_prompt_characters: int,
    ) -> int:
        approximate_fixed_tokens = max(
            0,
            int(fixed_prompt_characters) // 4,
        )
        remaining_tokens = max(
            1_000,
            self.available_input_tokens - approximate_fixed_tokens,
        )
        return max(
            MIN_DOCUMENT_CONTEXT_CHARACTERS,
            min(
                MAX_DOCUMENT_CONTEXT_CHARACTERS,
                remaining_tokens * 4,
            ),
        )

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_context_tokens": self.input_context_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "tool_result_reserve_tokens": self.tool_result_reserve_tokens,
            "available_input_tokens": self.available_input_tokens,
            "supported_modalities": list(self.supported_modalities),
            "sessions_resumable": self.sessions_resumable,
            "partial_text_deltas": self.partial_text_deltas,
            "source": self.source,
            "reliable": self.reliable,
        }

    @classmethod
    def conservative(
        cls,
        provider: str,
        model: str,
        *,
        partial_text_deltas: bool,
    ) -> ProviderContextBudget:
        return cls(
            str(provider),
            str(model),
            DEFAULT_INPUT_CONTEXT_TOKENS,
            DEFAULT_OUTPUT_RESERVE_TOKENS,
            DEFAULT_TOOL_RESULT_RESERVE_TOKENS,
            ("text",),
            False,
            partial_text_deltas,
            "conservative_config",
            False,
        )

    @classmethod
    def from_model_capability(
        cls,
        provider: str,
        model: str,
        capability: Any | None,
    ) -> ProviderContextBudget:
        if capability is None:
            return cls.conservative(
                provider,
                model,
                partial_text_deltas=str(provider).casefold() in {"codex", "claude"},
            )
        context_limit = getattr(
            capability,
            "input_context_limit",
            None,
        )
        reliable = context_limit is not None
        return cls(
            str(provider),
            str(model),
            int(context_limit or DEFAULT_INPUT_CONTEXT_TOKENS),
            int(
                getattr(
                    capability,
                    "output_reserve",
                    DEFAULT_OUTPUT_RESERVE_TOKENS,
                )
                or DEFAULT_OUTPUT_RESERVE_TOKENS
            ),
            int(
                getattr(
                    capability,
                    "tool_result_limit",
                    DEFAULT_TOOL_RESULT_RESERVE_TOKENS,
                )
                or DEFAULT_TOOL_RESULT_RESERVE_TOKENS
            ),
            tuple(getattr(capability, "input_modalities", ()) or ("text",)),
            bool(getattr(capability, "sessions_resumable", False)),
            bool(
                getattr(
                    capability,
                    "partial_text_deltas",
                    str(provider).casefold() in {"codex", "claude"},
                )
            ),
            (
                str(
                    getattr(
                        capability,
                        "context_limit_source",
                        "provider_catalog",
                    )
                )
                if reliable
                else "conservative_config"
            ),
            reliable,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ContextProjection:
    revision_id: str
    document_id: str
    scope: ScopeMode
    text: str
    included_node_ids: tuple[str, ...]
    included_paths: tuple[str, ...]
    omitted_node_count: int
    character_budget: int
    character_count: int
    index_status: IndexStatus
    coverage: dict[str, Any]
    budget: ProviderContextBudget
    partitions: tuple[tuple[str, ...], ...] = ()

    def public(self) -> dict[str, Any]:
        visible_partitions = self.partitions[:20]
        return {
            "revision_id": self.revision_id,
            "document_id": self.document_id,
            "scope": self.scope.value,
            "included_node_ids": list(self.included_node_ids),
            "included_paths": list(self.included_paths),
            "omitted_node_count": self.omitted_node_count,
            "character_budget": self.character_budget,
            "character_count": self.character_count,
            "index_status": self.index_status.value,
            "coverage": dict(self.coverage),
            "budget": self.budget.public(),
            "partition_count": len(self.partitions),
            "partitions": [list(partition) for partition in visible_partitions],
            "partitions_omitted": max(
                0,
                len(self.partitions) - len(visible_partitions),
            ),
        }

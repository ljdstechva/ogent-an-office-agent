"""Durable transcript projection and bounded provider-context assembly."""

from __future__ import annotations

import collections.abc
import dataclasses
from typing import Any

from ogent_app.domain.workspace import TurnRecord
from ogent_app.infrastructure.sqlite import TurnRepository

from .workspace_actor import ClearConversation, WorkspaceActor


DEFAULT_CONTEXT_BYTES = 96 * 1024
DEFAULT_CONTEXT_TURNS = 40


@dataclasses.dataclass(frozen=True)
class DurableContextSnapshot:
    text: str
    mode: str
    provider: str
    model: str
    effort: str
    sequence_from: int
    sequence_to: int
    included_sequences: tuple[int, ...]


class DurableConversation:
    def __init__(
        self,
        workspace_id: str,
        turns: TurnRepository,
        actor: WorkspaceActor,
    ) -> None:
        self.workspace_id = workspace_id
        self.turns = turns
        self.actor = actor

    def public_turn(self, turn: TurnRecord) -> dict[str, Any]:
        metadata = dict(turn.metadata)
        return {
            "turn_id": turn.turn_id,
            "sequence": turn.sequence,
            "role": turn.role,
            "text": self.turns.raw_content(turn.turn_id),
            "time": turn.created_at,
            "provider": turn.provider,
            "model": turn.model,
            "effort": turn.effort,
            "attachments": list(metadata.get("attachments") or []),
            "preview_selections": list(metadata.get("preview_selections") or []),
            "run_outcome": turn.run_outcome,
            "verification": dict(metadata.get("verification") or {}),
        }

    def page_public(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        page = self.turns.page(
            self.workspace_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return {
            "items": [self.public_turn(turn) for turn in page.items],
            "next_sequence": page.next_sequence,
            "total": self.turns.count(self.workspace_id),
        }

    def tail_public(self, *, limit: int = 50) -> dict[str, Any]:
        page = self.turns.tail(self.workspace_id, limit=limit)
        return {
            "items": [self.public_turn(turn) for turn in page.items],
            "previous_sequence": page.next_sequence,
            "total": self.turns.count(self.workspace_id),
        }

    def build_provider_context(
        self,
        *,
        current_user_sequence: int | None,
        maximum_bytes: int = DEFAULT_CONTEXT_BYTES,
        maximum_turns: int = DEFAULT_CONTEXT_TURNS,
    ) -> str:
        page = self.turns.tail(
            self.workspace_id,
            limit=max(1, min(200, maximum_turns + 1)),
        )
        records = [
            turn
            for turn in page.items
            if (current_user_sequence is None or turn.sequence < current_user_sequence)
        ][-maximum_turns:]
        selected: list[str] = []
        used = 0
        omitted: list[TurnRecord] = []
        for turn in reversed(records):
            raw = self.turns.raw_content(turn.turn_id)
            metadata_lines: list[str] = []
            if turn.metadata.get("attachments"):
                metadata_lines.append(f"attachments={turn.metadata['attachments']!r}")
            if turn.metadata.get("preview_selections"):
                metadata_lines.append(
                    f"preview_selections={turn.metadata['preview_selections']!r}"
                )
            metadata_text = "\n" + "\n".join(metadata_lines) if metadata_lines else ""
            block = (
                f"[Turn {turn.sequence} | {turn.role} | {turn.created_at}]\n"
                f"{raw}{metadata_text}\n"
            )
            size = len(block.encode("utf-8"))
            if size > maximum_bytes or used + size > maximum_bytes:
                omitted.append(turn)
                continue
            selected.append(block)
            used += size
        selected.reverse()
        disclosure = ""
        if omitted or page.next_sequence is not None:
            omitted_sequences = sorted(turn.sequence for turn in omitted)
            detail = (
                ", ".join(str(item) for item in omitted_sequences)
                if omitted_sequences
                else "older turns"
            )
            disclosure = (
                "[Context projection omitted canonical conversation content "
                f"({detail}) to fit the provider budget. The lossless turns "
                "remain retrievable from Ogent's durable store.]\n\n"
            )
        if not selected:
            return disclosure.rstrip()
        return (
            f"{disclosure}Durable Ogent conversation context:\n\n" + "\n".join(selected)
        ).rstrip()

    def clear(self, *, reason: str = "new_chat") -> int:
        state = self.actor.dispatch(ClearConversation(reason))
        return state.workspace.conversation_generation


class DurableTranscriptView(collections.abc.Sequence[dict[str, Any]]):
    """List-like compatibility view without owning a second transcript."""

    def __init__(self, conversation: DurableConversation) -> None:
        self.conversation = conversation

    def __len__(self) -> int:
        return self.conversation.turns.count(self.conversation.workspace_id)

    def __getitem__(self, index: int | slice) -> Any:
        values = list(self)
        return values[index]

    def __iter__(self):
        cursor = 0
        while True:
            page = self.conversation.page_public(
                after_sequence=cursor,
                limit=200,
            )
            items = page["items"]
            yield from items
            next_sequence = page["next_sequence"]
            if next_sequence is None:
                return
            cursor = int(next_sequence)

    def clear(self) -> None:
        self.conversation.clear()


class DurableMemoryProjection:
    """Compatibility surface backed by SQLite for conversational fields."""

    def __init__(self, legacy_memory: Any, conversation: DurableConversation) -> None:
        self._legacy_memory = legacy_memory
        self._conversation = conversation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._legacy_memory, name)

    @property
    def turns(self) -> list[dict[str, Any]]:
        return list(DurableTranscriptView(self._conversation))

    @property
    def sequence(self) -> int:
        tail = self._conversation.turns.tail(
            self._conversation.workspace_id,
            limit=1,
        )
        return tail.items[-1].sequence if tail.items else 0

    def summary(self) -> dict[str, Any]:
        return {
            **self._legacy_memory.summary(),
            "retained_turns": self._conversation.turns.count(
                self._conversation.workspace_id
            ),
            "durable": True,
        }

    def build_provider_context(
        self,
        current_message: str,
        *,
        provider: str,
        model: str,
        effort: str,
        current_user_sequence: int | None = None,
        **_: Any,
    ) -> DurableContextSnapshot:
        del current_message
        page = self._conversation.turns.tail(
            self._conversation.workspace_id,
            limit=DEFAULT_CONTEXT_TURNS,
        )
        included = tuple(
            turn.sequence
            for turn in page.items
            if (current_user_sequence is None or turn.sequence < current_user_sequence)
        )
        return DurableContextSnapshot(
            text=self._conversation.build_provider_context(
                current_user_sequence=current_user_sequence,
            ),
            mode="durable",
            provider=provider,
            model=model,
            effort=effort,
            sequence_from=included[0] if included else 0,
            sequence_to=included[-1] if included else 0,
            included_sequences=included,
        )

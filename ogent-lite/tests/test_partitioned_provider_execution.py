from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from ogent_app.application.document_context import (
    ContextProjection,
    ProviderContextBudget,
)
from ogent_app.application.partitioned_provider_execution import (
    execute_partitioned_analysis,
)
from ogent_app.application.provider_execution import ProviderExecutionResult
from ogent_app.domain.document_intelligence import IndexStatus
from ogent_app.domain.run import ScopeMode
from ogent_app.infrastructure.sqlite import ContentAddressedBlobStore


class _ContextService:
    def __init__(self, template: ContextProjection) -> None:
        self.template = template
        self.reviewed: set[str] = set()

    def retrieve_partition(
        self,
        *,
        stable_paths: tuple[str, ...],
        partition_index: int,
        partition_count: int,
        **_: Any,
    ) -> ContextProjection:
        text = f"Partition {partition_index}/{partition_count}\n" + "\n".join(
            stable_paths
        )
        return ContextProjection(
            self.template.revision_id,
            self.template.document_id,
            self.template.scope,
            text,
            tuple(f"node-{path}" for path in stable_paths),
            stable_paths,
            0,
            self.template.character_budget,
            len(text),
            self.template.index_status,
            self.template.coverage,
            self.template.budget,
        )

    def mark_partition_reviewed(
        self,
        *,
        stable_paths: tuple[str, ...],
        **_: Any,
    ) -> dict[str, Any]:
        self.reviewed.update(stable_paths)
        return {
            "complete": len(self.reviewed) == 3,
            "structurally_complete": len(self.reviewed) == 3,
            "categories": {
                "headings": {
                    "reviewed": len(self.reviewed),
                    "total": 3,
                }
            },
        }


class _Stream:
    def __init__(self) -> None:
        self.segments: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.sanitize = lambda value: str(value)

    def append_segment(
        self,
        text: str,
        **_: Any,
    ) -> None:
        self.segments.append(text)

    def observe(self, provider: str, event: dict[str, Any]) -> None:
        self.events.append((provider, event))


class PartitionedProviderExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.blobs = ContentAddressedBlobStore(Path(self.temporary.name) / "blobs")
        self.budget = ProviderContextBudget.conservative(
            "codex",
            "fixture",
            partial_text_deltas=True,
        )
        self.projection = ContextProjection(
            "revision-1",
            "document-1",
            ScopeMode.WHOLE_DOCUMENT,
            "Relevant search context",
            (),
            (),
            0,
            16_000,
            23,
            IndexStatus.COMPLETE,
            {"complete": False, "structurally_complete": False},
            self.budget,
            (("/p[1]",), ("/p[2]",), ("/p[3]",)),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_partitions_are_sequential_checkpointed_and_synthesized(
        self,
    ) -> None:
        service = _ContextService(self.projection)
        stream = _Stream()
        prompts: list[str] = []
        checkpoints: list[dict[str, Any]] = []

        def dispatch(
            prompt: str,
            observer: Any,
            images: tuple[Path, ...],
        ) -> ProviderExecutionResult:
            del images
            prompts.append(prompt)
            if observer is not None:
                observer("codex", {"text": "streamed synthesis"})
            final = (
                "Final synthesis"
                if "All 3 structural partitions" in prompt
                else f"Evidence for {len(prompts)}"
            )
            return ProviderExecutionResult(0, final, (), {"selected": "cold"})

        result = execute_partitioned_analysis(
            request="Review the whole document.",
            run_id="run-1",
            plan=object(),
            projection=self.projection,
            context_service=service,
            budget=self.budget,
            fixed_prompt_characters=2_000,
            prompt_factory=lambda message, context: message + "\n" + context,
            dispatch=dispatch,
            assistant_stream=stream,
            checkpoint=checkpoints.append,
            stop_requested=lambda: False,
            blob_store=self.blobs,
        )

        self.assertEqual(len(prompts), 4)
        self.assertEqual(result.completed_partitions, 3)
        self.assertEqual(result.provider_result.final_text, "Final synthesis")
        self.assertTrue(result.coverage["structurally_complete"])
        self.assertEqual(len(stream.segments), 3)
        self.assertEqual(checkpoints[-1]["phase"], "synthesis")
        self.assertIsNotNone(result.manifest_blob_id)

    def test_resume_restores_partition_notes_without_repeating_work(
        self,
    ) -> None:
        service = _ContextService(self.projection)
        first_stream = _Stream()
        checkpoints: list[dict[str, Any]] = []
        partition_calls = 0

        def first_dispatch(
            prompt: str,
            observer: Any,
            images: tuple[Path, ...],
        ) -> ProviderExecutionResult:
            nonlocal partition_calls
            del prompt, observer, images
            partition_calls += 1
            return ProviderExecutionResult(
                0,
                f"Evidence {partition_calls}",
                (),
                {},
            )

        first = execute_partitioned_analysis(
            request="Review the whole document.",
            run_id="run-2",
            plan=object(),
            projection=self.projection,
            context_service=service,
            budget=self.budget,
            fixed_prompt_characters=2_000,
            prompt_factory=lambda message, context: message + "\n" + context,
            dispatch=first_dispatch,
            assistant_stream=first_stream,
            checkpoint=checkpoints.append,
            stop_requested=lambda: partition_calls >= 2,
            blob_store=self.blobs,
        )
        self.assertEqual(first.completed_partitions, 2)

        resumed_prompts: list[str] = []
        resumed = execute_partitioned_analysis(
            request="Review the whole document.",
            run_id="run-2",
            plan=object(),
            projection=self.projection,
            context_service=service,
            budget=self.budget,
            fixed_prompt_characters=2_000,
            prompt_factory=lambda message, context: message + "\n" + context,
            dispatch=lambda prompt, observer, images: (
                resumed_prompts.append(prompt)
                or ProviderExecutionResult(
                    0,
                    (
                        "Final synthesis"
                        if "All 3 structural partitions" in prompt
                        else "Evidence 3"
                    ),
                    (),
                    {},
                )
            ),
            assistant_stream=_Stream(),
            checkpoint=checkpoints.append,
            stop_requested=lambda: False,
            blob_store=self.blobs,
            resume_checkpoint=checkpoints[-1],
        )

        self.assertEqual(len(resumed_prompts), 2)
        self.assertEqual(resumed.completed_partitions, 3)
        self.assertEqual(
            resumed.provider_result.final_text,
            "Final synthesis",
        )


if __name__ == "__main__":
    unittest.main()

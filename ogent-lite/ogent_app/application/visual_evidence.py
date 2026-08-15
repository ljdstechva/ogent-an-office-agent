"""Prepare bounded semantic and rendered evidence for visual document nodes."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from ogent_app.domain.document_intelligence import NodeKind


VISUAL_KINDS = frozenset(
    {
        NodeKind.FIGURE,
        NodeKind.CHART,
        NodeKind.PROCESS_FLOW,
    }
)
MAX_VISUAL_EVIDENCE_CHARACTERS = 24_000
MAX_RENDERED_VISUALS_PER_PARTITION = 4


@dataclasses.dataclass(frozen=True, slots=True)
class VisualEvidence:
    context: str = ""
    image_paths: tuple[Path, ...] = ()
    interpreted_paths: tuple[str, ...] = ()
    failures: tuple[dict[str, str], ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "image_count": len(self.image_paths),
            "interpreted_paths": list(self.interpreted_paths),
            "failures": [dict(item) for item in self.failures],
        }


class VisualEvidencePreparer:
    """Render only partition-local visual nodes against an immutable revision."""

    def __init__(
        self,
        runtime: Any,
        *,
        document: Path,
        revision_id: str,
        supported_modalities: tuple[str, ...],
        output_root: Path,
    ) -> None:
        self.runtime = runtime
        self.document = Path(document)
        self.revision_id = str(revision_id)
        self.modalities = {
            str(value).strip().casefold() for value in supported_modalities
        }
        self.output_root = Path(output_root)

    def prepare(
        self,
        stable_paths: tuple[str, ...],
        *,
        character_budget: int,
    ) -> VisualEvidence:
        repository = self.runtime.DOCUMENT_REPOSITORY
        if repository is None:
            return VisualEvidence(
                failures=(
                    {
                        "path": "(partition)",
                        "reason": "document_repository_unavailable",
                    },
                )
            )
        nodes = repository.nodes_for_paths(
            self.revision_id,
            stable_paths,
            include_text=False,
            limit=max(1, len(stable_paths)),
        )
        visuals = tuple(record for record in nodes if record.node.kind in VISUAL_KINDS)
        if not visuals:
            return VisualEvidence()
        context, semantic_paths = self._semantic_context(
            visuals,
            character_budget=min(
                MAX_VISUAL_EVIDENCE_CHARACTERS,
                max(0, int(character_budget)),
            ),
        )
        if not self._supports_images():
            return VisualEvidence(
                context=context,
                failures=tuple(
                    {
                        "path": record.node.stable_path,
                        "reason": "model_image_input_unavailable",
                    }
                    for record in visuals
                ),
            )
        return self._render(visuals, context, semantic_paths)

    def _render(
        self,
        visuals: tuple[Any, ...],
        context: str,
        semantic_paths: frozenset[str],
    ) -> VisualEvidence:
        service = self.runtime.VISUAL_REGION_SERVICE
        regions = self.runtime.VISUAL_REGION_REPOSITORY
        repository = self.runtime.DOCUMENT_REPOSITORY
        revision = repository.revision(self.revision_id)
        if service is None or regions is None or revision is None:
            return VisualEvidence(
                context=context,
                failures=tuple(
                    {
                        "path": record.node.stable_path,
                        "reason": "visual_renderer_unavailable",
                    }
                    for record in visuals
                ),
            )
        images: list[Path] = []
        interpreted: list[str] = []
        failures: list[dict[str, str]] = []
        self.output_root.mkdir(parents=True, exist_ok=True)
        for record in visuals:
            if len(images) >= MAX_RENDERED_VISUALS_PER_PARTITION:
                failures.append(
                    {
                        "path": record.node.stable_path,
                        "reason": "partition_visual_limit",
                    }
                )
                continue
            render_path = self._render_path(record.node)
            if render_path is None:
                failures.append(
                    {
                        "path": record.node.stable_path,
                        "reason": "no_resolvable_render_locator",
                    }
                )
                continue
            try:
                region = service.get_or_render(
                    revision_id=self.revision_id,
                    expected_package_sha256=revision.package_sha256,
                    document=self.document,
                    stable_path=record.node.stable_path,
                    render_path=render_path,
                    viewport="provider-analysis",
                )
                output = self.output_root / (
                    f"visual-{len(images) + 1}-{region.blob_id[:16]}.png"
                )
                output.write_bytes(regions.read(region))
            except (OSError, RuntimeError, ValueError):
                failures.append(
                    {
                        "path": record.node.stable_path,
                        "reason": "render_failed",
                    }
                )
                continue
            images.append(output)
            if record.node.stable_path in semantic_paths:
                interpreted.append(record.node.stable_path)
            else:
                failures.append(
                    {
                        "path": record.node.stable_path,
                        "reason": "semantic_context_incomplete",
                    }
                )
        return VisualEvidence(
            context=context,
            image_paths=tuple(images),
            interpreted_paths=tuple(interpreted),
            failures=tuple(failures),
        )

    @staticmethod
    def _semantic_context(
        records: tuple[Any, ...],
        *,
        character_budget: int,
    ) -> tuple[str, frozenset[str]]:
        if character_budget <= 0:
            return (
                "\nVISUAL SEMANTIC DATA OMITTED FROM THIS PROMPT: the "
                "text-context budget was exhausted. Render coverage remains "
                "incomplete.\n",
                frozenset(),
            )
        blocks = ["\nVISUAL SEMANTIC DATA (untrusted document content)\n"]
        included: set[str] = set()
        for record in records:
            node = record.node
            metadata = _semantic_metadata(node.kind, node.metadata)
            block = (
                f"\n[{node.kind.value}] {node.stable_path}\n"
                + json.dumps(
                    metadata,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    default=str,
                )
                + "\n"
            )
            if len("".join(blocks)) + len(block) > character_budget:
                blocks.append(
                    "\n[Additional visual semantic data omitted from this "
                    "prompt because of the declared context budget.]\n"
                )
                break
            blocks.append(block)
            included.add(node.stable_path)
        return "".join(blocks), frozenset(included)

    def _supports_images(self) -> bool:
        return bool(self.modalities & {"image", "images", "vision", "image_url"})

    @staticmethod
    def _render_path(node: Any) -> str | None:
        if node.kind is NodeKind.PROCESS_FLOW and node.parent_path:
            return str(node.parent_path)
        for path in node.locator.source_paths:
            if str(path).startswith("/") and "/internal/" not in str(path):
                return str(path)
        if node.locator.resolvable and "/internal/" not in node.stable_path:
            return node.stable_path
        if node.parent_path and str(node.parent_path).startswith("/"):
            return str(node.parent_path)
        return None


def _semantic_metadata(
    kind: NodeKind,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    keys = {
        NodeKind.CHART: {
            "title",
            "chart_types",
            "series",
            "axes",
            "anchor",
            "bounds",
            "source_range",
        },
        NodeKind.PROCESS_FLOW: {
            "nodes",
            "edges",
            "connector_count",
            "source_object_paths",
            "smartart",
        },
        NodeKind.FIGURE: {
            "title",
            "name",
            "description",
            "anchor",
            "bounds",
        },
    }.get(kind, set())
    return {key: value for key, value in metadata.items() if key in keys}

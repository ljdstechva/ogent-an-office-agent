from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from ogent_app.application import VisualRegionService
from ogent_app.application.visual_evidence import VisualEvidencePreparer
from ogent_app.domain.document_intelligence import (
    DocumentFormat,
    NodeKind,
    StructuralManifest,
)
from ogent_app.infrastructure.indexing.common import indexed_node
from ogent_app.infrastructure.sqlite import (
    ContentAddressedBlobStore,
    DocumentRepository,
    SqliteDatabase,
    VisualRegionRepository,
    WorkspaceRepository,
)


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 3), (12, 34, 56)).save(
        output,
        format="PNG",
    )
    return output.getvalue()


class _FakeExecutor:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, ...]] = []

    def version(self) -> str:
        return "officecli 1.0.test"

    def execute(
        self,
        arguments: list[str],
        **_kwargs: object,
    ) -> SimpleNamespace:
        self.calls.append(tuple(arguments))
        output_index = arguments.index("--out") + 1
        Path(arguments[output_index]).write_bytes(self.payload)
        return SimpleNamespace(exit_code=0)


class VisualRegionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = SqliteDatabase(self.root / "state.db")
        self.blobs = ContentAddressedBlobStore(self.root / "blobs")
        WorkspaceRepository(self.database).create("workspace-visual")
        self.document = self.root / "visual.docx"
        self.document.write_bytes(b"visual revision")
        digest = hashlib.sha256(self.document.read_bytes()).hexdigest()
        observed = DocumentRepository(
            self.database,
            self.blobs,
        ).observe(
            workspace_id="workspace-visual",
            source_path=self.document,
            active_path=self.document,
            mode="local_direct",
            document_format=DocumentFormat.DOCX,
            package_sha256=digest,
            quick_manifest=StructuralManifest(
                DocumentFormat.DOCX,
                digest,
                {},
                (),
            ),
        )
        self.revision_id = observed.revision.revision_id
        self.digest = digest
        self.regions = VisualRegionRepository(
            self.database,
            self.blobs,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_render_is_revision_bound_and_cached_by_profile_and_region(self) -> None:
        executor = _FakeExecutor(_png())
        service = VisualRegionService(executor, self.regions)
        first = service.get_or_render(
            revision_id=self.revision_id,
            expected_package_sha256=self.digest,
            document=self.document,
            stable_path="/internal/word/figure[1]",
            render_path="/body/p[1]",
            viewport="desktop-1440",
        )
        second = service.get_or_render(
            revision_id=self.revision_id,
            expected_package_sha256=self.digest,
            document=self.document,
            stable_path="/internal/word/figure[1]",
            render_path="/body/p[1]",
            viewport="desktop-1440",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(self.regions.read(first), _png())
        self.assertIn(
            "officecli 1.0.test",
            first.renderer_profile,
        )

        self.document.write_bytes(b"changed revision")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            service.get_or_render(
                revision_id=self.revision_id,
                expected_package_sha256=self.digest,
                document=self.document,
                stable_path="/internal/word/figure[2]",
                render_path="/body/p[2]",
            )
        self.assertEqual(len(executor.calls), 1)

    def test_repository_rejects_non_png_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a PNG"):
            self.regions.put_png(
                revision_id=self.revision_id,
                stable_path="/figure",
                renderer_profile="renderer",
                region_key="region",
                payload=b"not-an-image",
            )

    def test_partition_visual_evidence_pairs_chart_data_and_render(
        self,
    ) -> None:
        chart = indexed_node(
            "/slides/slide[1]/chart[@id=2]",
            NodeKind.CHART,
            parent_path="/slides/slide[1]",
            title="Quarterly results",
            metadata={
                "chart_types": ["barChart"],
                "series": [
                    {
                        "title": "BOD",
                        "categories": ["Q1", "Q2"],
                        "values": ["12", "9"],
                        "value_formula": "Data!$B$2:$B$3",
                    }
                ],
            },
            resolvable=True,
        )
        render_calls: list[str] = []
        runtime = SimpleNamespace(
            DOCUMENT_REPOSITORY=SimpleNamespace(
                nodes_for_paths=lambda *_args, **_kwargs: (
                    SimpleNamespace(node=chart),
                ),
                revision=lambda _revision_id: SimpleNamespace(
                    package_sha256=self.digest
                ),
            ),
            VISUAL_REGION_SERVICE=SimpleNamespace(
                get_or_render=lambda **kwargs: (
                    render_calls.append(kwargs["render_path"])
                    or SimpleNamespace(blob_id="a" * 64)
                )
            ),
            VISUAL_REGION_REPOSITORY=SimpleNamespace(read=lambda _region: _png()),
        )
        output = self.root / "visual-evidence"
        evidence = VisualEvidencePreparer(
            runtime,
            document=self.document,
            revision_id=self.revision_id,
            supported_modalities=("text", "image"),
            output_root=output,
        ).prepare(
            (chart.stable_path,),
            character_budget=12_000,
        )

        self.assertEqual(render_calls, [chart.stable_path])
        self.assertEqual(evidence.interpreted_paths, (chart.stable_path,))
        self.assertEqual(len(evidence.image_paths), 1)
        self.assertTrue(evidence.image_paths[0].is_file())
        self.assertIn("Data!$B$2:$B$3", evidence.context)
        self.assertIn('"series"', evidence.context)

    def test_visual_coverage_stays_incomplete_without_image_modality(
        self,
    ) -> None:
        figure = indexed_node(
            "/body/p[1]/figure[1]",
            NodeKind.FIGURE,
            parent_path="/body/p[1]",
            metadata={"description": "Treatment process"},
            resolvable=True,
        )
        runtime = SimpleNamespace(
            DOCUMENT_REPOSITORY=SimpleNamespace(
                nodes_for_paths=lambda *_args, **_kwargs: (
                    SimpleNamespace(node=figure),
                ),
            ),
        )
        evidence = VisualEvidencePreparer(
            runtime,
            document=self.document,
            revision_id=self.revision_id,
            supported_modalities=("text",),
            output_root=self.root / "unused",
        ).prepare(
            (figure.stable_path,),
            character_budget=4_000,
        )

        self.assertEqual(evidence.interpreted_paths, ())
        self.assertEqual(
            evidence.failures[0]["reason"],
            "model_image_input_unavailable",
        )
        self.assertIn("Treatment process", evidence.context)

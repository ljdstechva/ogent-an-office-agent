from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from ogent_app.application import (
    DocumentCapabilityBootstrap,
    GatewayAuditIngestor,
    OutcomeVerifier,
    RollbackManager,
)
from ogent_app.domain.capability import (
    CapabilityBootstrapResult,
    CapabilityReceipt,
    DocumentKind,
    MutationCategory,
    SkillPolicy,
    ToolReceipt,
)
from ogent_app.domain.run import RunContract, RunMode, ScopeMode
from ogent_app.infrastructure.officecli import (
    OfficeCliExecution,
    OfficeCliExecutor,
    SkillRegistry,
    TypedOfficeCliGateway,
)
from ogent_app.infrastructure.sqlite import (
    CapabilityReceiptRepository,
    ChangesetRepository,
    ContentAddressedBlobStore,
    EventRepository,
    RunRepository,
    SkillPolicyRepository,
    SqliteDatabase,
    ToolReceiptRepository,
    TurnRepository,
    WorkspaceRepository,
)
from ogent_officecli_mcp import OfficeCLIGate


class CapabilityLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = SqliteDatabase(self.root / "ogent.db")
        self.blobs = ContentAddressedBlobStore(self.root / "blobs")
        self.workspaces = WorkspaceRepository(self.database)
        self.turns = TurnRepository(self.database, self.blobs)
        self.runs = RunRepository(self.database)
        self.events = EventRepository(self.database)
        self.skill_policies = SkillPolicyRepository(
            self.database,
            self.blobs,
        )
        self.capabilities = CapabilityReceiptRepository(self.database)
        self.tools = ToolReceiptRepository(self.database)
        self.workspaces.create("workspace-capability")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_run(self, run_id: str | None = None) -> str:
        turn = self.turns.append(
            "workspace-capability",
            "user",
            "Review the document.",
        )
        run = self.runs.create(
            "workspace-capability",
            turn.turn_id,
            mode=RunMode.REVIEW,
            scope=ScopeMode.SELECTED_ONLY,
            run_id=run_id,
        )
        return run.run_id

    def test_skill_cache_preflight_and_receipt_are_deterministic(self) -> None:
        document = self.root / "report.docx"
        document.write_bytes(b"fixture-package")
        calls: list[tuple[str, ...]] = []

        def runner(
            arguments: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(arguments))
            if arguments[-1] == "--version":
                stdout = "1.0.143-fixture\n"
            elif "load_skill" in arguments:
                stdout = "COMPLETE WORD POLICY\n" * 200
            elif "stats" in arguments:
                stdout = '{"success":true,"count":7}'
            else:
                stdout = '{"success":true,"path":"/body/p[1]"}'
            return subprocess.CompletedProcess(arguments, 0, stdout, "")

        executor = OfficeCliExecutor(
            executable=Path(sys.executable),
            runner=runner,
        )
        gateway = TypedOfficeCliGateway(executor)
        bootstrap = DocumentCapabilityBootstrap(
            SkillRegistry(executor, self.skill_policies),
            gateway,
            self.capabilities,
            self.tools,
        )
        first_run = self.create_run()
        first = bootstrap.bootstrap(
            run_id=first_run,
            workspace_id="workspace-capability",
            document=document,
            document_revision=4,
            scope=ScopeMode.SELECTED_ONLY,
            selected_paths=("/body/p[1]",),
        )
        self.runs.transition(first_run, self._next_state(first_run))
        second_run = self.create_run()
        second = bootstrap.bootstrap(
            run_id=second_run,
            workspace_id="workspace-capability",
            document=document,
            document_revision=4,
            scope=ScopeMode.WHOLE_DOCUMENT,
        )

        self.assertEqual(first.policy.text, "COMPLETE WORD POLICY\n" * 200)
        self.assertEqual(first.policy.policy_sha256, second.policy.policy_sha256)
        self.assertEqual(
            sum("load_skill" in command for command in calls),
            1,
        )
        receipt = self.capabilities.get_for_run(first_run)
        assert receipt is not None
        self.assertEqual(receipt.skill_name, "word")
        self.assertEqual(receipt.document_revision, 4)
        self.assertEqual(receipt.probe["selected_probe_count"], 1)
        operations = [item.operation for item in self.tools.list_for_run(first_run)]
        self.assertEqual(operations, ["inspect_document", "read_nodes"])

    def _next_state(self, run_id: str):
        from ogent_app.domain.workspace import RunState

        del run_id
        return RunState.FAILED

    def test_gateway_audit_is_content_safe_and_ingests_mutation_receipt(
        self,
    ) -> None:
        document = self.root / "active.docx"
        document.write_bytes(b"before")
        audit_root = self.root / "audit"
        audit_root.mkdir()
        run_id = self.create_run(uuid.uuid4().hex)
        audit_path = audit_root / f"{run_id}.jsonl"

        def runner(
            arguments: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if "batch" in arguments:
                document.write_bytes(b"after")
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout='{"success":true,"text":"PRIVATE-CONTENT"}',
                stderr="",
            )

        gate = OfficeCLIGate(
            document,
            read_roots=(audit_root,),
            executable=Path(sys.executable),
            runner=runner,
            allow_mutations=True,
            scope_mode=ScopeMode.WHOLE_DOCUMENT,
            audit_log=audit_path,
            run_id=run_id,
            document_revision=1,
            skill_name="word",
            skill_sha256="a" * 64,
            initial_package_sha256=hashlib.sha256(b"before").hexdigest(),
        )
        result = gate.execute_typed(
            "apply_atomic_batch",
            {
                "commands": [
                    {
                        "command": "set",
                        "path": "/body/p[1]",
                        "props": {"text": "PRIVATE-CONTENT"},
                    }
                ]
            },
        )
        receipts = GatewayAuditIngestor(self.tools).ingest(
            audit_path,
            run_id=run_id,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            receipts[0].mutation_category,
            MutationCategory.MUTATION,
        )
        self.assertTrue(receipts[0].result["package_changed"])
        self.assertNotIn(
            "PRIVATE-CONTENT",
            audit_path.read_text(encoding="utf-8"),
        )

    def test_outcome_verifier_requires_gateway_mutation_and_rollback_restores(
        self,
    ) -> None:
        document = self.root / "verified.docx"
        document.write_bytes(b"before")
        run_id = self.create_run(uuid.uuid4().hex)
        changesets = ChangesetRepository(self.database)
        rollback = RollbackManager(
            self.database,
            self.blobs,
            changesets,
        )
        snapshot = rollback.create(document, run_id=run_id)
        document.write_bytes(b"after")
        execution = OfficeCliExecution(
            arguments=("officecli",),
            exit_code=0,
            stdout='{"success":true}',
            stderr="",
            started_at="2026-07-29T00:00:00+00:00",
            ended_at="2026-07-29T00:00:01+00:00",
        )
        gateway = mock.Mock(spec=TypedOfficeCliGateway)
        gateway.read_nodes.return_value = (execution,)
        gateway.validate.return_value = execution
        gateway.safe_result.side_effect = TypedOfficeCliGateway.safe_result
        verifier = OutcomeVerifier(gateway, self.tools)
        capability = self._capability(run_id)
        mutation = ToolReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=run_id,
            operation="batch",
            started_at=execution.started_at,
            ended_at=execution.ended_at,
            exit_status=0,
            mutation_category=MutationCategory.MUTATION,
            arguments={"selectors": ["/body/p[1]"]},
            result={"package_changed": True},
        )
        accepted = verifier.verify(
            run_id=run_id,
            contract=RunContract(
                RunMode.EDIT,
                ScopeMode.SELECTED_ONLY,
                ("/body/p[1]",),
            ),
            document=document,
            initial_package_sha256=snapshot.package_sha256,
            capability=capability,
            provider_receipts=(mutation,),
            expected_paths=("/body/p[1]",),
        )
        missing_receipt = verifier.verify(
            run_id=run_id,
            contract=RunContract(
                RunMode.EDIT,
                ScopeMode.SELECTED_ONLY,
                ("/body/p[1]",),
            ),
            document=document,
            initial_package_sha256=snapshot.package_sha256,
            capability=capability,
            provider_receipts=(),
            expected_paths=("/body/p[1]",),
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.outcome, "edit_completed")
        self.assertTrue(missing_receipt.rollback_required)
        self.assertEqual(rollback.restore(snapshot, document), snapshot.package_sha256)
        self.assertEqual(document.read_bytes(), b"before")

    def _capability(self, run_id: str) -> CapabilityBootstrapResult:
        policy = SkillPolicy(
            officecli_version="1.0.143",
            skill_name="word",
            policy_sha256="a" * 64,
            policy_blob_id="b" * 64,
            text="policy",
            loaded_at="2026-07-29T00:00:00+00:00",
        )
        receipt = CapabilityReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=run_id,
            workspace_id="workspace-capability",
            skill_name="word",
            skill_sha256=policy.policy_sha256,
            policy_blob_id=policy.policy_blob_id,
            officecli_version=policy.officecli_version,
            document_path_key="fixture",
            document_revision=1,
            package_sha256=hashlib.sha256(b"before").hexdigest(),
            probe_operation="view_stats",
            probe={"success": True},
            created_at=policy.loaded_at,
        )
        return CapabilityBootstrapResult(
            document_kind=DocumentKind.WORD,
            policy=policy,
            receipt=receipt,
            stable_paths=("/body/p[1]",),
        )

    @staticmethod
    def _write_minimal_docx(document: Path) -> None:
        """Author a schema-valid one-paragraph package with the stdlib.

        The pinned CI OfficeCLI fork prerelease cannot run ``create`` (its
        viewer-focused build trimmed that path), so the fixture is written
        directly and then OfficeCLI-validated.
        """
        wordml = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType='
            '"application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument'
            '.wordprocessingml.document.main+xml"/>'
            "</Types>"
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns='
            '"http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>"
        )
        body = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{wordml}"><w:body>'
            f"<w:p><w:r><w:t>Capability preflight fixture.</w:t></w:r></w:p>"
            f'<w:sectPr/></w:body></w:document>'
        )
        with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("[Content_Types].xml", content_types)
            package.writestr("_rels/.rels", rels)
            package.writestr("word/document.xml", body)

    @unittest.skipUnless(shutil.which("officecli"), "OfficeCLI is not installed")
    def test_real_officecli_skill_and_stats_preflight(self) -> None:
        document = self.root / "real.docx"
        self._write_minimal_docx(document)
        # The CI fork prerelease cannot run officecli create/validate (its
        # viewer build trimmed those XML paths), so package sanity comes from
        # the real stats preflight below rather than a standalone validate.
        run_id = self.create_run()
        executor = OfficeCliExecutor()
        bootstrap = DocumentCapabilityBootstrap(
            SkillRegistry(executor, self.skill_policies),
            TypedOfficeCliGateway(executor),
            self.capabilities,
            self.tools,
        )

        result = bootstrap.bootstrap(
            run_id=run_id,
            workspace_id="workspace-capability",
            document=document,
            document_revision=1,
            scope=ScopeMode.WHOLE_DOCUMENT,
        )

        self.assertEqual(result.document_kind, DocumentKind.WORD)
        self.assertGreater(len(result.policy.text), 10_000)
        self.assertTrue(result.receipt.probe["success"])


if __name__ == "__main__":
    unittest.main()

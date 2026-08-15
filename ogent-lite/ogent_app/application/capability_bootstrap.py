"""Mandatory document-skill resolution and real OfficeCLI preflight."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Iterable

from ogent_app.domain.capability import (
    CapabilityBootstrapResult,
    DocumentKind,
    FORMAT_SKILLS,
    MutationCategory,
    ToolReceipt,
)
from ogent_app.domain.run import ScopeMode
from ogent_app.infrastructure.officecli import (
    OfficeCliExecution,
    OfficeCliExecutionError,
    SkillRegistry,
    TypedOfficeCliGateway,
)
from ogent_app.infrastructure.sqlite import (
    CapabilityReceiptRepository,
    ToolReceiptRepository,
)


class DocumentCapabilityBootstrap:
    def __init__(
        self,
        skills: SkillRegistry,
        gateway: TypedOfficeCliGateway,
        capabilities: CapabilityReceiptRepository,
        tools: ToolReceiptRepository,
    ) -> None:
        self.skills = skills
        self.gateway = gateway
        self.capabilities = capabilities
        self.tools = tools

    @staticmethod
    def package_sha256(document: Path) -> str:
        digest = hashlib.sha256()
        with Path(document).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _document_kind(document: Path) -> DocumentKind:
        kind = FORMAT_SKILLS.get(document.suffix.casefold())
        if kind is None:
            raise OfficeCliExecutionError(
                "No OfficeCLI skill maps to the active document format."
            )
        return kind

    def _record_probe(
        self,
        *,
        run_id: str,
        operation: str,
        execution: OfficeCliExecution,
        skill_name: str,
        skill_sha256: str,
        document_revision: int,
        package_sha256: str,
        arguments: dict[str, object],
    ) -> None:
        result = self.gateway.safe_result(execution)
        self.tools.record(
            ToolReceipt(
                receipt_id=uuid.uuid4().hex,
                run_id=run_id,
                operation=operation,
                skill_name=skill_name,
                skill_sha256=skill_sha256,
                document_revision=document_revision,
                package_sha256=package_sha256,
                started_at=execution.started_at,
                ended_at=execution.ended_at,
                exit_status=execution.exit_code,
                mutation_category=MutationCategory.READ,
                arguments=arguments,
                result=result,
                output_sha256=str(result["output_sha256"]),
                output_bytes=int(result["output_bytes"]),
            )
        )

    def bootstrap(
        self,
        *,
        run_id: str,
        workspace_id: str,
        document: Path,
        document_revision: int,
        scope: ScopeMode,
        selected_paths: Iterable[str] = (),
    ) -> CapabilityBootstrapResult:
        active = self.gateway.validate_document(document)
        kind = self._document_kind(active)
        before = active.stat()
        package_sha256 = self.package_sha256(active)
        after = active.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise OfficeCliExecutionError(
                "The active document changed during capability bootstrap."
            )
        policy = self.skills.resolve(kind.value)
        stats = self.gateway.inspect_document(active, mode="stats")
        self._record_probe(
            run_id=run_id,
            operation="inspect_document",
            execution=stats,
            skill_name=policy.skill_name,
            skill_sha256=policy.policy_sha256,
            document_revision=document_revision,
            package_sha256=package_sha256,
            arguments={"mode": "stats"},
        )
        if stats.exit_code != 0:
            raise OfficeCliExecutionError("OfficeCLI document preflight failed.")
        stable_paths = tuple(
            dict.fromkeys(
                str(path).strip() for path in selected_paths if str(path).strip()
            )
        )
        scoped_results: tuple[OfficeCliExecution, ...] = ()
        if stable_paths and scope is not ScopeMode.WHOLE_DOCUMENT:
            scoped_results = self.gateway.read_nodes(
                active,
                stable_paths,
                depth=2,
            )
            for stable_path, execution in zip(
                stable_paths,
                scoped_results,
                strict=True,
            ):
                self._record_probe(
                    run_id=run_id,
                    operation="read_nodes",
                    execution=execution,
                    skill_name=policy.skill_name,
                    skill_sha256=policy.policy_sha256,
                    document_revision=document_revision,
                    package_sha256=package_sha256,
                    arguments={"path": stable_path, "depth": 2},
                )
                if execution.exit_code != 0:
                    raise OfficeCliExecutionError(
                        "OfficeCLI could not inspect the selected document target."
                    )
        probe = {
            **self.gateway.safe_result(stats),
            "scope": scope.value,
            "selected_probe_count": len(scoped_results),
            "stable_paths": list(stable_paths),
        }
        receipt = self.capabilities.create(
            run_id=run_id,
            workspace_id=workspace_id,
            policy=policy,
            document_path_key=os.path.normcase(str(active)),
            document_revision=document_revision,
            package_sha256=package_sha256,
            probe_operation="view_stats",
            probe=probe,
        )
        return CapabilityBootstrapResult(
            document_kind=kind,
            policy=policy,
            receipt=receipt,
            stable_paths=stable_paths,
        )

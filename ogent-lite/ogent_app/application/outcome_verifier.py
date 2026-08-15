"""Server-side Office run evidence verification."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from ogent_app.domain.capability import (
    CapabilityBootstrapResult,
    MutationCategory,
    ToolReceipt,
)
from ogent_app.domain.run import RunContract
from ogent_app.domain.verification import OutcomeVerification
from ogent_app.infrastructure.officecli import TypedOfficeCliGateway
from ogent_app.infrastructure.sqlite import ToolReceiptRepository


class OutcomeVerifier:
    def __init__(
        self,
        gateway: TypedOfficeCliGateway,
        receipts: ToolReceiptRepository,
    ) -> None:
        self.gateway = gateway
        self.receipts = receipts

    @staticmethod
    def package_sha256(document: Path) -> str:
        return hashlib.sha256(Path(document).read_bytes()).hexdigest()

    def _record_backend_receipt(
        self,
        *,
        run_id: str,
        operation: str,
        execution: Any,
        capability: CapabilityBootstrapResult,
        arguments: dict[str, Any],
        category: MutationCategory,
    ) -> ToolReceipt:
        result = self.gateway.safe_result(execution)
        receipt = ToolReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=run_id,
            operation=operation,
            skill_name=capability.policy.skill_name,
            skill_sha256=capability.policy.policy_sha256,
            document_revision=capability.receipt.document_revision,
            package_sha256=capability.receipt.package_sha256,
            started_at=execution.started_at,
            ended_at=execution.ended_at,
            exit_status=execution.exit_code,
            mutation_category=category,
            arguments=arguments,
            result=result,
            output_sha256=str(result["output_sha256"]),
            output_bytes=int(result["output_bytes"]),
        )
        self.receipts.record(receipt)
        return receipt

    def verify(
        self,
        *,
        run_id: str,
        contract: RunContract,
        document: Path,
        initial_package_sha256: str,
        capability: CapabilityBootstrapResult,
        provider_receipts: tuple[ToolReceipt, ...],
        expected_paths: tuple[str, ...],
    ) -> OutcomeVerification:
        final_hash = self.package_sha256(document)
        changed = final_hash != initial_package_sha256
        mutation_receipts = tuple(
            receipt
            for receipt in provider_receipts
            if receipt.mutation_category
            in {MutationCategory.MUTATION, MutationCategory.REFRESH}
            and receipt.successful
            and bool(receipt.result.get("package_changed"))
        )
        if contract.analysis_only:
            if changed:
                return OutcomeVerification(
                    accepted=False,
                    outcome="error",
                    package_changed=True,
                    affected_paths=expected_paths,
                    assertions={"unexpected_mutation": True},
                    rollback_required=True,
                    reason="A read-only run changed the active document.",
                )
            return OutcomeVerification(
                accepted=True,
                outcome="analysis_completed",
                package_changed=False,
                affected_paths=expected_paths,
                assertions={
                    "capability_receipt": True,
                    "inspection_receipt": True,
                },
            )
        if not changed:
            return OutcomeVerification(
                accepted=True,
                outcome="no_change",
                package_changed=False,
                affected_paths=expected_paths,
                assertions={"mutation_evidence": False},
            )
        if not mutation_receipts:
            return OutcomeVerification(
                accepted=False,
                outcome="error",
                package_changed=True,
                affected_paths=expected_paths,
                assertions={"gateway_mutation_receipt": False},
                rollback_required=True,
                reason=(
                    "The document changed without an audited OfficeCLI "
                    "mutation receipt."
                ),
            )
        if expected_paths:
            readbacks = self.gateway.read_nodes(
                document,
                expected_paths,
                depth=2,
            )
            readback_ok = all(item.exit_code == 0 for item in readbacks)
            for path, execution in zip(expected_paths, readbacks, strict=True):
                self._record_backend_receipt(
                    run_id=run_id,
                    operation="read_nodes",
                    execution=execution,
                    capability=capability,
                    arguments={"path": path, "depth": 2},
                    category=MutationCategory.READ,
                )
        else:
            execution = self.gateway.inspect_document(document, mode="stats")
            readback_ok = execution.exit_code == 0
            self._record_backend_receipt(
                run_id=run_id,
                operation="inspect_document",
                execution=execution,
                capability=capability,
                arguments={"mode": "stats", "post_mutation": True},
                category=MutationCategory.READ,
            )
        validation = self.gateway.validate(document)
        self._record_backend_receipt(
            run_id=run_id,
            operation="validate_document",
            execution=validation,
            capability=capability,
            arguments={},
            category=MutationCategory.VALIDATION,
        )
        assertions = {
            "gateway_mutation_receipt": True,
            "targeted_readback": readback_ok,
            "officecli_validate": validation.exit_code == 0,
            "pre_package_sha256": initial_package_sha256,
            "post_package_sha256": final_hash,
        }
        accepted = readback_ok and validation.exit_code == 0
        return OutcomeVerification(
            accepted=accepted,
            outcome="edit_completed" if accepted else "error",
            package_changed=True,
            affected_paths=expected_paths,
            assertions=assertions,
            rollback_required=not accepted,
            reason=(
                None
                if accepted
                else "Document readback or OfficeCLI validation failed."
            ),
        )

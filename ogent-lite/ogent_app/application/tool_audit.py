"""Validate and persist gateway-owned JSONL tool receipts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ogent_app.domain.capability import MutationCategory, ToolReceipt
from ogent_app.infrastructure.sqlite import ToolReceiptRepository


MAX_AUDIT_BYTES = 32 * 1024 * 1024
MAX_AUDIT_LINES = 10_000
HEX_ID = re.compile(r"[0-9a-f]{32}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class GatewayAuditError(RuntimeError):
    pass


class GatewayAuditIngestor:
    def __init__(self, repository: ToolReceiptRepository) -> None:
        self.repository = repository

    def ingest(
        self,
        path: Path,
        *,
        run_id: str,
    ) -> tuple[ToolReceipt, ...]:
        target = Path(path)
        if not target.exists():
            return ()
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_size > MAX_AUDIT_BYTES
        ):
            raise GatewayAuditError("The OfficeCLI gateway audit log is unsafe.")
        receipts: list[ToolReceipt] = []
        with target.open("r", encoding="utf-8") as stream:
            for index, line in enumerate(stream):
                if index >= MAX_AUDIT_LINES:
                    raise GatewayAuditError(
                        "The OfficeCLI gateway audit log has too many records."
                    )
                try:
                    payload = json.loads(line)
                except ValueError as exc:
                    raise GatewayAuditError(
                        "The OfficeCLI gateway audit log is malformed."
                    ) from exc
                receipts.append(self._receipt(payload, run_id=run_id))
        for receipt in receipts:
            self.repository.record(receipt)
        return tuple(receipts)

    @staticmethod
    def _receipt(payload: Any, *, run_id: str) -> ToolReceipt:
        if not isinstance(payload, dict):
            raise GatewayAuditError("A gateway receipt is not an object.")
        receipt_id = str(payload.get("id") or "")
        payload_run_id = str(payload.get("run_id") or "")
        if (
            not HEX_ID.fullmatch(receipt_id)
            or payload_run_id != run_id
            or not HEX_ID.fullmatch(payload_run_id)
        ):
            raise GatewayAuditError("A gateway receipt has an invalid identity.")
        category = MutationCategory(str(payload.get("mutation_category") or "read"))
        arguments = payload.get("arguments")
        result = payload.get("result")
        if not isinstance(arguments, dict) or not isinstance(result, dict):
            raise GatewayAuditError("A gateway receipt has invalid metadata.")
        output_sha256 = str(payload.get("output_sha256") or "")
        if output_sha256 and not SHA256.fullmatch(output_sha256):
            raise GatewayAuditError("A gateway receipt has an invalid output hash.")
        output_bytes = payload.get("output_bytes")
        if output_bytes is not None:
            output_bytes = int(output_bytes)
            if output_bytes < 0:
                raise GatewayAuditError("A gateway receipt has an invalid output size.")
        return ToolReceipt(
            receipt_id=receipt_id,
            run_id=payload_run_id,
            operation=str(payload.get("operation") or "unknown")[:80],
            skill_name=(
                str(payload.get("skill_name")) if payload.get("skill_name") else None
            ),
            skill_sha256=(
                str(payload.get("skill_sha256"))
                if payload.get("skill_sha256")
                else None
            ),
            document_revision=(
                int(payload["document_revision"])
                if payload.get("document_revision") is not None
                else None
            ),
            package_sha256=(
                str(payload.get("package_sha256"))
                if payload.get("package_sha256")
                else None
            ),
            started_at=str(payload.get("started_at") or ""),
            ended_at=(
                str(payload.get("ended_at")) if payload.get("ended_at") else None
            ),
            exit_status=(
                int(payload["exit_status"])
                if payload.get("exit_status") is not None
                else None
            ),
            mutation_category=category,
            arguments=dict(arguments),
            result=dict(result),
            output_sha256=output_sha256 or None,
            output_bytes=output_bytes,
        )

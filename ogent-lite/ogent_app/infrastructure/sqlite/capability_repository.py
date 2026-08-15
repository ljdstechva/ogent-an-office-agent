"""Persistence for cached skills, capability receipts, and tool audits."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from ogent_app.domain.capability import (
    CapabilityReceipt,
    MutationCategory,
    SkillPolicy,
    ToolReceipt,
)

from .blob_store import ContentAddressedBlobStore
from .connection import SqliteDatabase, utc_now_iso


class SkillPolicyRepository:
    def __init__(
        self,
        database: SqliteDatabase,
        blobs: ContentAddressedBlobStore,
    ) -> None:
        self.database = database
        self.blobs = blobs

    def get(self, officecli_version: str, skill_name: str) -> SkillPolicy | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM skill_policies "
                "WHERE officecli_version = ? AND skill_name = ?",
                (officecli_version, skill_name),
            ).fetchone()
        if row is None:
            return None
        text = self.blobs.read_text(str(row["policy_blob_id"]))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != str(row["policy_sha256"]):
            raise OSError("A cached OfficeCLI skill failed integrity verification.")
        return SkillPolicy(
            officecli_version=str(row["officecli_version"]),
            skill_name=str(row["skill_name"]),
            policy_sha256=digest,
            policy_blob_id=str(row["policy_blob_id"]),
            text=text,
            loaded_at=str(row["loaded_at"]),
        )

    def put(
        self,
        officecli_version: str,
        skill_name: str,
        text: str,
    ) -> SkillPolicy:
        blob = self.blobs.put_text(text)
        timestamp = utc_now_iso()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO blobs("
                "id, sha256, byte_size, media_type, relative_path, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    blob.blob_id,
                    blob.sha256,
                    blob.byte_size,
                    blob.media_type,
                    blob.relative_path,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO skill_policies("
                "id, officecli_version, skill_name, policy_blob_id, "
                "policy_sha256, loaded_at"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(officecli_version, skill_name) DO UPDATE SET "
                "policy_blob_id = excluded.policy_blob_id, "
                "policy_sha256 = excluded.policy_sha256, "
                "loaded_at = excluded.loaded_at",
                (
                    uuid.uuid4().hex,
                    officecli_version,
                    skill_name,
                    blob.blob_id,
                    blob.sha256,
                    timestamp,
                ),
            )
        cached = self.get(officecli_version, skill_name)
        assert cached is not None
        return cached


class CapabilityReceiptRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def create(
        self,
        *,
        run_id: str,
        workspace_id: str,
        policy: SkillPolicy,
        document_path_key: str,
        document_revision: int,
        package_sha256: str,
        probe_operation: str,
        probe: dict[str, Any],
    ) -> CapabilityReceipt:
        receipt = CapabilityReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=run_id,
            workspace_id=workspace_id,
            skill_name=policy.skill_name,
            skill_sha256=policy.policy_sha256,
            policy_blob_id=policy.policy_blob_id,
            officecli_version=policy.officecli_version,
            document_path_key=document_path_key,
            document_revision=max(0, int(document_revision)),
            package_sha256=package_sha256,
            probe_operation=probe_operation,
            probe=json.loads(json.dumps(probe, ensure_ascii=False, default=str)),
            created_at=utc_now_iso(),
        )
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO capability_receipts("
                "id, run_id, workspace_id, skill_name, skill_sha256, "
                "policy_blob_id, officecli_version, document_path_key, "
                "document_revision, package_sha256, probe_operation, "
                "probe_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.run_id,
                    receipt.workspace_id,
                    receipt.skill_name,
                    receipt.skill_sha256,
                    receipt.policy_blob_id,
                    receipt.officecli_version,
                    receipt.document_path_key,
                    receipt.document_revision,
                    receipt.package_sha256,
                    receipt.probe_operation,
                    json.dumps(
                        receipt.probe,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    receipt.created_at,
                ),
            )
        return receipt

    def get_for_run(self, run_id: str) -> CapabilityReceipt | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM capability_receipts WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return CapabilityReceipt(
            receipt_id=str(row["id"]),
            run_id=str(row["run_id"]),
            workspace_id=str(row["workspace_id"]),
            skill_name=str(row["skill_name"]),
            skill_sha256=str(row["skill_sha256"]),
            policy_blob_id=str(row["policy_blob_id"]),
            officecli_version=str(row["officecli_version"]),
            document_path_key=str(row["document_path_key"]),
            document_revision=int(row["document_revision"]),
            package_sha256=str(row["package_sha256"]),
            probe_operation=str(row["probe_operation"]),
            probe=json.loads(str(row["probe_json"])),
            created_at=str(row["created_at"]),
        )


class ToolReceiptRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def record(
        self,
        receipt: ToolReceipt,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            with self.database.transaction() as transaction:
                self.record(receipt, connection=transaction)
            return
        connection.execute(
            "INSERT OR IGNORE INTO tool_receipts("
            "id, run_id, operation, skill_name, skill_sha256, "
            "document_revision, package_sha256, started_at, ended_at, "
            "exit_status, mutation_category, result_json, arguments_json, "
            "output_sha256, output_bytes"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.receipt_id,
                receipt.run_id,
                receipt.operation,
                receipt.skill_name,
                receipt.skill_sha256,
                receipt.document_revision,
                receipt.package_sha256,
                receipt.started_at,
                receipt.ended_at,
                receipt.exit_status,
                receipt.mutation_category.value,
                json.dumps(
                    receipt.result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(
                    receipt.arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                receipt.output_sha256,
                receipt.output_bytes,
            ),
        )

    def list_for_run(self, run_id: str) -> tuple[ToolReceipt, ...]:
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_receipts WHERE run_id = ? ORDER BY started_at, id",
                (run_id,),
            ).fetchall()
        return tuple(
            ToolReceipt(
                receipt_id=str(row["id"]),
                run_id=str(row["run_id"]),
                operation=str(row["operation"]),
                skill_name=row["skill_name"],
                skill_sha256=row["skill_sha256"],
                document_revision=row["document_revision"],
                package_sha256=row["package_sha256"],
                started_at=str(row["started_at"]),
                ended_at=row["ended_at"],
                exit_status=row["exit_status"],
                mutation_category=MutationCategory(row["mutation_category"]),
                arguments=json.loads(str(row["arguments_json"])),
                result=json.loads(str(row["result_json"])),
                output_sha256=row["output_sha256"],
                output_bytes=row["output_bytes"],
            )
            for row in rows
        )

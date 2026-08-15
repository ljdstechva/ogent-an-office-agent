"""Coverage ledgers and revision-bound lazy visual-region cache."""

from __future__ import annotations

import io
import json
import sqlite3
import uuid
from typing import Any

from PIL import Image

from ogent_app.domain.document_intelligence import CoverageLedger, VisualRegion

from .blob_store import BlobRef, ContentAddressedBlobStore
from .connection import SqliteDatabase, utc_now_iso


class CoverageRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def save(self, run_id: str, ledger: CoverageLedger) -> str:
        now = utc_now_iso()
        required_payload = {
            "categories": {
                key: list(paths)
                for key, paths in ledger.required_paths_by_category.items()
            },
            "visuals": list(ledger.required_visual_paths),
        }
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM coverage_ledgers WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            identifier = str(existing["id"]) if existing else uuid.uuid4().hex
            connection.execute(
                "INSERT INTO coverage_ledgers("
                "id, run_id, revision_id, required_paths_json, "
                "reviewed_paths_json, unsupported_json, visuals_json, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "revision_id = excluded.revision_id, "
                "required_paths_json = excluded.required_paths_json, "
                "reviewed_paths_json = excluded.reviewed_paths_json, "
                "unsupported_json = excluded.unsupported_json, "
                "visuals_json = excluded.visuals_json, "
                "updated_at = excluded.updated_at",
                (
                    identifier,
                    run_id,
                    ledger.revision_id,
                    self._json(required_payload),
                    self._json(
                        {
                            key: list(paths)
                            for key, paths in ledger.reviewed_paths_by_category.items()
                        }
                    ),
                    self._json(list(ledger.unreadable_or_unsupported)),
                    self._json(list(ledger.visual_interpretation_used)),
                    now,
                    now,
                ),
            )
        return identifier

    def get(self, run_id: str) -> CoverageLedger | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM coverage_ledgers WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        required = json.loads(str(row["required_paths_json"]))
        return CoverageLedger(
            revision_id=str(row["revision_id"]),
            required_paths_by_category={
                str(key): tuple(str(path) for path in paths)
                for key, paths in required.get("categories", {}).items()
            },
            reviewed_paths_by_category={
                str(key): tuple(str(path) for path in paths)
                for key, paths in json.loads(str(row["reviewed_paths_json"])).items()
            },
            required_visual_paths=tuple(
                str(path) for path in required.get("visuals", ())
            ),
            unreadable_or_unsupported=tuple(
                str(value) for value in json.loads(str(row["unsupported_json"]))
            ),
            visual_interpretation_used=tuple(
                str(value) for value in json.loads(str(row["visuals_json"]))
            ),
        )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class VisualRegionRepository:
    def __init__(
        self,
        database: SqliteDatabase,
        blobs: ContentAddressedBlobStore,
    ) -> None:
        self.database = database
        self.blobs = blobs

    def get(
        self,
        revision_id: str,
        stable_path: str,
        renderer_profile: str,
        region_key: str,
    ) -> VisualRegion | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM visual_regions WHERE revision_id = ? "
                "AND stable_path = ? AND renderer_profile = ? "
                "AND region_key = ?",
                (
                    revision_id,
                    stable_path,
                    renderer_profile,
                    region_key,
                ),
            ).fetchone()
        return self._record(row) if row is not None else None

    def put_png(
        self,
        *,
        revision_id: str,
        stable_path: str,
        renderer_profile: str,
        region_key: str,
        payload: bytes,
    ) -> VisualRegion:
        data = bytes(payload)
        self._validate_png(data)
        blob = self.blobs.put_bytes(data, media_type="image/png")
        now = utc_now_iso()
        identifier = uuid.uuid4().hex
        with self.database.transaction() as connection:
            self._insert_blob(connection, blob, now)
            connection.execute(
                "INSERT INTO visual_regions("
                "id, revision_id, stable_path, renderer_profile, "
                "region_key, blob_id, media_type, byte_size, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'image/png', ?, ?) "
                "ON CONFLICT("
                "revision_id, stable_path, renderer_profile, region_key"
                ") DO NOTHING",
                (
                    identifier,
                    revision_id,
                    stable_path,
                    renderer_profile,
                    region_key,
                    blob.blob_id,
                    blob.byte_size,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM visual_regions WHERE revision_id = ? "
                "AND stable_path = ? AND renderer_profile = ? "
                "AND region_key = ?",
                (
                    revision_id,
                    stable_path,
                    renderer_profile,
                    region_key,
                ),
            ).fetchone()
        assert row is not None
        return self._record(row)

    def read(self, region: VisualRegion) -> bytes:
        return self.blobs.read_bytes(region.blob_id)

    @staticmethod
    def _validate_png(payload: bytes) -> None:
        if len(payload) < 24 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("The rendered visual region is not a PNG.")
        try:
            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
                width, height = image.size
        except Exception as exc:
            raise ValueError(
                "The rendered visual region failed image validation."
            ) from exc
        if width <= 0 or height <= 0 or width * height > 100_000_000:
            raise ValueError("The rendered visual region has unsafe dimensions.")

    @staticmethod
    def _insert_blob(
        connection: sqlite3.Connection,
        blob: BlobRef,
        now: str,
    ) -> None:
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
                now,
            ),
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> VisualRegion:
        return VisualRegion(
            revision_id=str(row["revision_id"]),
            stable_path=str(row["stable_path"]),
            renderer_profile=str(row["renderer_profile"]),
            region_key=str(row["region_key"]),
            media_type=str(row["media_type"]),
            blob_id=str(row["blob_id"]),
            byte_size=int(row["byte_size"]),
            created_at=str(row["created_at"]),
        )

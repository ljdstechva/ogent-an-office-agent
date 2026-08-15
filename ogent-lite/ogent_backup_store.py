#!/usr/bin/env python3
"""Verified recovery backups for Ogent direct-edit document sessions.

The backup root is intentionally separate from Ogent workspaces and provider
working directories.  Every public operation is serialized so creation,
cleanup, inspection, folder opening, and restore cannot race one another.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable


MANIFEST_SCHEMA_VERSION = 1
RETENTION = dt.timedelta(days=30)
CLEANUP_INTERVAL = dt.timedelta(hours=6)
SUPPORTED_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
BACKUP_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STATE_FILENAME = ".cleanup-state.json"
MANIFEST_FILENAME = "manifest.json"
MAX_MANIFEST_BYTES = 128 * 1024
MIN_FREE_SPACE_MARGIN = 64 * 1024


class BackupError(RuntimeError):
    """A recovery error that is safe to surface in the local Ogent UI."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC timestamp has no timezone.")
    return parsed.astimezone(dt.timezone.utc)


def utc_iso(value: dt.datetime) -> str:
    normalized = value.astimezone(dt.timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def sanitize_source_name(value: str) -> str:
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "-", value).strip(" .-_")
    return (cleaned[:120] or "document")


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


@dataclasses.dataclass(frozen=True)
class BackupRecord:
    backup_id: str
    backup_path: Path
    manifest_path: Path
    source_path: Path
    source_name: str
    extension: str
    sha256: str
    byte_size: int
    application_version: str
    created_at: str
    expires_at: str
    pending_delete: bool = False
    delete_error: str | None = None

    def public_metadata(self) -> dict[str, Any]:
        """Return recovery metadata without exposing the original source path."""
        return {
            "id": self.backup_id,
            "filename": self.backup_path.name,
            "source_name": self.source_name,
            "extension": self.extension,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "application_version": self.application_version,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "pending_delete": self.pending_delete,
            "delete_error": self.delete_error,
        }


class BackupStore:
    """Create, inspect, restore, and expire independently verified backups."""

    def __init__(
        self,
        root: Path,
        *,
        application_version: str,
        clock: Callable[[], dt.datetime] = utc_now,
        disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
        popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.application_version = application_version
        self.clock = clock
        self.disk_usage = disk_usage
        self.popen = popen
        self.lock = threading.RLock()
        self.last_cleanup: dict[str, Any] | None = None
        self.next_cleanup_at: dt.datetime | None = None

    @property
    def state_path(self) -> Path:
        return self.root / STATE_FILENAME

    def initialize(self) -> dict[str, Any]:
        """Create the root, remove abandoned partials, and run startup cleanup."""
        with self.lock:
            self._ensure_root()
            self._load_cleanup_state()
            self._remove_abandoned_partials()
            return self.cleanup_expired(reason="startup")

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise BackupError("The recovery backup folder is not a safe directory.")
        resolved = self.root.resolve(strict=True)
        if resolved != self.root:
            self.root = resolved

    def _now(self) -> dt.datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise BackupError("The recovery clock must return a timezone-aware value.")
        return value.astimezone(dt.timezone.utc)

    @staticmethod
    def _source_metadata(source: Path) -> tuple[int, int, int | None]:
        stat = source.stat()
        return (stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", None))

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    payload,
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    @staticmethod
    def _check_source_writable(source: Path) -> None:
        if not os.access(source, os.W_OK):
            raise BackupError("The selected document is read-only or not writable.")
        try:
            descriptor = os.open(source, os.O_RDWR)
        except OSError as exc:
            raise BackupError(
                "The selected document cannot be opened for writing. "
                "Close any application that has locked it and try again."
            ) from exc
        else:
            os.close(descriptor)

    def _require_contained(self, path: Path, label: str) -> Path:
        candidate = path.resolve(strict=False)
        if not path_is_within(candidate, self.root) or candidate == self.root:
            raise BackupError(
                f"Refusing {label} outside the recovery backup folder."
            )
        return candidate

    def create_backup(self, source_path: Path) -> BackupRecord:
        """Create and verify one physical backup before direct editing begins."""
        with self.lock:
            self._ensure_root()
            source = Path(source_path).expanduser().resolve(strict=True)
            extension = source.suffix.casefold()
            if extension not in SUPPORTED_EXTENSIONS:
                raise BackupError(
                    "Recovery backups support DOCX, XLSX, and PPTX documents."
                )
            if not source.is_file() or source.is_symlink():
                raise BackupError("The selected document is not a regular local file.")
            self._check_source_writable(source)

            initial_metadata = self._source_metadata(source)
            byte_size = initial_metadata[0]
            try:
                free = self.disk_usage(self.root).free
            except OSError as exc:
                raise BackupError(
                    "Available recovery-disk space could not be checked."
                ) from exc
            if free < byte_size + MIN_FREE_SPACE_MARGIN:
                raise BackupError(
                    "There is not enough free space to create a verified recovery "
                    "backup."
                )

            source_hash = self._hash_file(source)
            if self._source_metadata(source) != initial_metadata:
                raise BackupError(
                    "The source document changed while its recovery hash was read. "
                    "Close other editors and try again."
                )

            backup_id = uuid.uuid4().hex
            created = self._now()
            expires = created + RETENTION
            timestamp = created.strftime("%Y%m%dT%H%M%S.%fZ")
            safe_stem = sanitize_source_name(source.stem)
            filename = f"{safe_stem}--{timestamp}--{backup_id[:8]}{extension}"
            partial_directory = self._require_contained(
                self.root / f".{backup_id}.partial",
                "partial backup creation",
            )
            final_directory = self._require_contained(
                self.root / backup_id,
                "backup creation",
            )
            if partial_directory.exists() or final_directory.exists():
                raise BackupError("A recovery backup identifier collision occurred.")
            partial_directory.mkdir(parents=False, exist_ok=False)
            partial_file = partial_directory / f"{filename}.partial"
            final_file_in_partial = partial_directory / filename
            manifest_path_in_partial = partial_directory / MANIFEST_FILENAME
            try:
                copied_hash = hashlib.sha256()
                copied_bytes = 0
                with source.open("rb") as input_stream, partial_file.open(
                    "xb"
                ) as output_stream:
                    for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                        output_stream.write(chunk)
                        copied_hash.update(chunk)
                        copied_bytes += len(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                if copied_bytes != byte_size or copied_hash.hexdigest() != source_hash:
                    raise BackupError(
                        "The recovery copy did not match the source byte-for-byte."
                    )
                if self._hash_file(partial_file) != source_hash:
                    raise BackupError("Recovery backup SHA-256 verification failed.")
                final_metadata = self._source_metadata(source)
                final_source_hash = self._hash_file(source)
                if (
                    final_metadata != initial_metadata
                    or final_source_hash != source_hash
                ):
                    raise BackupError(
                        "The source document changed during backup creation. "
                        "Direct editing was not started."
                    )
                os.replace(partial_file, final_file_in_partial)
                manifest = {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "backup_id": backup_id,
                    "backup_file": filename,
                    "source_path": str(source),
                    "source_name": source.name,
                    "extension": extension,
                    "sha256": source_hash,
                    "byte_size": byte_size,
                    "application_version": self.application_version,
                    "created_at": utc_iso(created),
                    "expires_at": utc_iso(expires),
                    "pending_delete": False,
                    "delete_error": None,
                    "last_delete_attempt": None,
                }
                self._atomic_json(manifest_path_in_partial, manifest)
                os.replace(partial_directory, final_directory)
            except Exception:
                self._safe_remove_partial(partial_directory)
                raise

            record = self._record_from_directory(final_directory)
            if self._hash_file(record.backup_path) != source_hash:
                # This should be impossible after the final directory rename,
                # but fail closed if storage changed under us.
                with contextlib.suppress(OSError):
                    self._delete_directory_contents(final_directory)
                raise BackupError(
                    "The committed recovery backup failed its final verification."
                )
            return record

    def _load_manifest(self, path: Path) -> dict[str, Any]:
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > MAX_MANIFEST_BYTES
        ):
            raise BackupError("A recovery manifest is missing or invalid.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BackupError("A recovery manifest could not be read.") from exc
        if not isinstance(value, dict):
            raise BackupError("A recovery manifest is not a JSON object.")
        return value

    def _record_from_directory(self, directory: Path) -> BackupRecord:
        safe_directory = self._require_contained(directory, "backup inspection")
        if (
            safe_directory.is_symlink()
            or not safe_directory.is_dir()
            or not BACKUP_ID_PATTERN.fullmatch(safe_directory.name)
        ):
            raise BackupError("A recovery backup directory is invalid.")
        manifest_path = safe_directory / MANIFEST_FILENAME
        manifest = self._load_manifest(manifest_path)
        if (
            manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
            or manifest.get("backup_id") != safe_directory.name
        ):
            raise BackupError("A recovery manifest has an unsupported schema or id.")
        backup_file = str(manifest.get("backup_file") or "")
        if (
            not backup_file
            or Path(backup_file).name != backup_file
            or "/" in backup_file
            or "\\" in backup_file
        ):
            raise BackupError("A recovery manifest contains an unsafe backup path.")
        backup_path = self._require_contained(
            safe_directory / backup_file,
            "backup inspection",
        )
        if (
            not backup_path.is_file()
            or backup_path.is_symlink()
            or backup_path.parent != safe_directory
        ):
            raise BackupError("The recovery backup file is missing or unsafe.")
        try:
            created = parse_utc(str(manifest["created_at"]))
            expires = parse_utc(str(manifest["expires_at"]))
            expected_expiry = created + RETENTION
            if expires != expected_expiry:
                raise ValueError("Expiry does not equal the immutable retention.")
            byte_size = int(manifest["byte_size"])
            sha256 = str(manifest["sha256"])
            source_path = Path(str(manifest["source_path"]))
            source_name = str(manifest["source_name"])
            extension = str(manifest["extension"]).casefold()
            application_version = str(manifest["application_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupError("A recovery manifest has invalid fields.") from exc
        if (
            byte_size < 0
            or backup_path.stat().st_size != byte_size
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or extension not in SUPPORTED_EXTENSIONS
            or Path(source_name).name != source_name
        ):
            raise BackupError("A recovery manifest does not match its backup file.")
        return BackupRecord(
            backup_id=safe_directory.name,
            backup_path=backup_path,
            manifest_path=manifest_path,
            source_path=source_path,
            source_name=source_name,
            extension=extension,
            sha256=sha256,
            byte_size=byte_size,
            application_version=application_version,
            created_at=utc_iso(created),
            expires_at=utc_iso(expires),
            pending_delete=bool(manifest.get("pending_delete")),
            delete_error=(
                str(manifest.get("delete_error"))
                if manifest.get("delete_error")
                else None
            ),
        )

    def list_records(self) -> list[BackupRecord]:
        with self.lock:
            self._ensure_root()
            records: list[BackupRecord] = []
            for directory in sorted(self.root.iterdir(), key=lambda item: item.name):
                if (
                    not directory.is_dir()
                    or not BACKUP_ID_PATTERN.fullmatch(directory.name)
                ):
                    continue
                try:
                    records.append(self._record_from_directory(directory))
                except BackupError:
                    continue
            return sorted(records, key=lambda item: item.created_at)

    def _mark_pending_delete(self, record: BackupRecord, error: Exception) -> None:
        try:
            manifest = self._load_manifest(record.manifest_path)
            manifest["pending_delete"] = True
            manifest["delete_error"] = (
                f"{type(error).__name__}: {str(error).strip() or 'file is locked'}"
            )[:800]
            manifest["last_delete_attempt"] = utc_iso(self._now())
            self._atomic_json(record.manifest_path, manifest)
        except Exception:
            # Preserve the original backup and manifest when even the retry
            # record cannot be committed.  The cleanup summary remains
            # actionable and a later run will attempt the expired item again.
            return

    def _delete_directory_contents(self, directory: Path) -> None:
        safe_directory = self._require_contained(directory, "backup deletion")
        if safe_directory.is_symlink() or not safe_directory.is_dir():
            raise BackupError("Refusing to delete an unsafe recovery directory.")
        children = list(safe_directory.iterdir())
        for child in children:
            safe_child = self._require_contained(child, "backup deletion")
            if safe_child.is_symlink() or not safe_child.is_file():
                raise BackupError(
                    "Refusing to follow an unexpected path in a recovery backup."
                )
        # Delete the data file before its manifest. A failed data-file deletion
        # leaves the manifest available for pending_delete retry state.
        children.sort(key=lambda item: item.name == MANIFEST_FILENAME)
        for child in children:
            child.unlink()
        safe_directory.rmdir()

    def cleanup_expired(self, *, reason: str = "scheduled") -> dict[str, Any]:
        with self.lock:
            self._ensure_root()
            now = self._now()
            scanned = deleted = pending = invalid = 0
            errors: list[str] = []
            for directory in sorted(self.root.iterdir(), key=lambda item: item.name):
                if (
                    not directory.is_dir()
                    or not BACKUP_ID_PATTERN.fullmatch(directory.name)
                ):
                    continue
                scanned += 1
                try:
                    record = self._record_from_directory(directory)
                    expired = now >= parse_utc(record.created_at) + RETENTION
                    if not expired and not record.pending_delete:
                        continue
                    try:
                        self._delete_directory_contents(directory)
                        deleted += 1
                    except (OSError, BackupError) as exc:
                        pending += 1
                        errors.append(
                            f"{record.backup_id}: "
                            f"{str(exc).strip() or type(exc).__name__}"
                        )
                        self._mark_pending_delete(record, exc)
                except BackupError as exc:
                    invalid += 1
                    errors.append(f"{directory.name}: {exc}")
            result = {
                "reason": reason,
                "started_at": utc_iso(now),
                "completed_at": utc_iso(self._now()),
                "scanned": scanned,
                "deleted": deleted,
                "pending_delete": pending,
                "invalid": invalid,
                "errors": errors[:20],
            }
            self.last_cleanup = result
            self.next_cleanup_at = now + CLEANUP_INTERVAL
            self._atomic_json(self.state_path, result)
            return dict(result)

    def cleanup_if_due(self) -> dict[str, Any] | None:
        with self.lock:
            now = self._now()
            if self.next_cleanup_at is not None and now < self.next_cleanup_at:
                return None
        return self.cleanup_expired(reason="scheduled")

    def _load_cleanup_state(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self.last_cleanup = value
                completed = parse_utc(str(value["completed_at"]))
                self.next_cleanup_at = completed + CLEANUP_INTERVAL
        except (OSError, ValueError, KeyError, TypeError):
            self.last_cleanup = None
            self.next_cleanup_at = None

    def summary(self) -> dict[str, Any]:
        with self.lock:
            records = self.list_records()
            return {
                "folder": str(self.root),
                "retention_days": 30,
                "count": len(records),
                "total_size": sum(item.byte_size for item in records),
                "oldest_created_at": records[0].created_at if records else None,
                "newest_created_at": records[-1].created_at if records else None,
                "pending_delete": sum(item.pending_delete for item in records),
                "last_cleanup": dict(self.last_cleanup)
                if self.last_cleanup
                else None,
            }

    def open_folder(self) -> None:
        with self.lock:
            self._ensure_root()
            if os.name != "nt":
                raise BackupError(
                    "Opening the recovery folder is available only on Windows."
                )
            try:
                self.popen(
                    ["explorer.exe", str(self.root)],
                    shell=False,
                )
            except OSError as exc:
                raise BackupError("Windows Explorer could not open the folder.") from exc

    def restore_backup(
        self,
        backup_id: str,
        destination: Path,
        *,
        replace_existing: bool = False,
    ) -> Path:
        """Copy one verified recovery item to an explicit destination."""
        with self.lock:
            if not BACKUP_ID_PATTERN.fullmatch(backup_id):
                raise BackupError("Invalid recovery backup id.")
            record = self._record_from_directory(self.root / backup_id)
            if self._hash_file(record.backup_path) != record.sha256:
                raise BackupError("The recovery backup failed SHA-256 verification.")
            requested_target = Path(destination).expanduser()
            if requested_target.is_symlink():
                raise BackupError(
                    "The recovery destination cannot be a symbolic link."
                )
            target = requested_target.resolve(strict=False)
            if target.exists() and not replace_existing:
                raise BackupError("The recovery destination already exists.")
            if target.exists() and (
                not target.is_file()
                or target.is_symlink()
            ):
                raise BackupError(
                    "The recovery destination is not a safe regular file."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
            try:
                with record.backup_path.open("rb") as input_stream, temporary.open(
                    "xb"
                ) as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                if self._hash_file(temporary) != record.sha256:
                    raise BackupError("The restored copy failed SHA-256 verification.")
                os.replace(temporary, target)
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink()
            return target

    def _safe_remove_partial(self, directory: Path) -> None:
        with contextlib.suppress(OSError, BackupError):
            safe = self._require_contained(directory, "partial cleanup")
            if safe.is_symlink() or not safe.is_dir():
                return
            self._delete_directory_contents(safe)

    def _remove_abandoned_partials(self) -> None:
        for item in list(self.root.iterdir()):
            if (
                item.is_dir()
                and item.name.startswith(".")
                and item.name.endswith(".partial")
            ):
                self._safe_remove_partial(item)

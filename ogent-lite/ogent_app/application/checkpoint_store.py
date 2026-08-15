"""Beside-document checkpoint copies under .officecli-checkpoints/<name>/."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable


CHECKPOINT_DIR_NAME = ".officecli-checkpoints"
CHECKPOINT_SOURCES = ("manual", "pre-restore")
CHECKPOINT_NAME_PATTERN = re.compile(
    r"\d{8}-\d{6}-(manual|pre-restore)-[0-9a-f]{8}\.(docx|xlsx|pptx)"
)


class CheckpointError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_directory(document: Path) -> Path:
    document = Path(document)
    return document.parent / CHECKPOINT_DIR_NAME / document.name


def create_checkpoint(document: Path, *, source: str = "manual") -> dict[str, Any]:
    """Copy the document into its checkpoint folder; verified and collision-proof."""
    if source not in CHECKPOINT_SOURCES:
        raise CheckpointError(f"Unknown checkpoint source: {source}")
    active = Path(document).resolve(strict=True)
    if not active.is_file():
        raise CheckpointError(f"Not a file: {active}")
    directory = checkpoint_directory(active)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().astimezone()
    name = (
        f"{stamp:%Y%m%d-%H%M%S}-{source}-{uuid.uuid4().hex[:8]}{active.suffix.lower()}"
    )
    target = directory / name
    payload = active.read_bytes()
    original_hash = hashlib.sha256(payload).hexdigest()
    with target.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if _sha256(target) != original_hash:
        target.unlink(missing_ok=True)
        raise CheckpointError("The checkpoint copy failed hash verification.")
    return {
        "name": name,
        "source": source,
        "created_at": stamp.isoformat(timespec="seconds"),
        "byte_size": len(payload),
        "sha256": original_hash,
    }


def list_checkpoints(document: Path) -> list[dict[str, Any]]:
    """Newest-first checkpoint listing with time and source."""
    directory = checkpoint_directory(Path(document))
    if not directory.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in directory.iterdir():
        match = CHECKPOINT_NAME_PATTERN.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        stat = path.stat()
        created = dt.datetime.strptime(
            path.name[:15], "%Y%m%d-%H%M%S"
        ).astimezone()
        entries.append(
            {
                "name": path.name,
                "source": match.group(1),
                "created_at": created.isoformat(timespec="seconds"),
                "byte_size": stat.st_size,
            }
        )
    entries.sort(key=lambda item: item["name"], reverse=True)
    return entries


def restore_checkpoint(
    document: Path,
    checkpoint_name: str,
    *,
    validate: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Restore a checkpoint atomically; the current file is checkpointed first."""
    active = Path(document).resolve(strict=True)
    if CHECKPOINT_NAME_PATTERN.fullmatch(checkpoint_name) is None:
        raise CheckpointError("Invalid checkpoint name.")
    candidate = checkpoint_directory(active) / checkpoint_name
    if not candidate.is_file():
        raise CheckpointError("That checkpoint no longer exists.")
    payload = candidate.read_bytes()
    expected_hash = hashlib.sha256(payload).hexdigest()
    # Reversibility: the current file becomes a pre-restore checkpoint first.
    safety = create_checkpoint(active, source="pre-restore")
    temporary = active.with_name(f".{active.name}.{uuid.uuid4().hex}.checkpoint")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, active)
    finally:
        temporary.unlink(missing_ok=True)
    restored_hash = _sha256(active)
    if restored_hash != expected_hash:
        raise CheckpointError("The restored document failed hash verification.")
    validation: dict[str, Any] | None = None
    if validate is not None:
        validation = dict(validate(active))
        if validation.get("accepted") is not True:
            # Roll back to the pre-restore checkpoint we just created.
            restore_checkpoint(active, safety["name"], validate=None)
            raise CheckpointError(
                "The restored package did not pass OfficeCLI validation; "
                "the previous document contents were put back."
            )
    return {
        "restored": checkpoint_name,
        "sha256": restored_hash,
        "safety_checkpoint": safety,
        "validation": validation,
    }

"""Content-addressed, atomic blob storage for lossless turn and artifact data."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class BlobRef:
    blob_id: str
    sha256: str
    byte_size: int
    media_type: str
    relative_path: str


class ContentAddressedBlobStore:
    def __init__(
        self,
        root: Path,
        *,
        capacity_guard: Callable[[int], Any] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.capacity_guard = capacity_guard

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise OSError("The Ogent blob root is not a safe directory.")

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> BlobRef:
        self.initialize()
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        relative = Path(digest[:2]) / digest[2:4] / f"{digest}.blob"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            if self.capacity_guard is not None:
                self.capacity_guard(len(payload))
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
            try:
                with temporary.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.replace(temporary, target)
                except OSError:
                    if not target.exists():
                        raise
            finally:
                temporary.unlink(missing_ok=True)
        return BlobRef(
            blob_id=digest,
            sha256=digest,
            byte_size=len(payload),
            media_type=str(media_type),
            relative_path=relative.as_posix(),
        )

    def put_text(self, text: str) -> BlobRef:
        return self.put_bytes(
            str(text).encode("utf-8"),
            media_type="text/plain; charset=utf-8",
        )

    def read_bytes(self, reference: BlobRef | str) -> bytes:
        relative = (
            Path(reference.relative_path)
            if isinstance(reference, BlobRef)
            else Path(str(reference)[:2]) / str(reference)[2:4] / f"{reference}.blob"
        )
        target = (self.root / relative).resolve(strict=True)
        target.relative_to(self.root)
        payload = target.read_bytes()
        expected = (
            reference.sha256 if isinstance(reference, BlobRef) else str(reference)
        )
        if hashlib.sha256(payload).hexdigest() != expected:
            raise OSError("An Ogent blob failed integrity verification.")
        return payload

    def read_text(self, reference: BlobRef | str) -> str:
        return self.read_bytes(reference).decode("utf-8")

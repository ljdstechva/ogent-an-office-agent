#!/usr/bin/env python3
"""Canonical retained attachment storage and per-run materialization."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from ogent_references import ReferenceAttachment


CACHE_SCHEMA_VERSION = 1
CACHE_FILENAME = "derived-cache.json"
MAX_CACHE_MANIFEST_BYTES = 64 * 1024
MAX_CACHED_IMAGES = 200


class RetainedAttachmentError(RuntimeError):
    """A safe retained-attachment storage error."""


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


class RetainedAttachmentStore:
    """Own canonical files while exposing only independent run copies."""

    def __init__(self, session_memory_root: Path, run_root: Path) -> None:
        self.session_memory_root = Path(session_memory_root).resolve(strict=False)
        self.canonical_root = self.session_memory_root / "attachments"
        self.incoming_root = self.session_memory_root / "incoming-attachments"
        self.run_root = Path(run_root).resolve(strict=False)
        self.lock = threading.RLock()
        self.canonical_root.mkdir(parents=True, exist_ok=True)
        self.incoming_root.mkdir(parents=True, exist_ok=True)

    def _require_canonical(self, path: Path, label: str) -> Path:
        candidate = path.resolve(strict=False)
        if (
            not path_is_within(candidate, self.session_memory_root)
            or candidate == self.session_memory_root
        ):
            raise RetainedAttachmentError(
                f"Refusing {label} outside session attachment storage."
            )
        return candidate

    def _require_run(self, path: Path, label: str) -> Path:
        candidate = path.resolve(strict=False)
        if not path_is_within(candidate, self.run_root) or candidate == self.run_root:
            raise RetainedAttachmentError(
                f"Refusing {label} outside materialized run storage."
            )
        return candidate

    def begin_upload(self, attachment_id: str) -> Path:
        if not _valid_id(attachment_id):
            raise RetainedAttachmentError("Invalid retained attachment id.")
        with self.lock:
            target = self._require_canonical(
                self.incoming_root / attachment_id,
                "incoming attachment creation",
            )
            target.mkdir(parents=False, exist_ok=False)
            return target

    def commit_upload(
        self,
        incoming_source: Path,
        attachment: ReferenceAttachment,
    ) -> ReferenceAttachment:
        """Atomically move a validated upload into the canonical session store."""
        with self.lock:
            incoming = self._require_canonical(
                incoming_source.parent,
                "incoming attachment commit",
            )
            if incoming.parent != self.incoming_root or incoming.is_symlink():
                raise RetainedAttachmentError(
                    "The validated upload was not in the incoming store."
                )
            if not incoming_source.is_file() or incoming_source.is_symlink():
                raise RetainedAttachmentError(
                    "The validated upload source is not a regular file."
                )
            destination = self._require_canonical(
                self.canonical_root / attachment.attachment_id,
                "canonical attachment commit",
            )
            if destination.exists():
                raise RetainedAttachmentError(
                    "A retained attachment identifier collision occurred."
                )
            os.replace(incoming, destination)
            canonical_source = destination / incoming_source.name
            return dataclasses.replace(
                attachment,
                source_path=canonical_source,
                canonical_attachment_id=attachment.attachment_id,
                available_in_session=True,
            )

    def reject_upload(self, incoming_directory: Path) -> None:
        with self.lock:
            candidate = self._require_canonical(
                incoming_directory,
                "rejected attachment cleanup",
            )
            if candidate.exists():
                self._remove_tree(candidate, self.incoming_root)

    def materialize(
        self,
        attachments: list[ReferenceAttachment],
        run_id: str,
    ) -> tuple[list[ReferenceAttachment], Path | None]:
        """Copy only this message's canonical attachments into one run bundle."""
        if not attachments:
            return [], None
        if not _valid_id(run_id):
            raise RetainedAttachmentError("Invalid materialized run id.")
        with self.lock:
            self.run_root.mkdir(parents=True, exist_ok=True)
            bundle = self._require_run(
                self.run_root / run_id,
                "run attachment materialization",
            )
            bundle.mkdir(parents=True, exist_ok=False)
            materialized: list[ReferenceAttachment] = []
            try:
                for order, attachment in enumerate(attachments):
                    canonical_id = (
                        attachment.canonical_attachment_id
                        or attachment.attachment_id
                    )
                    canonical_directory = self._require_canonical(
                        self.canonical_root / canonical_id,
                        "canonical attachment read",
                    )
                    source = self._require_canonical(
                        attachment.source_path,
                        "canonical attachment read",
                    )
                    if (
                        source.parent != canonical_directory
                        or not source.is_file()
                        or source.is_symlink()
                    ):
                        raise RetainedAttachmentError(
                            "A canonical attachment failed containment validation."
                        )
                    item_directory = bundle / f"{order:02d}-{attachment.attachment_id}"
                    item_directory.mkdir(parents=False, exist_ok=False)
                    target = item_directory / source.name
                    shutil.copy2(source, target)
                    if target.stat().st_size != attachment.byte_size:
                        raise RetainedAttachmentError(
                            "A materialized attachment copy has the wrong size."
                        )
                    materialized.append(
                        dataclasses.replace(
                            attachment,
                            source_path=target,
                            owning_run_id=run_id,
                            extracted_text_path=None,
                            image_paths=[],
                            status="Ready",
                            error_message=None,
                        )
                    )
                return materialized, bundle
            except Exception:
                with contextlib.suppress(Exception):
                    self._remove_tree(bundle, self.run_root)
                with contextlib.suppress(OSError):
                    self.run_root.rmdir()
                raise

    def cache_prepared(
        self,
        canonical: list[ReferenceAttachment],
        prepared: list[ReferenceAttachment],
    ) -> None:
        """Retain safe extracted derivatives for later-session context."""
        canonical_by_id = {
            item.canonical_attachment_id or item.attachment_id: item
            for item in canonical
        }
        with self.lock:
            for item in prepared:
                canonical_id = item.canonical_attachment_id or item.attachment_id
                source_item = canonical_by_id.get(canonical_id)
                if source_item is None:
                    continue
                canonical_directory = self._require_canonical(
                    self.canonical_root / canonical_id,
                    "derived cache creation",
                )
                cache_directory = canonical_directory / "derived"
                temporary = canonical_directory / f".derived-{uuid.uuid4().hex}.partial"
                temporary.mkdir(parents=False, exist_ok=False)
                result: dict[str, Any] = {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "extracted_text": None,
                    "images": [],
                    "ocr_or_vision": bool(item.image_paths),
                }
                try:
                    if item.extracted_text_path and item.extracted_text_path.is_file():
                        target = temporary / "extracted.txt"
                        shutil.copy2(item.extracted_text_path, target)
                        result["extracted_text"] = target.name
                    for index, image in enumerate(item.image_paths):
                        if not image.is_file():
                            continue
                        target = temporary / f"image-{index:03d}{image.suffix.casefold()}"
                        shutil.copy2(image, target)
                        result["images"].append(target.name)
                    manifest = temporary / CACHE_FILENAME
                    manifest.write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    if cache_directory.exists():
                        self._remove_tree(cache_directory, canonical_directory)
                    os.replace(temporary, cache_directory)
                finally:
                    if temporary.exists():
                        with contextlib.suppress(Exception):
                            self._remove_tree(temporary, canonical_directory)

    def _validated_cache(
        self,
        attachment: ReferenceAttachment,
    ) -> tuple[Path | None, list[Path], bool] | None:
        canonical_id = (
            attachment.canonical_attachment_id or attachment.attachment_id
        )
        try:
            canonical_directory = self._require_canonical(
                self.canonical_root / canonical_id,
                "derived cache read",
            )
            cache_directory = self._require_canonical(
                canonical_directory / "derived",
                "derived cache read",
            )
            manifest = cache_directory / CACHE_FILENAME
            if (
                cache_directory.is_symlink()
                or not cache_directory.is_dir()
                or not manifest.is_file()
                or manifest.is_symlink()
                or manifest.stat().st_size > MAX_CACHE_MANIFEST_BYTES
            ):
                return None
            value = json.loads(manifest.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != CACHE_SCHEMA_VERSION
            ):
                return None
            extracted_name = value.get("extracted_text")
            if extracted_name is not None and (
                not isinstance(extracted_name, str)
                or Path(extracted_name).name != extracted_name
            ):
                return None
            image_names = value.get("images")
            if (
                not isinstance(image_names, list)
                or len(image_names) > MAX_CACHED_IMAGES
                or any(
                    not isinstance(name, str)
                    or Path(name).name != name
                    for name in image_names
                )
            ):
                return None
            extracted = (
                self._require_canonical(
                    cache_directory / extracted_name,
                    "derived cache text read",
                )
                if extracted_name
                else None
            )
            images = [
                self._require_canonical(
                    cache_directory / name,
                    "derived cache image read",
                )
                for name in image_names
            ]
            candidates = [*([extracted] if extracted is not None else []), *images]
            if any(
                path.parent != cache_directory
                or not path.is_file()
                or path.is_symlink()
                for path in candidates
            ):
                return None
            return extracted, images, bool(value.get("ocr_or_vision"))
        except (
            OSError,
            ValueError,
            TypeError,
            RetainedAttachmentError,
        ):
            return None

    def restore_cached(
        self,
        attachment: ReferenceAttachment,
        derived_root: Path,
        *,
        require_visual: bool,
    ) -> ReferenceAttachment | None:
        """Copy a sufficient canonical derivative cache into this run."""
        with self.lock:
            cached = self._validated_cache(attachment)
            if cached is None:
                return None
            extracted, images, ocr_or_vision = cached
            needs_text = attachment.kind in {"Text", "Office", "PDF"}
            needs_images = attachment.kind == "Image" or (
                require_visual and attachment.kind in {"Office", "PDF", "Image"}
            )
            if needs_text and extracted is None:
                return None
            if needs_images and not images:
                return None
            safe_derived_root = self._require_run(
                derived_root,
                "cached run derivative creation",
            )
            if (
                safe_derived_root.is_symlink()
                or not safe_derived_root.is_dir()
            ):
                raise RetainedAttachmentError(
                    "The materialized derivative directory is unsafe."
                )
            item_root = self._require_run(
                safe_derived_root / f"cached-{attachment.attachment_id}",
                "cached run derivative creation",
            )
            try:
                item_root.mkdir(parents=False, exist_ok=False)
                restored_text: Path | None = None
                if extracted is not None:
                    restored_text = item_root / "extracted.txt"
                    shutil.copy2(extracted, restored_text)
                restored_images: list[Path] = []
                for index, image in enumerate(images):
                    target = (
                        item_root
                        / f"image-{index:03d}{image.suffix.casefold()}"
                    )
                    shutil.copy2(image, target)
                    restored_images.append(target)
                return dataclasses.replace(
                    attachment,
                    extracted_text_path=restored_text,
                    image_paths=restored_images,
                    status="Ready",
                    error_message=None,
                    ocr_or_vision=ocr_or_vision or bool(restored_images),
                )
            except Exception:
                with contextlib.suppress(Exception):
                    self._remove_tree(item_root, safe_derived_root)
                raise

    def forget(self, attachment: ReferenceAttachment) -> None:
        with self.lock:
            canonical_id = (
                attachment.canonical_attachment_id or attachment.attachment_id
            )
            directory = self._require_canonical(
                self.canonical_root / canonical_id,
                "forgotten attachment deletion",
            )
            source = self._require_canonical(
                attachment.source_path,
                "forgotten attachment source",
            )
            if source.parent != directory:
                raise RetainedAttachmentError(
                    "The forgotten attachment did not match its canonical record."
                )
            if directory.exists():
                self._remove_tree(directory, self.canonical_root)

    def stage_conversation_clear(self) -> Path:
        """Atomically detach every canonical/incoming attachment for reset."""
        with self.lock:
            if (
                self.session_memory_root.is_symlink()
                or not self.session_memory_root.is_dir()
            ):
                raise RetainedAttachmentError(
                    "The session attachment storage root is unsafe."
                )
            quarantine = self._require_canonical(
                self.session_memory_root
                / f".conversation-reset-{uuid.uuid4().hex}",
                "conversation attachment reset",
            )
            quarantine.mkdir(parents=False, exist_ok=False)
            moved: list[tuple[Path, Path]] = []
            try:
                for root in (self.canonical_root, self.incoming_root):
                    candidate = self._require_canonical(
                        root,
                        "conversation attachment reset",
                    )
                    if (
                        candidate.parent != self.session_memory_root
                        or candidate.is_symlink()
                    ):
                        raise RetainedAttachmentError(
                            "The session attachment directory is unsafe."
                        )
                    destination = quarantine / candidate.name
                    if candidate.exists():
                        os.replace(candidate, destination)
                        moved.append((destination, candidate))
                    candidate.mkdir(parents=False, exist_ok=False)
                return quarantine
            except Exception:
                for destination, original in reversed(moved):
                    if original.exists():
                        with contextlib.suppress(OSError):
                            original.rmdir()
                    if destination.exists() and not original.exists():
                        os.replace(destination, original)
                with contextlib.suppress(OSError):
                    quarantine.rmdir()
                raise

    def rollback_conversation_clear(self, quarantine: Path) -> None:
        """Restore a staged reset when the memory transaction did not commit."""
        with self.lock:
            staged = self._require_canonical(
                quarantine,
                "conversation attachment reset rollback",
            )
            if (
                staged.parent != self.session_memory_root
                or staged.is_symlink()
                or not staged.is_dir()
            ):
                raise RetainedAttachmentError(
                    "The staged attachment reset is unsafe."
                )
            for root in (self.canonical_root, self.incoming_root):
                candidate = self._require_canonical(
                    root,
                    "conversation attachment reset rollback",
                )
                restored = staged / candidate.name
                if candidate.exists():
                    if any(candidate.iterdir()):
                        raise RetainedAttachmentError(
                            "New attachment data appeared during reset rollback."
                        )
                    candidate.rmdir()
                if restored.exists():
                    os.replace(restored, candidate)
                else:
                    candidate.mkdir(parents=False, exist_ok=False)
            staged.rmdir()

    def commit_conversation_clear(self, quarantine: Path) -> None:
        """Delete only the validated reset quarantine after memory commits."""
        with self.lock:
            staged = self._require_canonical(
                quarantine,
                "conversation attachment reset commit",
            )
            if not staged.exists():
                return
            self._remove_tree(staged, self.session_memory_root)

    def cleanup_run(self, run_id: str) -> bool:
        if not _valid_id(run_id):
            raise RetainedAttachmentError("Invalid materialized run id.")
        with self.lock:
            bundle = self._require_run(
                self.run_root / run_id,
                "materialized attachment cleanup",
            )
            if not bundle.exists():
                return False
            self._remove_tree(bundle, self.run_root)
            with contextlib.suppress(OSError):
                self.run_root.rmdir()
            return True

    @staticmethod
    def _remove_tree(target: Path, expected_parent: Path) -> None:
        if target.is_symlink() or target.parent.resolve(strict=False) != expected_parent.resolve(
            strict=False
        ):
            raise RetainedAttachmentError(
                "Refusing to follow an unsafe attachment deletion path."
            )
        shutil.rmtree(target)


def _valid_id(value: str) -> bool:
    return bool(
        len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )

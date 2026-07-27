#!/usr/bin/env python3
"""Validation, preparation, and deletion helpers for temporary Ogent references.

This module deliberately has no dependency on the Ogent HTTP server.  The server
uses the validation and cleanup functions directly, while potentially expensive
Office/PDF/image preparation runs through this file as a killable child process.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import warnings
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


MAX_REFERENCE_BYTES = 50 * 1024 * 1024
MAX_REFERENCES_PER_SEND = 20
MAX_COMBINED_BYTES_PER_SEND = 100 * 1024 * 1024
MAX_SESSION_REFERENCE_COUNT = 100
MAX_SESSION_REFERENCE_BYTES = 500 * 1024 * 1024
MAX_CONCURRENT_REFERENCE_UPLOADS = 3
# Compatibility aliases for older integrations. New code and UI use the
# explicit send-scoped names above.
MAX_REFERENCES_PER_RUN = MAX_REFERENCES_PER_SEND
MAX_COMBINED_BYTES = MAX_COMBINED_BYTES_PER_SEND
MAX_PDF_PAGES = 25
MAX_IMAGE_FRAMES = 25
MAX_IMAGE_PIXELS = 40_000_000
MAX_TOTAL_IMAGE_PIXELS = 100_000_000
MAX_RENDERED_PAGE_PIXELS = 20_000_000
MAX_RENDERED_RUN_PIXELS = 100_000_000
MAX_IMAGE_DIMENSION = 10_000
NORMALIZED_IMAGE_MAX_DIMENSION = 6_000
MAX_OFFICE_ENTRIES = 10_000
MAX_OFFICE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_OFFICE_CONTENT_TYPES_BYTES = 4 * 1024 * 1024
MAX_EXTRACTED_TEXT_BYTES = 100 * 1024 * 1024
MIN_SEARCHABLE_PAGE_CHARACTERS = 40
REFERENCE_ROOT_NAME = "temporary-references"
SUPPORTED_REFERENCE_EXTENSIONS = {
    ".docx",
    ".xlsx",
    ".pptx",
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}
IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}
TEXT_EXTENSIONS = {".txt", ".md", ".csv"}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx"}

OFFICE_EXPECTATIONS = {
    ".docx": (
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".xlsx": (
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet.main+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".pptx": (
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation.main+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
}

IMAGE_FORMATS_BY_EXTENSION = {
    ".png": {"PNG"},
    ".jpg": {"JPEG"},
    ".jpeg": {"JPEG"},
    ".webp": {"WEBP"},
    ".bmp": {"BMP"},
    ".tif": {"TIFF"},
    ".tiff": {"TIFF"},
}

VISUAL_REQUEST_PATTERN = re.compile(
    r"\b("
    r"layout|visual|diagram|drawing|signature|stamp|photograph|photo|image|"
    r"figure|chart|graph|map|plan|blueprint|schematic|appearance|formatting|"
    r"design|slide|screenshot|handwrit(?:ing|ten)|ocr"
    r")\b",
    re.IGNORECASE,
)


class ReferenceError(RuntimeError):
    """An actionable reference error safe to show in the local UI."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ReferenceInspection:
    kind: str
    detected_type: str
    page_count: int | None = None
    frame_count: int | None = None


@dataclass
class ReferenceAttachment:
    attachment_id: str
    original_name: str
    source_path: Path
    detected_type: str
    kind: str
    byte_size: int
    uploaded_at: str
    status: str = "Ready"
    extracted_text_path: Path | None = None
    image_paths: list[Path] = field(default_factory=list)
    owning_run_id: str | None = None
    error_message: str | None = None
    page_count: int | None = None
    frame_count: int | None = None
    ocr_or_vision: bool = False
    available_in_session: bool = True
    sent_sequence: int | None = None
    canonical_attachment_id: str | None = None

    def public_metadata(self) -> dict[str, Any]:
        """Return browser-safe metadata.  Filesystem paths are intentionally absent."""
        return {
            "id": self.attachment_id,
            "filename": self.original_name,
            "size": self.byte_size,
            "kind": self.kind,
            "detected_type": self.detected_type,
            "status": self.status,
            "error": self.error_message,
            "ocr_or_vision": self.ocr_or_vision,
            "available_in_session": self.available_in_session,
            "pending": self.sent_sequence is None and self.owning_run_id is None,
        }

    def preparation_manifest_item(self) -> dict[str, Any]:
        return {
            "id": self.attachment_id,
            "filename": self.original_name,
            "source_path": str(self.source_path),
            "detected_type": self.detected_type,
            "kind": self.kind,
            "size": self.byte_size,
            "page_count": self.page_count,
            "frame_count": self.frame_count,
        }


def sanitize_reference_filename(value: str) -> str:
    """Return a safe display/storage leaf while retaining useful Unicode."""
    normalized = unicodedata.normalize("NFKC", value or "")
    leaf = normalized.replace("\\", "/").rsplit("/", 1)[-1].strip()
    suffix = Path(leaf).suffix.casefold()
    if suffix not in SUPPORTED_REFERENCE_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_REFERENCE_EXTENSIONS))
        raise ReferenceError(
            f"Unsupported reference type. Choose one of: {supported}.",
            415,
        )
    stem = leaf[: -len(suffix)] if suffix else leaf
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "-", stem).strip(" .")
    if not stem:
        stem = "reference"
    if stem.casefold() in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }:
        stem = f"_{stem}"
    maximum_stem = max(1, 180 - len(suffix))
    return f"{stem[:maximum_stem]}{suffix}"


def _load_pdfium() -> Any:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ReferenceError(
            "PDF references require pypdfium2. Run "
            "`py -3 -m pip install -r ogent-lite\\requirements.txt`, then restart Ogent.",
            500,
        ) from exc
    return pdfium


def _load_pillow() -> tuple[Any, Any]:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ReferenceError(
            "Image references require Pillow. Run "
            "`py -3 -m pip install -r ogent-lite\\requirements.txt`, then restart Ogent.",
            500,
        ) from exc
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    return Image, ImageOps


def decode_text_file(path: Path) -> str:
    data = path.read_bytes()
    if not data:
        raise ReferenceError("The reference file is empty.")
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        if b"\x00" in data:
            raise ReferenceError(
                "This text reference appears to be binary. Save it as UTF-8 text."
            )
        encoding = "utf-8"
    try:
        text = data.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise ReferenceError(
            "Text references must be UTF-8, or UTF-16 with a byte-order mark."
        ) from exc
    controls = sum(
        1
        for character in text
        if ord(character) < 32 and character not in "\t\r\n\f"
    )
    if controls > max(8, len(text) // 200):
        raise ReferenceError(
            "This text reference contains unsupported binary control characters."
        )
    return text


def _validate_office(path: Path, suffix: str) -> ReferenceInspection:
    try:
        with path.open("rb") as stream:
            if stream.read(4) not in {b"PK\x03\x04", b"PK\x05\x06"}:
                raise ReferenceError(
                    "Modern Office references must begin with a valid ZIP signature."
                )
    except OSError as exc:
        raise ReferenceError(f"Could not read the Office reference: {exc}") from exc
    if not zipfile.is_zipfile(path):
        raise ReferenceError(
            f"{suffix.upper()[1:]} references must be valid modern Office ZIP packages."
        )
    expected_main, expected_content_type, detected_type = OFFICE_EXPECTATIONS[suffix]
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_OFFICE_ENTRIES:
                raise ReferenceError(
                    "The Office reference has an invalid or excessive package structure."
                )
            total_uncompressed = 0
            names: set[str] = set()
            for info in infos:
                if info.flag_bits & 0x1:
                    raise ReferenceError("Encrypted Office references are not supported.")
                pure_name = PurePosixPath(info.filename.replace("\\", "/"))
                if (
                    pure_name.is_absolute()
                    or ".." in pure_name.parts
                    or not pure_name.parts
                ):
                    raise ReferenceError(
                        "The Office reference contains an unsafe package path."
                    )
                normalized_name = str(pure_name).casefold()
                if normalized_name in names:
                    raise ReferenceError(
                        "The Office reference contains duplicate package members."
                    )
                names.add(normalized_name)
                dangerous_suffixes = {
                    ".exe",
                    ".dll",
                    ".com",
                    ".bat",
                    ".cmd",
                    ".ps1",
                    ".vbs",
                    ".js",
                    ".jse",
                    ".scr",
                    ".msi",
                    ".jar",
                    ".hta",
                }
                member_suffix = PurePosixPath(normalized_name).suffix
                if (
                    member_suffix in dangerous_suffixes
                    or "vbaproject.bin" in normalized_name
                    or "/activex/" in f"/{normalized_name}"
                    or "/embeddings/" in f"/{normalized_name}"
                ):
                    raise ReferenceError(
                        "The Office reference contains executable, macro, ActiveX, "
                        "or embedded binary content that Ogent will not inspect."
                    )
                total_uncompressed += max(0, info.file_size)
                if total_uncompressed > MAX_OFFICE_UNCOMPRESSED_BYTES:
                    raise ReferenceError(
                        "The Office reference expands beyond Ogent's safe inspection limit."
                    )
                if (
                    info.compress_size > 0
                    and info.file_size > 32 * 1024 * 1024
                    and info.file_size / info.compress_size > 250
                ):
                    raise ReferenceError(
                        "The Office reference has a suspicious compression ratio."
                    )
            required = {
                "[content_types].xml",
                "_rels/.rels",
                expected_main.casefold(),
            }
            if not required.issubset(names):
                raise ReferenceError(
                    f"The file extension does not match a valid {suffix.upper()[1:]} package."
                )
            try:
                content_types_info = archive.getinfo("[Content_Types].xml")
                if content_types_info.file_size > MAX_OFFICE_CONTENT_TYPES_BYTES:
                    raise ReferenceError(
                        "The Office reference has an excessively large content-type "
                        "manifest."
                    )
                content_types = archive.read("[Content_Types].xml")
            except KeyError as exc:
                raise ReferenceError(
                    "The Office reference is missing [Content_Types].xml."
                ) from exc
            if expected_content_type.encode("ascii") not in content_types:
                raise ReferenceError(
                    f"The file contents do not match the {suffix.upper()[1:]} extension."
                )
            bad_member = archive.testzip()
            if bad_member:
                raise ReferenceError(
                    "The Office reference is malformed or truncated."
                )
    except ReferenceError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReferenceError("The Office reference is malformed or truncated.") from exc
    return ReferenceInspection(
        kind="Office",
        detected_type=detected_type,
    )


def _validate_pdf(path: Path) -> ReferenceInspection:
    try:
        with path.open("rb") as stream:
            signature = stream.read(8)
    except OSError as exc:
        raise ReferenceError(f"Could not read the PDF reference: {exc}") from exc
    if not re.match(rb"^%PDF-\d\.\d", signature):
        raise ReferenceError(
            "The file extension does not match a PDF with a valid %PDF signature."
        )
    pdfium = _load_pdfium()
    try:
        with pdfium.PdfDocument(path) as pdf:
            page_count = len(pdf)
    except Exception as exc:
        raise ReferenceError(
            "The PDF reference is malformed, truncated, encrypted, or unreadable."
        ) from exc
    if page_count <= 0:
        raise ReferenceError("The PDF reference contains no pages.")
    if page_count > MAX_PDF_PAGES:
        raise ReferenceError(
            f"The PDF has {page_count} pages; the limit is {MAX_PDF_PAGES}. "
            "Split it into smaller references and attach only the needed pages.",
            413,
        )
    return ReferenceInspection(
        kind="PDF",
        detected_type="application/pdf",
        page_count=page_count,
    )


def _validate_image(path: Path, suffix: str) -> ReferenceInspection:
    Image, _ = _load_pillow()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as verifier:
                detected_format = str(verifier.format or "").upper()
                verifier.verify()
            if detected_format not in IMAGE_FORMATS_BY_EXTENSION[suffix]:
                raise ReferenceError(
                    "The image contents do not match the filename extension."
                )
            with Image.open(path) as image:
                frame_count = int(getattr(image, "n_frames", 1))
                if frame_count <= 0 or frame_count > MAX_IMAGE_FRAMES:
                    raise ReferenceError(
                        f"The image contains {frame_count} frames; the limit is "
                        f"{MAX_IMAGE_FRAMES}. Export only the needed frames."
                    )
                total_pixels = 0
                for frame_index in range(frame_count):
                    image.seek(frame_index)
                    width, height = image.size
                    if (
                        width <= 0
                        or height <= 0
                        or width > MAX_IMAGE_DIMENSION
                        or height > MAX_IMAGE_DIMENSION
                        or width * height > MAX_IMAGE_PIXELS
                    ):
                        raise ReferenceError(
                            "The image dimensions exceed Ogent's safe 40-megapixel "
                            "and 10,000-pixel-per-side limits."
                        )
                    total_pixels += width * height
                    if total_pixels > MAX_TOTAL_IMAGE_PIXELS:
                        raise ReferenceError(
                            "The image frames exceed Ogent's combined pixel limit."
                        )
                    image.load()
    except ReferenceError:
        raise
    except Exception as exc:
        raise ReferenceError(
            "The image is malformed, truncated, or cannot be decoded safely."
        ) from exc
    mime = {
        "JPEG": "image/jpeg",
        "TIFF": "image/tiff",
    }.get(detected_format, f"image/{detected_format.casefold()}")
    return ReferenceInspection(
        kind="Image",
        detected_type=mime,
        frame_count=frame_count,
    )


def validate_reference_file(path: Path, original_name: str) -> ReferenceInspection:
    """Validate extension, signature/structure, size, and safe decodability."""
    sanitized = sanitize_reference_filename(original_name)
    suffix = Path(sanitized).suffix.casefold()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReferenceError(f"Could not inspect the reference file: {exc}") from exc
    if size <= 0:
        raise ReferenceError("The reference file is empty.")
    if size > MAX_REFERENCE_BYTES:
        raise ReferenceError(
            f"The reference exceeds the {MAX_REFERENCE_BYTES // (1024 * 1024)} MB "
            "per-file limit.",
            413,
        )
    if suffix in OFFICE_EXTENSIONS:
        return _validate_office(path, suffix)
    if suffix == ".pdf":
        return _validate_pdf(path)
    if suffix in IMAGE_EXTENSIONS:
        return _validate_image(path, suffix)
    if suffix in TEXT_EXTENSIONS:
        text = decode_text_file(path)
        if not text:
            raise ReferenceError("The text reference contains no readable text.")
        detected_type = {
            ".txt": "text/plain; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".csv": "text/csv; charset=utf-8",
        }[suffix]
        return ReferenceInspection(kind="Text", detected_type=detected_type)
    raise ReferenceError("Unsupported reference type.", 415)


def visual_analysis_requested(message: str) -> bool:
    return bool(VISUAL_REQUEST_PATTERN.search(message or ""))


def _guard_reference_root(root: Path) -> Path:
    root = Path(root)
    if root.name != REFERENCE_ROOT_NAME:
        raise ReferenceError(
            f"Refusing cleanup: reference root must be named {REFERENCE_ROOT_NAME}.",
            500,
        )
    parent = root.parent.resolve()
    unresolved = parent / root.name
    if root.exists() and root.is_symlink():
        raise ReferenceError("Refusing cleanup: reference root is a symbolic link.", 500)
    resolved = root.resolve(strict=False)
    if resolved != unresolved:
        raise ReferenceError(
            "Refusing cleanup: reference root does not resolve to its expected location.",
            500,
        )
    if resolved == Path(resolved.anchor) or resolved.parent == resolved:
        raise ReferenceError("Refusing cleanup of a filesystem root.", 500)
    return resolved


def reference_path_is_within(path: Path, root: Path) -> bool:
    try:
        guarded_root = _guard_reference_root(root)
        Path(path).resolve(strict=False).relative_to(guarded_root)
        return True
    except (ReferenceError, ValueError, OSError):
        return False


def _make_tree_writable(path: Path) -> None:
    if not path.exists():
        return
    for current_root, directories, filenames in os.walk(path, topdown=False):
        for name in filenames:
            with contextlib.suppress(OSError):
                os.chmod(Path(current_root) / name, 0o600)
        for name in directories:
            with contextlib.suppress(OSError):
                os.chmod(Path(current_root) / name, 0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def _prune_empty_parents(start: Path, root: Path) -> None:
    guarded_root = _guard_reference_root(root)
    current = start.resolve(strict=False)
    while current != guarded_root and reference_path_is_within(current, guarded_root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def cleanup_reference_path(path: Path, root: Path) -> bool:
    """Idempotently delete one contained reference file/tree and empty parents."""
    guarded_root = _guard_reference_root(root)
    target = Path(path).resolve(strict=False)
    if target == guarded_root:
        raise ReferenceError(
            "Use reset_reference_root() for whole-root cleanup.",
            500,
        )
    try:
        target.relative_to(guarded_root)
    except ValueError as exc:
        raise ReferenceError(
            "Refusing to delete a path outside the temporary reference root.",
            500,
        ) from exc
    if not target.exists() and not target.is_symlink():
        _prune_empty_parents(target.parent, guarded_root)
        return False
    _make_tree_writable(target)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()
    _prune_empty_parents(target.parent, guarded_root)
    return True


def reset_reference_root(root: Path) -> Path:
    """Safely remove abandoned data and recreate an empty reference root."""
    guarded_root = _guard_reference_root(root)
    if guarded_root.exists():
        _make_tree_writable(guarded_root)
        shutil.rmtree(guarded_root)
    guarded_root.mkdir(parents=True, exist_ok=False)
    return guarded_root


def _require_contained(path: Path, run_root: Path, label: str) -> Path:
    resolved_root = run_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ReferenceError(f"{label} escapes the temporary run directory.", 500) from exc
    return resolved_path


def _write_extraction(path: Path, text: str) -> None:
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_EXTRACTED_TEXT_BYTES:
        raise ReferenceError(
            "The extracted reference text exceeds Ogent's 100 MB preparation limit. "
            "Attach a smaller source or only the relevant portion.",
            413,
        )
    path.write_bytes(encoded)


def _prepare_text(item: dict[str, Any], source: Path, derived: Path) -> dict[str, Any]:
    text = decode_text_file(source)
    extracted = derived / "extracted.txt"
    _write_extraction(
        extracted,
        f"Reference: {item['filename']}\nLocation: whole file\n\n{text}",
    )
    return {
        "extracted_text_path": str(extracted),
        "image_paths": [],
        "ocr_expected": False,
        "status": "Ready",
    }


def _prepare_office_text(
    item: dict[str, Any],
    source: Path,
    derived: Path,
) -> Path:
    raw_output = derived / "officecli-view.txt"
    environment = os.environ.copy()
    environment["OFFICECLI_NO_AUTO_RESIDENT"] = "1"
    environment["OFFICECLI_RESIDENT_FLUSH"] = "each"
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        with raw_output.open("wb") as output:
            process = subprocess.run(
                ["officecli", "view", str(source), "text"],
                cwd=str(source.parent),
                env=environment,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=120,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
                check=False,
            )
    except FileNotFoundError as exc:
        raise ReferenceError(
            "OfficeCLI is required to inspect Office references.",
            500,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ReferenceError("OfficeCLI reference inspection timed out.") from exc
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise ReferenceError(
            f"OfficeCLI could not read this Office reference. {detail[-800:]}".strip()
        )
    if raw_output.stat().st_size > MAX_EXTRACTED_TEXT_BYTES:
        raise ReferenceError(
            "The Office reference extraction exceeds Ogent's 100 MB limit."
        )
    raw_text = raw_output.read_text(encoding="utf-8", errors="replace")
    extracted = derived / "extracted.txt"
    _write_extraction(
        extracted,
        f"Reference: {item['filename']}\n"
        f"Office type: {source.suffix.upper()[1:]}\n"
        "Locations below retain OfficeCLI's paragraph, cell, sheet, and slide labels "
        "when available.\n\n"
        f"{raw_text}",
    )
    raw_output.unlink()
    return extracted


def _render_pdf_pages(
    pdf_path: Path,
    output_directory: Path,
    page_indexes: list[int],
    *,
    name_prefix: str,
) -> list[Path]:
    if not page_indexes:
        return []
    pdfium = _load_pdfium()
    Image, _ = _load_pillow()
    image_paths: list[Path] = []
    rendered_pixels = 0
    with pdfium.PdfDocument(pdf_path) as pdf:
        page_count = len(pdf)
        for page_index in page_indexes:
            if page_index < 0 or page_index >= page_count:
                raise ReferenceError("A requested render page is outside the PDF.")
            page = pdf[page_index]
            try:
                width = max(float(page.get_width()), 1.0)
                height = max(float(page.get_height()), 1.0)
                maximum_scale = min(
                    4096.0 / max(width, height),
                    (MAX_RENDERED_PAGE_PIXELS / (width * height)) ** 0.5,
                )
                requested_scale = max(
                    2.5,
                    256.0 / min(width, height),
                )
                scale = min(requested_scale, maximum_scale)
                if scale <= 0 or width * scale < 256 or height * scale < 256:
                    raise ReferenceError(
                        f"PDF page {page_index + 1} is too large to render "
                        "legibly within Ogent's pixel limit."
                    )
                page_pixels = int(width * scale) * int(height * scale)
                rendered_pixels += page_pixels
                if rendered_pixels > MAX_RENDERED_RUN_PIXELS:
                    raise ReferenceError(
                        "Rendered reference pages exceed Ogent's 100-megapixel "
                        "per-run limit. Attach fewer visual pages."
                    )
                bitmap = page.render(scale=scale)
                try:
                    rendered = bitmap.to_pil().copy()
                finally:
                    bitmap.close()
                if rendered.mode not in {"RGB", "RGBA"}:
                    rendered = rendered.convert("RGB")
                target = output_directory / f"{name_prefix}-{page_index + 1:03d}.png"
                rendered.save(target, format="PNG", optimize=True)
                rendered.close()
                with Image.open(target) as check:
                    check.verify()
                image_paths.append(target)
            finally:
                page.close()
    return image_paths


def _prepare_pdf(
    item: dict[str, Any],
    source: Path,
    derived: Path,
    *,
    visual_requested: bool,
) -> dict[str, Any]:
    pdfium = _load_pdfium()
    page_sections: list[str] = []
    render_indexes: list[int] = []
    with pdfium.PdfDocument(source) as pdf:
        page_count = len(pdf)
        if page_count > MAX_PDF_PAGES:
            raise ReferenceError(
                f"The PDF now has {page_count} pages; the limit is {MAX_PDF_PAGES}."
            )
        for page_index in range(page_count):
            page = pdf[page_index]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded()
                finally:
                    textpage.close()
            finally:
                page.close()
            normalized = text.replace("\x00", "").strip()
            page_sections.append(
                f"=== Page {page_index + 1} ===\n"
                f"{normalized if normalized else '[No searchable text detected]'}"
            )
            searchable_characters = len(re.sub(r"\s+", "", normalized))
            if visual_requested or searchable_characters < MIN_SEARCHABLE_PAGE_CHARACTERS:
                render_indexes.append(page_index)
    extracted = derived / "extracted.txt"
    _write_extraction(
        extracted,
        f"Reference: {item['filename']}\n"
        f"PDF pages: {len(page_sections)}\n\n"
        + "\n\n".join(page_sections),
    )
    images = _render_pdf_pages(
        source,
        derived,
        render_indexes,
        name_prefix="pdf-page",
    )
    return {
        "extracted_text_path": str(extracted),
        "image_paths": [str(path) for path in images],
        "ocr_expected": bool(images),
        "status": "OCR/vision" if images else "Ready",
    }


def _export_office_pdf(
    source: Path,
    derived: Path,
    helper_path: Path,
) -> Path:
    output = derived / "office-visual.pdf"
    pid_file = derived / ".office-process.json"
    process = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper_path),
            "-InputFile",
            str(source),
            "-OutPdf",
            str(output),
            "-PidFile",
            str(pid_file),
        ],
        cwd=str(derived),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0
        ),
        check=False,
    )
    if process.returncode != 0 or not output.is_file():
        detail = (
            process.stderr.decode("utf-8", errors="replace")
            or process.stdout.decode("utf-8", errors="replace")
        ).strip()
        raise ReferenceError(
            f"Office visual export failed. {detail[-1000:]}".strip()
        )
    inspection = _validate_pdf(output)
    if inspection.page_count and inspection.page_count > MAX_PDF_PAGES:
        raise ReferenceError(
            f"Office visual export produced {inspection.page_count} pages; "
            f"the visual-analysis limit is {MAX_PDF_PAGES}. "
            "Attach a smaller document or request text-only analysis."
        )
    return output


def _prepare_office(
    item: dict[str, Any],
    source: Path,
    derived: Path,
    *,
    visual_requested: bool,
    office_visual_helper: Path,
) -> dict[str, Any]:
    extracted = _prepare_office_text(item, source, derived)
    needs_visual = visual_requested or source.suffix.casefold() == ".pptx"
    images: list[Path] = []
    if needs_visual:
        exported_pdf = _export_office_pdf(source, derived, office_visual_helper)
        pdfium = _load_pdfium()
        with pdfium.PdfDocument(exported_pdf) as pdf:
            page_indexes = list(range(len(pdf)))
        images = _render_pdf_pages(
            exported_pdf,
            derived,
            page_indexes,
            name_prefix=(
                "slide" if source.suffix.casefold() == ".pptx" else "office-page"
            ),
        )
    return {
        "extracted_text_path": str(extracted),
        "image_paths": [str(path) for path in images],
        "ocr_expected": bool(images),
        "status": "OCR/vision" if images else "Ready",
    }


def _prepare_image(
    item: dict[str, Any],
    source: Path,
    derived: Path,
) -> dict[str, Any]:
    Image, ImageOps = _load_pillow()
    image_paths: list[str] = []
    with Image.open(source) as image:
        frame_count = int(getattr(image, "n_frames", 1))
        for frame_index in range(frame_count):
            image.seek(frame_index)
            normalized = ImageOps.exif_transpose(image.copy())
            if normalized.mode not in {"RGB", "RGBA"}:
                normalized = normalized.convert("RGBA" if "A" in normalized.getbands() else "RGB")
            normalized.thumbnail(
                (NORMALIZED_IMAGE_MAX_DIMENSION, NORMALIZED_IMAGE_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            target = derived / f"image-{frame_index + 1:03d}.png"
            normalized.save(target, format="PNG", optimize=True)
            normalized.close()
            image_paths.append(str(target))
    return {
        "extracted_text_path": None,
        "image_paths": image_paths,
        "ocr_expected": True,
        "status": "OCR/vision",
    }


def prepare_manifest(manifest_path: Path, result_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ReferenceError("Invalid reference preparation manifest.", 500)
    run_root = Path(str(manifest.get("run_root", ""))).resolve(strict=True)
    _require_contained(manifest_path, run_root, "Preparation manifest")
    _require_contained(result_path.parent, run_root, "Preparation result directory")
    office_visual_helper = Path(
        str(manifest.get("office_visual_helper", ""))
    ).resolve(strict=True)
    visual_requested = bool(manifest.get("visual_requested"))
    raw_items = manifest.get("references")
    if not isinstance(raw_items, list) or not raw_items:
        raise ReferenceError("The preparation manifest contains no references.", 500)

    results: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise ReferenceError("Invalid reference manifest item.", 500)
        source = _require_contained(
            Path(str(item.get("source_path", ""))),
            run_root,
            "Reference source",
        )
        expected_name = sanitize_reference_filename(str(item.get("filename", "")))
        inspection = validate_reference_file(source, expected_name)
        if inspection.kind != item.get("kind"):
            raise ReferenceError(
                f"{expected_name} changed type after upload; preparation was refused."
            )
        derived = source.parent / "derived"
        derived.mkdir(parents=False, exist_ok=False)
        if inspection.kind == "Text":
            prepared = _prepare_text(item, source, derived)
        elif inspection.kind == "PDF":
            prepared = _prepare_pdf(
                item,
                source,
                derived,
                visual_requested=visual_requested,
            )
        elif inspection.kind == "Office":
            prepared = _prepare_office(
                item,
                source,
                derived,
                visual_requested=visual_requested,
                office_visual_helper=office_visual_helper,
            )
        elif inspection.kind == "Image":
            prepared = _prepare_image(item, source, derived)
        else:
            raise ReferenceError("Unsupported detected reference type.")
        results.append(
            {
                "id": str(item.get("id", "")),
                "filename": expected_name,
                **prepared,
            }
        )
    temporary = result_path.with_suffix(result_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"references": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, result_path)


def inspect_reference(
    source_path: Path,
    original_name: str,
    result_path: Path,
) -> None:
    source = source_path.resolve(strict=True)
    result_parent = result_path.parent.resolve(strict=True)
    if source.parent != result_parent:
        raise ReferenceError(
            "Inspection output must stay beside the temporary upload.",
            500,
        )
    inspection = validate_reference_file(source, original_name)
    payload = {
        "kind": inspection.kind,
        "detected_type": inspection.detected_type,
        "page_count": inspection.page_count,
        "frame_count": inspection.frame_count,
    }
    temporary = result_path.with_suffix(result_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, result_path)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Ogent temporary references")
    subcommands = parser.add_subparsers(dest="command", required=True)
    prepare = subcommands.add_parser("prepare")
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--result", required=True)
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("--source", required=True)
    inspect.add_argument("--filename", required=True)
    inspect.add_argument("--result", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare_manifest(Path(args.manifest), Path(args.result))
        elif args.command == "inspect":
            inspect_reference(
                Path(args.source),
                str(args.filename),
                Path(args.result),
            )
        return 0
    except ReferenceError as exc:
        if args.command == "inspect":
            with contextlib.suppress(OSError):
                Path(args.result).write_text(
                    json.dumps(
                        {"error": str(exc), "status": exc.status},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Reference preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())

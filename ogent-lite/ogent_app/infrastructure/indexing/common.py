"""Safe OOXML reading and deterministic index helpers."""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from ogent_app.domain.document_intelligence import (
    IndexedNode,
    LocatorNamespace,
    LocatorStability,
    NodeKind,
    StructuralLocator,
)


@dataclass(frozen=True, slots=True)
class PackageLimits:
    max_entries: int = 10_000
    max_uncompressed_bytes: int = 512_000_000
    max_part_bytes: int = 96_000_000
    max_compression_ratio: float = 250.0
    max_xml_elements: int = 2_000_000
    max_xml_depth: int = 256


DEFAULT_PACKAGE_LIMITS = PackageLimits()

RELATIONSHIP_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_RELATIONSHIP_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)


class DocumentIndexError(RuntimeError):
    """Raised when a document package cannot be indexed safely."""


def package_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_sha256(
    *,
    kind: NodeKind,
    title: str | None,
    text: str,
    metadata: dict[str, Any],
) -> str:
    payload = json.dumps(
        {
            "kind": kind.value,
            "title": title,
            "text": text,
            "metadata": metadata,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def indexed_node(
    stable_path: str,
    kind: NodeKind,
    *,
    parent_path: str | None = None,
    title: str | None = None,
    text: str = "",
    metadata: dict[str, Any] | None = None,
    sheet_name: str | None = None,
    slide_number: int | None = None,
    page_number: int | None = None,
    ordinal: int = 0,
    native_key: str | None = None,
    stability: LocatorStability = LocatorStability.REVISION_SCOPED,
    lineage_key: str | None = None,
    source_paths: tuple[str, ...] = (),
    namespace: LocatorNamespace = LocatorNamespace.INTERNAL,
    resolvable: bool = False,
) -> IndexedNode:
    safe_metadata = json.loads(
        json.dumps(metadata or {}, ensure_ascii=False, default=str)
    )
    return IndexedNode(
        locator=StructuralLocator(
            stable_path=stable_path,
            native_key=native_key,
            stability=stability,
            lineage_key=lineage_key or native_key,
            source_paths=tuple(source_paths),
            namespace=namespace,
            resolvable=bool(resolvable),
        ),
        kind=kind,
        parent_path=parent_path,
        title=title,
        text=text,
        metadata=safe_metadata,
        sheet_name=sheet_name,
        slide_number=slide_number,
        page_number=page_number,
        ordinal=ordinal,
        content_sha256=content_sha256(
            kind=kind,
            title=title,
            text=text,
            metadata=safe_metadata,
        ),
    )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalized_text(element: ET.Element) -> str:
    text = " ".join(
        value.strip() for value in element.itertext() if value and value.strip()
    )
    return re.sub(r"\s+", " ", text).strip()


def attribute_by_local_name(
    element: ET.Element,
    name: str,
    default: str | None = None,
) -> str | None:
    for key, value in element.attrib.items():
        if local_name(key) == name:
            return value
    return default


class OoxmlPackage:
    """Bounded, traversal-safe reader for an OOXML ZIP package."""

    def __init__(
        self,
        path: Path,
        *,
        limits: PackageLimits = DEFAULT_PACKAGE_LIMITS,
    ) -> None:
        self.path = Path(path).expanduser().resolve(strict=True)
        self.limits = limits
        try:
            self.archive = zipfile.ZipFile(self.path, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise DocumentIndexError("The Office package is not a valid ZIP.") from exc
        self._validate_directory()

    def __enter__(self) -> "OoxmlPackage":
        return self

    def __exit__(self, *_args: object) -> None:
        self.archive.close()

    def _validate_directory(self) -> None:
        entries = self.archive.infolist()
        if len(entries) > self.limits.max_entries:
            self.archive.close()
            raise DocumentIndexError("The Office package contains too many parts.")
        total = 0
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or name.startswith("/")
                or "\x00" in name
            ):
                self.archive.close()
                raise DocumentIndexError(
                    "The Office package contains an unsafe part path."
                )
            unix_mode = (entry.external_attr >> 16) & 0xF000
            if unix_mode == 0xA000:
                self.archive.close()
                raise DocumentIndexError(
                    "The Office package contains a symbolic-link part."
                )
            if entry.flag_bits & 0x1:
                self.archive.close()
                raise DocumentIndexError("Encrypted package parts cannot be indexed.")
            if (
                entry.file_size > 0
                and entry.file_size / max(1, entry.compress_size)
                > self.limits.max_compression_ratio
            ):
                self.archive.close()
                raise DocumentIndexError(
                    "The Office package contains a suspicious compression ratio."
                )
            total += max(0, int(entry.file_size))
            if total > self.limits.max_uncompressed_bytes:
                self.archive.close()
                raise DocumentIndexError(
                    "The Office package exceeds the indexing size limit."
                )

    def names(self, prefix: str = "") -> tuple[str, ...]:
        normalized = prefix.replace("\\", "/")
        return tuple(
            name for name in self.archive.namelist() if name.startswith(normalized)
        )

    def exists(self, name: str) -> bool:
        return name.replace("\\", "/") in self.archive.namelist()

    def read(self, name: str, *, max_bytes: int | None = None) -> bytes:
        normalized = name.replace("\\", "/").lstrip("/")
        limit = self.limits.max_part_bytes if max_bytes is None else max_bytes
        try:
            info = self.archive.getinfo(normalized)
        except KeyError as exc:
            raise DocumentIndexError(
                f"Required package part is missing: {normalized}"
            ) from exc
        if info.file_size > limit:
            raise DocumentIndexError(
                f"Package part exceeds the indexing limit: {normalized}"
            )
        payload = self.archive.read(info)
        if len(payload) > limit:
            raise DocumentIndexError(
                f"Package part exceeds the indexing limit: {normalized}"
            )
        return payload

    def xml(self, name: str) -> ET.Element:
        payload = self.read(name)
        lowered = payload[:4096].lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise DocumentIndexError(
                f"Package XML contains a forbidden declaration: {name}"
            )
        try:
            depth = 0
            element_count = 0
            root: ET.Element | None = None
            for event, element in ET.iterparse(
                io.BytesIO(payload),
                events=("start", "end"),
            ):
                if event == "start":
                    depth += 1
                    element_count += 1
                    if root is None:
                        root = element
                    if depth > self.limits.max_xml_depth:
                        raise DocumentIndexError(
                            f"Package XML nesting is too deep: {name}"
                        )
                    if element_count > self.limits.max_xml_elements:
                        raise DocumentIndexError(
                            f"Package XML contains too many elements: {name}"
                        )
                else:
                    depth -= 1
            if root is None:
                raise DocumentIndexError(f"Package XML is empty: {name}")
            return root
        except ET.ParseError as exc:
            raise DocumentIndexError(f"Package XML is malformed: {name}") from exc

    def relationships(self, source_part: str) -> dict[str, tuple[str, str]]:
        source = source_part.replace("\\", "/").lstrip("/")
        folder, filename = posixpath.split(source)
        rels_name = posixpath.join(folder, "_rels", f"{filename}.rels")
        if not self.exists(rels_name):
            return {}
        root = self.xml(rels_name)
        result: dict[str, tuple[str, str]] = {}
        for relationship in root:
            if local_name(relationship.tag) != "Relationship":
                continue
            identifier = relationship.attrib.get("Id")
            target = relationship.attrib.get("Target")
            rel_type = relationship.attrib.get("Type", "")
            target_mode = relationship.attrib.get("TargetMode", "Internal")
            if not identifier or not target or target_mode.casefold() == "external":
                continue
            resolved = resolve_relationship_target(source, target)
            result[identifier] = (resolved, rel_type)
        return result

    def external_relationships(
        self,
        source_part: str,
    ) -> tuple[dict[str, str], ...]:
        source = source_part.replace("\\", "/").lstrip("/")
        folder, filename = posixpath.split(source)
        rels_name = posixpath.join(folder, "_rels", f"{filename}.rels")
        if not self.exists(rels_name):
            return ()
        root = self.xml(rels_name)
        result: list[dict[str, str]] = []
        for relationship in root:
            if (
                local_name(relationship.tag) == "Relationship"
                and relationship.attrib.get(
                    "TargetMode",
                    "Internal",
                ).casefold()
                == "external"
            ):
                result.append(
                    {
                        "id": relationship.attrib.get("Id", ""),
                        "target": relationship.attrib.get("Target", ""),
                        "type": relationship.attrib.get("Type", ""),
                    }
                )
        return tuple(result)


def resolve_relationship_target(source_part: str, target: str) -> str:
    source = source_part.replace("\\", "/").lstrip("/")
    raw_target = target.replace("\\", "/")
    if raw_target.startswith("/"):
        resolved = posixpath.normpath(raw_target.lstrip("/"))
    else:
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(source), raw_target)
        )
    pure = PurePosixPath(resolved)
    if pure.is_absolute() or ".." in pure.parts:
        raise DocumentIndexError("A package relationship escapes the package.")
    return resolved


def relationship_id(element: ET.Element) -> str | None:
    return element.attrib.get(
        f"{{{OFFICE_RELATIONSHIP_NAMESPACE}}}id"
    ) or attribute_by_local_name(element, "id")

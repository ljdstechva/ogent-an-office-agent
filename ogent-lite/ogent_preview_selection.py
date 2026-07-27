#!/usr/bin/env python3
"""Secure, event-driven OfficeCLI live-preview selection context."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import re
import secrets
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable


SELECTION_PROTOCOL = "officecli-preview-selection"
SELECTION_PROTOCOL_VERSION = 1
MAX_PREVIEW_SELECTION_TARGETS = 20
MAX_RAW_SELECTION_PATHS = 400
MAX_EXCERPT_CHARS = 1_200
MAX_LABEL_CHARS = 100

SUPPORTED_ELEMENT_KINDS = {
    "paragraph",
    "heading",
    "list_item",
    "table",
    "row",
    "cell",
    "range",
    "slide",
    "shape",
    "text_box",
    "chart",
    "picture",
    "connector",
    "group",
}

DOCX_PATH_PATTERN = re.compile(
    r"^/body/(?:"
    r"(?:p|paragraph)\[(?:\d+|@paraId=[A-Fa-f0-9]{1,16})\]"
    r"|(?:tbl|table)\[\d+\]"
    r"(?:/(?:tr|row)\[\d+\](?:/(?:tc|cell)\[\d+\])?)?"
    r")$"
)
PPTX_PATH_PATTERN = re.compile(
    r"^/slide\[[1-9]\d{0,5}\](?:/"
    r"(?:shape|picture|table|chart|connector|group)"
    r"\[(?:\d+|@(?:id|name)=[^\]\x00-\x1f]{1,160})\]"
    r"(?:/(?:tr|row)\[\d+\](?:/(?:tc|cell)\[\d+\])?)?"
    r")?$"
)
XLSX_PATH_PATTERN = re.compile(
    r"^/[^/\x00-\x1f]{1,128}/(?:"
    r"[A-Za-z]{1,3}[1-9]\d{0,6}(?::[A-Za-z]{1,3}[1-9]\d{0,6})?"
    r"|row\[[1-9]\d{0,6}\]"
    r"|col\[[A-Za-z]{1,3}\]"
    r")$"
)
DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,160}$")
# Runtime sessions are UUID hex strings. Accepting a bounded safe identifier here
# also keeps the protocol model independently testable without weakening the
# cross-session equality, channel, watch, document, and revision checks.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CHANNEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,160}$")
WATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,160}$")


class PreviewSelectionError(RuntimeError):
    """A selection validation error safe for the local Ogent UI."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise PreviewSelectionError("Selection timestamps must be timezone-aware.")
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_plain_text(value: Any, maximum: int) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:maximum]


def path_allowed(document_format: str, path: str) -> bool:
    normalized = str(document_format).casefold().lstrip(".")
    value = str(path or "")
    if len(value) > 320:
        return False
    if normalized == "docx":
        return bool(DOCX_PATH_PATTERN.fullmatch(value))
    if normalized == "pptx":
        return bool(PPTX_PATH_PATTERN.fullmatch(value))
    if normalized == "xlsx":
        return bool(XLSX_PATH_PATTERN.fullmatch(value))
    return False


def _column_number(value: str) -> int:
    result = 0
    for character in value.upper():
        result = result * 26 + ord(character) - 64
    return result


def _column_name(value: int) -> str:
    result = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def compact_excel_rectangle(paths: list[str]) -> str | None:
    if not paths:
        return None
    cells: list[tuple[str, str, int]] = []
    for path in paths:
        match = re.fullmatch(
            r"(/[^/\x00-\x1f]{1,128})/([A-Za-z]{1,3})([1-9]\d{0,6})",
            path,
        )
        if not match:
            return None
        cells.append((match.group(1), match.group(2).upper(), int(match.group(3))))
    sheet = cells[0][0]
    if any(item[0] != sheet for item in cells):
        return None
    columns = [_column_number(item[1]) for item in cells]
    rows = [item[2] for item in cells]
    min_column, max_column = min(columns), max(columns)
    min_row, max_row = min(rows), max(rows)
    expected = {
        (column, row)
        for row in range(min_row, max_row + 1)
        for column in range(min_column, max_column + 1)
    }
    actual = set(zip(columns, rows, strict=True))
    if actual != expected:
        return None
    start = f"{_column_name(min_column)}{min_row}"
    end = f"{_column_name(max_column)}{max_row}"
    if start == end:
        return f"{sheet}/{start}"
    return f"{sheet}/{start}:{end}"


def _node_kind(document_format: str, node: dict[str, Any], path: str) -> str:
    normalized_format = document_format.casefold().lstrip(".")
    node_type = str(node.get("type") or "").casefold()
    format_value = node.get("format")
    node_format = format_value if isinstance(format_value, dict) else {}
    style = str(node.get("style") or node_format.get("style") or "")
    if normalized_format == "xlsx":
        return "range" if ":" in path.rsplit("/", 1)[-1] else "cell"
    if normalized_format == "docx" and node_type in {"paragraph", "p"}:
        if style.casefold().startswith("heading"):
            return "heading"
        if any(
            key in node_format for key in ("numId", "numbering", "list")
        ):
            return "list_item"
        return "paragraph"
    aliases = {
        "p": "paragraph",
        "paragraph": "paragraph",
        "tbl": "table",
        "table": "table",
        "tr": "row",
        "row": "row",
        "tc": "cell",
        "cell": "cell",
        "slide": "slide",
        "shape": "shape",
        "textbox": "text_box",
        "text box": "text_box",
        "chart": "chart",
        "picture": "picture",
        "image": "picture",
        "connector": "connector",
        "group": "group",
    }
    return aliases.get(node_type, "shape" if normalized_format == "pptx" else "")


def _target_label(
    document_format: str,
    path: str,
    kind: str,
    node: dict[str, Any],
) -> str:
    if document_format.casefold().lstrip(".") == "xlsx":
        sheet, address = path.rsplit("/", 1)
        return f"{sheet.lstrip('/')}!{address}"
    text = _bounded_plain_text(
        node.get("preview") or node.get("text") or "",
        MAX_LABEL_CHARS,
    )
    if text and text != "(empty)":
        return text
    friendly = kind.replace("_", " ").title() or "Element"
    return f"{friendly} \u00b7 {path.rsplit('/', 1)[-1]}"[:MAX_LABEL_CHARS]


@dataclasses.dataclass(frozen=True)
class PreviewSelectionTarget:
    selection_id: str
    session_id: str
    document_id: str
    document_name: str
    document_format: str
    path: str
    kind: str
    label: str
    order: int
    primary: bool
    watch_id: str
    revision: int
    selected_at: str
    excerpt: str
    text_range_anchor: dict[str, int] | None = None
    stale: bool = False

    def to_dict(self, *, include_excerpt: bool = True) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        if not include_excerpt:
            value.pop("excerpt", None)
            value.pop("text_range_anchor", None)
        return value


@dataclasses.dataclass(frozen=True)
class PreviewSelectionSnapshot:
    snapshot_id: str
    session_id: str
    document_id: str
    document_name: str
    document_format: str
    watch_id: str
    revision: int
    created_at: str
    targets: tuple[PreviewSelectionTarget, ...]

    def to_dict(self, *, include_excerpts: bool = True) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "session_id": self.session_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "document_format": self.document_format,
            "watch_id": self.watch_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "targets": [
                item.to_dict(include_excerpt=include_excerpts)
                for item in self.targets
            ],
        }


class PreviewSelectionState:
    """Mutable composer selection for one active watch generation."""

    def __init__(
        self,
        session_id: str,
        *,
        clock: Callable[[], dt.datetime] = utc_now,
    ) -> None:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise PreviewSelectionError("Invalid Ogent session id.")
        self.session_id = session_id
        self.clock = clock
        self.lock = threading.RLock()
        self.channel_id = secrets.token_urlsafe(24)
        self.watch_id = uuid.uuid4().hex
        self.document_id = ""
        self.document_name = ""
        self.document_format = ""
        self.revision = 0
        self.targets: list[PreviewSelectionTarget] = []
        self.limit_message: str | None = None

    def _now(self) -> dt.datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise PreviewSelectionError(
                "The preview-selection clock must be timezone-aware."
            )
        return value.astimezone(dt.timezone.utc)

    def reset_for_watch(
        self,
        *,
        document_id: str,
        document_name: str,
        document_format: str,
        revision: int,
    ) -> None:
        normalized_format = document_format.casefold().lstrip(".")
        if (
            not DOCUMENT_ID_PATTERN.fullmatch(document_id)
            or normalized_format not in {"docx", "xlsx", "pptx"}
            or revision < 0
        ):
            raise PreviewSelectionError("Invalid active preview identity.")
        with self.lock:
            self.channel_id = secrets.token_urlsafe(24)
            self.watch_id = uuid.uuid4().hex
            self.document_id = document_id
            self.document_name = Path(document_name).name
            self.document_format = normalized_format
            self.revision = int(revision)
            self.targets.clear()
            self.limit_message = None

    def restart_watch(self) -> None:
        """Invalidate, but retain visibly stale, unsent targets."""
        with self.lock:
            old_targets = list(self.targets)
            self.channel_id = secrets.token_urlsafe(24)
            self.watch_id = uuid.uuid4().hex
            self.targets = [
                dataclasses.replace(item, stale=True) for item in old_targets
            ]

    def advance_revision(self, revision: int) -> None:
        with self.lock:
            if revision <= self.revision:
                return
            self.revision = revision
            self.targets = [
                dataclasses.replace(item, stale=True) for item in self.targets
            ]

    def clear(self) -> None:
        with self.lock:
            self.targets.clear()
            self.limit_message = None

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "protocol": SELECTION_PROTOCOL,
                "version": SELECTION_PROTOCOL_VERSION,
                "session_id": self.session_id,
                "channel_id": self.channel_id,
                "watch_id": self.watch_id,
                "document_id": self.document_id,
                "document_name": self.document_name,
                "document_format": self.document_format,
                "revision": self.revision,
                "targets": [
                    item.to_dict(include_excerpt=False) for item in self.targets
                ],
                "limit_message": self.limit_message,
            }

    def validate_bridge_envelope(
        self,
        payload: dict[str, Any],
        *,
        event_origin: str,
        expected_origin: str,
        source_matches: bool,
    ) -> list[str]:
        """Validate a future postMessage bridge before backend resolution."""
        with self.lock:
            if event_origin != expected_origin:
                raise PreviewSelectionError("Preview selection origin mismatch.")
            if not source_matches:
                raise PreviewSelectionError("Preview selection iframe mismatch.")
            if payload.get("protocol") != SELECTION_PROTOCOL:
                raise PreviewSelectionError("Unsupported preview selection protocol.")
            if payload.get("version") != SELECTION_PROTOCOL_VERSION:
                raise PreviewSelectionError("Unsupported preview selection version.")
            if payload.get("type") != "selection.changed":
                raise PreviewSelectionError("Unsupported preview selection event.")
            if payload.get("channel_id") != self.channel_id:
                raise PreviewSelectionError("Stale preview selection channel.")
            if payload.get("watch_id") != self.watch_id:
                raise PreviewSelectionError("Stale preview watch generation.")
            if payload.get("document_id") != self.document_id:
                raise PreviewSelectionError("Preview document identity mismatch.")
            if payload.get("session_id") != self.session_id:
                raise PreviewSelectionError("Preview session identity mismatch.")
            if payload.get("revision") != self.revision:
                raise PreviewSelectionError("Stale preview document revision.")
            selected = payload.get("selected")
            if not isinstance(selected, list):
                raise PreviewSelectionError("Malformed preview selection list.")
            return self._raw_paths(selected)

    def _raw_paths(self, selected: Iterable[Any]) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for item in selected:
            if len(paths) >= MAX_RAW_SELECTION_PATHS:
                raise PreviewSelectionError(
                    "The preview sent an excessive selection payload."
                )
            if isinstance(item, dict):
                value = str(item.get("path") or "")
            else:
                value = str(item or "")
            if not path_allowed(self.document_format, value):
                raise PreviewSelectionError(
                    "The preview sent an invalid or unsupported OfficeCLI path."
                )
            if value in seen:
                continue
            seen.add(value)
            paths.append(value)
        return paths

    def apply_paths(
        self,
        selected: Iterable[Any],
        resolver: Callable[[list[str]], list[dict[str, Any]]],
        *,
        primary_path: str | None = None,
        expected_watch_id: str | None = None,
        expected_document_id: str | None = None,
        expected_revision: int | None = None,
    ) -> list[PreviewSelectionTarget]:
        """Resolve watch paths against the active document and accept them."""
        with self.lock:
            if expected_watch_id is not None and expected_watch_id != self.watch_id:
                raise PreviewSelectionError("Stale preview watch generation.")
            if (
                expected_document_id is not None
                and expected_document_id != self.document_id
            ):
                raise PreviewSelectionError("Preview document identity mismatch.")
            if expected_revision is not None and expected_revision != self.revision:
                raise PreviewSelectionError("Stale preview document revision.")
            raw_paths = self._raw_paths(selected)
            if not raw_paths:
                self.clear()
                return []
            raw_count = len(raw_paths)
            rectangle = (
                compact_excel_rectangle(raw_paths)
                if self.document_format == "xlsx"
                else None
            )
            resolve_paths = (
                [rectangle]
                if rectangle
                else raw_paths[:MAX_PREVIEW_SELECTION_TARGETS]
            )
            nodes = resolver(resolve_paths)
            if not isinstance(nodes, list) or len(nodes) != len(resolve_paths):
                raise PreviewSelectionError(
                    "OfficeCLI could not resolve every selected target."
                )

            targets: list[PreviewSelectionTarget] = []
            canonical_seen: set[str] = set()
            now = utc_iso(self._now())
            for requested_path, node in zip(resolve_paths, nodes, strict=True):
                if not isinstance(node, dict):
                    raise PreviewSelectionError(
                        "OfficeCLI returned invalid selection metadata."
                    )
                canonical_path = str(node.get("path") or requested_path)
                if not path_allowed(self.document_format, canonical_path):
                    # Stable DOCX/PPTX IDs are allowed even when the original
                    # watch path was positional; the patterns already cover
                    # those supported stable forms.
                    raise PreviewSelectionError(
                        "OfficeCLI returned an unsupported canonical target path."
                    )
                if canonical_path in canonical_seen:
                    continue
                canonical_seen.add(canonical_path)
                kind = _node_kind(self.document_format, node, canonical_path)
                if kind not in SUPPORTED_ELEMENT_KINDS:
                    raise PreviewSelectionError(
                        "The selected Office element type is not supported."
                    )
                excerpt = _bounded_plain_text(
                    node.get("text") or node.get("preview") or "",
                    MAX_EXCERPT_CHARS,
                )
                target = PreviewSelectionTarget(
                    selection_id=uuid.uuid4().hex,
                    session_id=self.session_id,
                    document_id=self.document_id,
                    document_name=self.document_name,
                    document_format=self.document_format,
                    path=canonical_path,
                    kind=kind,
                    label=_target_label(
                        self.document_format,
                        canonical_path,
                        kind,
                        node,
                    ),
                    order=len(targets),
                    primary=(
                        canonical_path == primary_path
                        if primary_path
                        else len(targets) == 0
                    ),
                    watch_id=self.watch_id,
                    revision=self.revision,
                    selected_at=now,
                    excerpt=excerpt,
                    text_range_anchor=None,
                    stale=False,
                )
                targets.append(target)
            if targets and not any(item.primary for item in targets):
                targets[0] = dataclasses.replace(targets[0], primary=True)
            self.limit_message = (
                "Selection is limited to 20 targets per message. "
                "The first 20 remain selected."
                if raw_count > MAX_PREVIEW_SELECTION_TARGETS and rectangle is None
                else None
            )
            self.targets = targets
            return list(targets)

    def remove(self, selection_id: str) -> list[PreviewSelectionTarget]:
        with self.lock:
            remaining = [
                item for item in self.targets if item.selection_id != selection_id
            ]
            if len(remaining) == len(self.targets):
                raise PreviewSelectionError("Preview selection target not found.")
            self.targets = [
                dataclasses.replace(
                    item,
                    order=index,
                    primary=(index == 0),
                )
                for index, item in enumerate(remaining)
            ]
            self.limit_message = None
            return list(self.targets)

    def snapshot_for_send(self) -> PreviewSelectionSnapshot | None:
        with self.lock:
            if not self.targets:
                return None
            if any(item.stale for item in self.targets):
                raise PreviewSelectionError(
                    "The selected preview context is stale. Reselect the targets."
                )
            if any(
                item.watch_id != self.watch_id
                or item.document_id != self.document_id
                or item.revision != self.revision
                for item in self.targets
            ):
                raise PreviewSelectionError(
                    "The selected preview context no longer matches the active document."
                )
            return PreviewSelectionSnapshot(
                snapshot_id=uuid.uuid4().hex,
                session_id=self.session_id,
                document_id=self.document_id,
                document_name=self.document_name,
                document_format=self.document_format,
                watch_id=self.watch_id,
                revision=self.revision,
                created_at=utc_iso(self._now()),
                targets=tuple(self.targets),
            )

    def claim_for_send(self) -> PreviewSelectionSnapshot | None:
        """Atomically snapshot and clear the current composer selection."""
        with self.lock:
            snapshot = self.snapshot_for_send()
            if snapshot is not None:
                self.targets.clear()
                self.limit_message = None
            return snapshot


def post_watch_selection(port: int, paths: Iterable[str]) -> None:
    """Update OfficeCLI's native visual selection without exposing a token."""
    if not (1 <= int(port) <= 65535):
        raise PreviewSelectionError("Invalid OfficeCLI watch port.")
    values = [str(path) for path in paths]
    body = json.dumps({"paths": values}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api/selection",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status not in {200, 204}:
                raise PreviewSelectionError(
                    "OfficeCLI did not accept the updated selection."
                )
    except (OSError, urllib.error.URLError) as exc:
        raise PreviewSelectionError(
            "The OfficeCLI preview could not update its selection."
        ) from exc


class OfficeCLISelectionBroker:
    """Subscribe to the exact watch SSE endpoint and relay selection changes."""

    def __init__(
        self,
        port: int,
        *,
        on_selection: Callable[[list[str]], None],
        on_document_event: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not (1 <= int(port) <= 65535):
            raise PreviewSelectionError("Invalid OfficeCLI watch port.")
        self.port = int(port)
        self.on_selection = on_selection
        self.on_document_event = on_document_event
        self.on_error = on_error
        self.urlopen = urlopen
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.response: Any | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._run,
                name=f"ogent-watch-selection-{self.port}",
                daemon=True,
            )
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            response = self.response
            thread = self.thread
            self.response = None
            self.thread = None
        if response is not None:
            with contextlib.suppress(OSError):
                response.close()
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=3)

    def _run(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/events",
            method="GET",
            headers={"Accept": "text/event-stream"},
        )
        try:
            # A watch SSE stream is intentionally long-lived and can remain
            # quiet while the user reads the preview.  A per-socket timeout
            # would permanently disable selection synchronization after an
            # idle interval; stop() closes the response to unblock readline.
            response = self.urlopen(request)
            with self.lock:
                if self.stop_event.is_set():
                    response.close()
                    return
                self.response = response
            data_lines: list[str] = []
            while not self.stop_event.is_set():
                raw = response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        self._dispatch("\n".join(data_lines))
                        data_lines.clear()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            if not self.stop_event.is_set() and self.on_error is not None:
                self.on_error(
                    f"OfficeCLI selection bridge stopped: "
                    f"{str(exc).strip() or type(exc).__name__}"
                )
        finally:
            with self.lock:
                response = self.response
                self.response = None
            if response is not None:
                with contextlib.suppress(OSError):
                    response.close()

    def _dispatch(self, data: str) -> None:
        try:
            payload = json.loads(data)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("action") == "selection-update":
            paths = payload.get("paths")
            if isinstance(paths, list):
                self.on_selection([str(item) for item in paths])
            return
        if (
            payload.get("action") not in {"mark-update"}
            and self.on_document_event is not None
        ):
            self.on_document_event(payload)

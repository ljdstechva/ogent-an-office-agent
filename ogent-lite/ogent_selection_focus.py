#!/usr/bin/env python3
"""Trusted historical-selection resolution and OfficeCLI watch focus."""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from ogent_preview_selection import path_allowed


FOCUS_COLOR = "#FFD54F"
FOCUS_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
OFFICECLI_MARK_ID_PATTERN = re.compile(r"^[1-9]\d{0,11}$")
MAX_FOCUS_PAYLOAD_KEYS = frozenset({"message_sequence", "selection_id"})
WORD_POSITIONAL_PATH = re.compile(
    r"^/body/(?:(?:p|paragraph|table|tbl)\[\d+\]"
    r"(?:/(?:tr|row)\[\d+\](?:/(?:tc|cell)\[\d+\])?)?)$"
)
EXCEL_RANGE_PATH = re.compile(
    r"^(?P<sheet>/[^/\x00-\x1f]{1,128})/"
    r"(?P<c1>[A-Za-z]{1,3})(?P<r1>[1-9]\d{0,6}):"
    r"(?P<c2>[A-Za-z]{1,3})(?P<r2>[1-9]\d{0,6})$"
)


class HistoricalFocusError(RuntimeError):
    """A safe focus failure with an HTTP-compatible status."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclasses.dataclass(frozen=True)
class HistoricalSelectionReference:
    message_sequence: int
    selection_id: str
    session_id: str
    document_id: str
    document_name: str
    document_format: str
    canonical_path: str
    watch_path: str
    kind: str
    label: str
    stored_revision: int
    excerpt: str


@dataclasses.dataclass(frozen=True)
class ResolvedFocusTarget:
    reference: HistoricalSelectionReference
    canonical_path: str
    watch_path: str
    node: dict[str, Any]
    relocated: bool


@dataclasses.dataclass(frozen=True)
class OwnedFocusMark:
    mark_id: str
    path: str
    find: str | None


@dataclasses.dataclass
class HistoricalFocusState:
    """Tracks only Ogent-owned advisory marks for one browser workspace."""

    session_id: str
    marks: tuple[OwnedFocusMark, ...] = ()
    selection_id: str | None = None

    @property
    def note_prefix(self) -> str:
        return f"ogent-historical-focus:{self.session_id}:"


@dataclasses.dataclass(frozen=True)
class HistoricalFocusResult:
    message_sequence: int
    selection_id: str
    label: str
    document_name: str
    document_format: str
    path: str
    watch_path: str
    relocated: bool
    mark_ids: tuple[str, ...]
    highlight_color: str
    center_strategy: str

    def public_state(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


Runner = Callable[..., Any]


def validate_focus_payload(payload: Any) -> tuple[int, str]:
    if not isinstance(payload, dict):
        raise HistoricalFocusError("Invalid historical selection request.", 400)
    if set(payload) != MAX_FOCUS_PAYLOAD_KEYS:
        raise HistoricalFocusError(
            "Historical focus accepts only message_sequence and selection_id.",
            400,
        )
    sequence = payload.get("message_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise HistoricalFocusError("Invalid historical message sequence.", 400)
    selection_id = payload.get("selection_id")
    if not isinstance(selection_id, str) or not FOCUS_ID_PATTERN.fullmatch(
        selection_id
    ):
        raise HistoricalFocusError("Invalid historical selection id.", 400)
    return sequence, selection_id


def resolve_memory_selection(
    memory: Any,
    *,
    expected_session_id: str,
    message_sequence: int,
    selection_id: str,
) -> HistoricalSelectionReference:
    """Resolve only from canonical memory; never trust the public transcript."""
    if memory is None:
        raise HistoricalFocusError("Session memory is unavailable.", 409)
    with memory.lock:
        turns = tuple(memory.turns)
    turn = next(
        (
            item
            for item in turns
            if item.sequence == message_sequence and item.role == "user"
        ),
        None,
    )
    if turn is None:
        raise HistoricalFocusError(
            "That submitted message is no longer available in this workspace.",
            404,
        )
    matches = [
        item
        for item in turn.preview_selections
        if isinstance(item, dict) and item.get("selection_id") == selection_id
    ]
    if len(matches) != 1:
        raise HistoricalFocusError(
            "That selection does not belong to the submitted message.",
            404,
        )
    value = dict(matches[0])
    session_id = str(value.get("session_id") or "")
    document_id = str(value.get("document_id") or "")
    document_format = str(value.get("document_format") or "").casefold().lstrip(".")
    canonical_path = str(value.get("path") or "")
    watch_path = str(value.get("watch_path") or canonical_path)
    turn_document_id = str((turn.document or {}).get("document_id") or "")
    if session_id != expected_session_id:
        raise HistoricalFocusError(
            "That selection belongs to another Ogent workspace.",
            403,
        )
    if not document_id or turn_document_id != document_id:
        raise HistoricalFocusError(
            "The historical selection has an invalid document identity.",
            409,
        )
    if not path_allowed(document_format, canonical_path) or not path_allowed(
        document_format,
        watch_path,
    ):
        raise HistoricalFocusError(
            "The historical selection has an unsupported OfficeCLI path.",
            409,
        )
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise HistoricalFocusError(
            "The historical selection has an invalid document revision.",
            409,
        )
    return HistoricalSelectionReference(
        message_sequence=message_sequence,
        selection_id=selection_id,
        session_id=session_id,
        document_id=document_id,
        document_name=Path(str(value.get("document_name") or "Document")).name,
        document_format=document_format,
        canonical_path=canonical_path,
        watch_path=watch_path,
        kind=str(value.get("kind") or ""),
        label=_bounded_text(value.get("label") or canonical_path, 100),
        stored_revision=revision,
        excerpt=_bounded_text(value.get("excerpt") or "", 1_200),
    )


def package_sha256(document: Path) -> str:
    digest = hashlib.sha256()
    with Path(document).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_text(value: Any, maximum: int) -> str:
    text = re.sub(r"[\x00-\x1f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:maximum]


def _run(
    runner: Runner,
    document: Path,
    arguments: list[str],
    *,
    timeout: float = 30,
) -> Any:
    try:
        return runner(
            ["officecli", *arguments],
            cwd=document.parent,
            timeout=timeout,
        )
    except Exception as exc:
        raise HistoricalFocusError(
            "OfficeCLI could not resolve the historical selection.",
            503,
        ) from exc


def _json_output(result: Any) -> Any:
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        return json.loads(str(getattr(result, "stdout", "") or ""))
    except (TypeError, ValueError):
        return None


def _results_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    values = data.get("results")
    if not isinstance(values, list):
        values = data.get("Results")
    return [dict(item) for item in values or [] if isinstance(item, dict)]


def _get_node(
    runner: Runner,
    document: Path,
    path: str,
    *,
    depth: int | None = None,
) -> dict[str, Any] | None:
    arguments = ["get", str(document), path]
    if depth is not None:
        arguments.extend(["--depth", str(depth)])
    arguments.append("--json")
    nodes = _results_from_payload(_json_output(_run(runner, document, arguments)))
    return nodes[0] if len(nodes) == 1 else None


def _node_text(node: dict[str, Any]) -> str:
    text = _bounded_text(node.get("text") or node.get("preview") or "", 4_000)
    if text:
        return text
    children = node.get("children")
    if isinstance(children, list):
        return _bounded_text(
            " | ".join(
                str(item.get("text") or item.get("preview") or "")
                for item in children[:80]
                if isinstance(item, dict)
            ),
            4_000,
        )
    return ""


def _kind_family(kind: str) -> str:
    value = kind.casefold().replace(" ", "_")
    if value in {"paragraph", "heading", "list_item", "p"}:
        return "paragraph"
    if value in {"shape", "text_box", "textbox"}:
        return "shape"
    if value in {"cell", "range"}:
        return "cell"
    aliases = {"tbl": "table", "tr": "row", "tc": "cell"}
    return aliases.get(value, value)


def _node_kind(node: dict[str, Any], document_format: str, path: str) -> str:
    node_type = str(node.get("type") or "").casefold().replace(" ", "_")
    if document_format == "xlsx":
        return "range" if ":" in path.rsplit("/", 1)[-1] else "cell"
    if document_format == "docx" and node_type in {"p", "paragraph"}:
        fmt = node.get("format") if isinstance(node.get("format"), dict) else {}
        style = str(node.get("style") or fmt.get("style") or "")
        if style.casefold().startswith("heading"):
            return "heading"
        return "paragraph"
    aliases = {
        "p": "paragraph",
        "tbl": "table",
        "tr": "row",
        "tc": "cell",
        "textbox": "text_box",
        "text_box": "text_box",
    }
    return aliases.get(node_type, node_type)


def _stable_path(path: str) -> bool:
    return any(marker in path for marker in ("@paraId=", "@id=", "@name="))


def _fingerprint_matches(stored: str, current: str) -> bool:
    left = _bounded_text(stored, 1_200).casefold()
    right = _bounded_text(current, 1_200).casefold()
    if not left or not right:
        return True
    if left == right or left in right or right in left:
        return True
    ratio = difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()
    left_tokens = set(re.findall(r"[a-z0-9]{3,}", left))
    right_tokens = set(re.findall(r"[a-z0-9]{3,}", right))
    common = left_tokens & right_tokens
    overlap = len(common) / max(1, min(len(left_tokens), len(right_tokens)))
    return ratio >= 0.36 or (len(common) >= 4 and overlap >= 0.5)


def _exact_target_is_trusted(
    reference: HistoricalSelectionReference,
    node: dict[str, Any],
    *,
    current_revision: int,
) -> bool:
    current_kind = _node_kind(
        node,
        reference.document_format,
        str(node.get("path") or reference.canonical_path),
    )
    if _kind_family(current_kind) != _kind_family(reference.kind):
        return False
    if current_revision == reference.stored_revision:
        return True
    if _stable_path(reference.canonical_path):
        return True
    if reference.document_format == "xlsx":
        # A cell/range address is the user's logical target. Values are expected
        # to change after an edit, so content equality cannot be required.
        return True
    return _fingerprint_matches(reference.excerpt, _node_text(node))


def _query_selector_for(reference: HistoricalSelectionReference) -> str | None:
    family = _kind_family(reference.kind)
    if reference.document_format == "docx":
        return "p" if family == "paragraph" else family
    if reference.document_format == "xlsx":
        return "cell"
    if reference.document_format == "pptx":
        return "shape" if family == "shape" else family
    return None


def _relocation_anchors(excerpt: str) -> list[str]:
    cleaned = _bounded_text(excerpt, 1_200)
    if not cleaned:
        return []
    segments = [
        item.strip()
        for item in re.split(r'["\'\\\r\n]+', cleaned)
        if len(item.strip()) >= 12
    ]
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    segments.extend(
        sentence.strip()
        for sentence in sentences
        if len(re.findall(r"[A-Za-z0-9]{3,}", sentence)) >= 3
    )
    segments.extend(
        [
            cleaned[:96],
            cleaned[max(0, len(cleaned) // 2 - 48) : len(cleaned) // 2 + 48],
            cleaned[-96:],
        ]
    )
    values: list[str] = []
    for segment in segments:
        candidate = segment.strip()[:96].replace('"', " ").replace("\\", " ")
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if len(candidate) < 12 or candidate in values:
            continue
        values.append(candidate)
    return values[:5]


def _relocate_unique(
    runner: Runner,
    document: Path,
    reference: HistoricalSelectionReference,
) -> dict[str, Any] | None:
    selector = _query_selector_for(reference)
    if not selector:
        return None
    candidates: dict[str, dict[str, Any]] = {}
    for anchor in _relocation_anchors(reference.excerpt):
        query = f'{selector}:contains("{anchor}")'
        payload = _json_output(
            _run(
                runner,
                document,
                ["query", str(document), query, "--json"],
                timeout=45,
            )
        )
        for node in _results_from_payload(payload)[:100]:
            path = str(node.get("path") or "")
            if not path_allowed(reference.document_format, path):
                continue
            if _kind_family(
                _node_kind(node, reference.document_format, path)
            ) != _kind_family(reference.kind):
                continue
            if not _fingerprint_matches(reference.excerpt, _node_text(node)):
                continue
            candidates[path] = node
        if len(candidates) == 1:
            # Continue through one more independent anchor only when available;
            # a later conflicting candidate keeps relocation fail-closed.
            continue
        if len(candidates) > 1:
            break
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _same_canonical_path(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def _word_watch_path(
    runner: Runner,
    document: Path,
    canonical_path: str,
    stored_watch_path: str,
) -> str | None:
    if WORD_POSITIONAL_PATH.fullmatch(stored_watch_path):
        node = _get_node(runner, document, stored_watch_path)
        if node is not None and _same_canonical_path(
            str(node.get("path") or stored_watch_path),
            canonical_path,
        ):
            return stored_watch_path
    if WORD_POSITIONAL_PATH.fullmatch(canonical_path):
        return canonical_path
    if not re.fullmatch(
        r"^/body/(?:p|paragraph)\[@paraId=[A-Fa-f0-9]{1,16}\]$",
        canonical_path,
    ):
        return None
    body = _get_node(runner, document, "/body", depth=1)
    children = body.get("children") if isinstance(body, dict) else None
    if not isinstance(children, list):
        return None
    paragraph_index = 0
    for child in children:
        if not isinstance(child, dict):
            continue
        child_type = str(child.get("type") or "").casefold()
        if child_type not in {"p", "paragraph"}:
            continue
        paragraph_index += 1
        if _same_canonical_path(str(child.get("path") or ""), canonical_path):
            return f"/body/p[{paragraph_index}]"
    return None


def resolve_current_target(
    runner: Runner,
    document: Path,
    reference: HistoricalSelectionReference,
    *,
    current_revision: int,
) -> ResolvedFocusTarget:
    if not Path(document).is_file():
        raise HistoricalFocusError(
            "The active Office document is missing. Open it again.",
            404,
        )
    exact = _get_node(runner, document, reference.canonical_path)
    relocated = False
    if exact is None or not _exact_target_is_trusted(
        reference,
        exact,
        current_revision=current_revision,
    ):
        exact = _relocate_unique(runner, document, reference)
        relocated = True
    if exact is None:
        raise HistoricalFocusError(
            "That section moved or was removed. Select it again in the current preview.",
            409,
        )
    canonical_path = str(exact.get("path") or reference.canonical_path)
    if not path_allowed(reference.document_format, canonical_path):
        raise HistoricalFocusError(
            "The relocated OfficeCLI path is unsupported.",
            409,
        )
    if reference.document_format == "docx":
        watch_path = _word_watch_path(
            runner,
            document,
            canonical_path,
            reference.watch_path,
        )
        if watch_path is None:
            raise HistoricalFocusError(
                "That section moved or was removed. Select it again in the current preview.",
                409,
            )
    else:
        watch_path = canonical_path
    return ResolvedFocusTarget(
        reference=reference,
        canonical_path=canonical_path,
        watch_path=watch_path,
        node=exact,
        relocated=relocated
        or not _same_canonical_path(canonical_path, reference.canonical_path),
    )


def _column_number(value: str) -> int:
    result = 0
    for character in value.upper():
        result = result * 26 + ord(character) - 64
    return result


def _column_name(value: int) -> str:
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _focus_paths(target: ResolvedFocusTarget) -> list[str]:
    if target.reference.document_format != "xlsx":
        return [target.watch_path]
    match = EXCEL_RANGE_PATH.fullmatch(target.watch_path)
    if not match:
        return [target.watch_path]
    first_column = _column_number(match.group("c1"))
    last_column = _column_number(match.group("c2"))
    first_row = int(match.group("r1"))
    last_row = int(match.group("r2"))
    if first_column > last_column or first_row > last_row:
        raise HistoricalFocusError(
            "The historical Excel range is invalid.",
            409,
        )
    cell_count = (last_column - first_column + 1) * (last_row - first_row + 1)
    if cell_count > 100:
        # Keep interaction bounded; the primary cell is still centered and
        # highlighted without spawning hundreds of CLI processes.
        return [
            f"{match.group('sheet')}/{_column_name(first_column)}{first_row}"
        ]
    return [
        f"{match.group('sheet')}/{_column_name(column)}{row}"
        for row in range(first_row, last_row + 1)
        for column in range(first_column, last_column + 1)
    ]


def _meaningful_anchor(text: str) -> str | None:
    value = _bounded_text(text, 1_200)
    if not value:
        return None
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?])\s+", value)
        if item.strip()
    ]
    for sentence in sentences:
        words = re.findall(r"[A-Za-z0-9]{2,}", sentence)
        if len(words) >= 4 and len(sentence) >= 18:
            return sentence[:180]
    return value[:180]


def _marks_payload(
    runner: Runner,
    document: Path,
) -> list[dict[str, Any]]:
    payload = _json_output(
        _run(
            runner,
            document,
            ["watch", "marks", str(document), "--json"],
        )
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("marks"), list):
        raise HistoricalFocusError(
            "OfficeCLI could not inspect the current preview marks.",
            503,
        )
    return [dict(item) for item in payload["marks"] if isinstance(item, dict)]


def _mark_arguments(
    document: Path,
    *,
    path: str,
    find: str | None,
    color: str | None,
    note: str | None,
    tofix: str | None,
) -> list[str]:
    arguments = ["watch", "mark", str(document), path]
    for key, value in (
        ("find", find),
        ("color", color),
        ("note", note),
        ("tofix", tofix),
    ):
        if value is not None and str(value) != "":
            arguments.extend(["--prop", f"{key}={value}"])
    arguments.append("--json")
    return arguments


def _add_mark(
    runner: Runner,
    document: Path,
    *,
    path: str,
    find: str | None,
    color: str | None,
    note: str | None,
    tofix: str | None,
) -> dict[str, Any]:
    payload = _json_output(
        _run(
            runner,
            document,
            _mark_arguments(
                document,
                path=path,
                find=find,
                color=color,
                note=note,
                tofix=tofix,
            ),
        )
    )
    if not isinstance(payload, dict) or not str(payload.get("id") or "").isdigit():
        raise HistoricalFocusError(
            "OfficeCLI could not create the historical focus highlight.",
            503,
        )
    if payload.get("stale") is True:
        raise HistoricalFocusError(
            "That section moved or was removed. Select it again in the current preview.",
            409,
        )
    return payload


def _unmark_id(runner: Runner, document: Path, mark_id: str) -> None:
    if not OFFICECLI_MARK_ID_PATTERN.fullmatch(mark_id):
        raise HistoricalFocusError(
            "OfficeCLI returned an invalid historical highlight id.",
            503,
        )
    result = _run(
        runner,
        document,
        ["watch", "unmark", str(document), "--id", mark_id, "--json"],
    )
    if getattr(result, "returncode", 1) != 0:
        raise HistoricalFocusError(
            "OfficeCLI could not replace the previous historical highlight.",
            503,
        )


def _goto_mark(
    runner: Runner,
    document: Path,
    mark_id: str,
) -> None:
    if not OFFICECLI_MARK_ID_PATTERN.fullmatch(mark_id):
        raise HistoricalFocusError(
            "OfficeCLI returned an invalid historical highlight id.",
            503,
        )
    result = _run(
        runner,
        document,
        [
            "watch",
            "goto",
            str(document),
            "--mark-id",
            mark_id,
            "--json",
        ],
    )
    if getattr(result, "returncode", 1) != 0:
        raise HistoricalFocusError(
            "That section moved or was removed. Select it again in the current preview.",
            409,
        )


def _goto_path(
    runner: Runner,
    document: Path,
    *,
    document_format: str,
    path: str,
) -> None:
    if not path_allowed(document_format, path):
        raise HistoricalFocusError(
            "The historical selection has an unsupported OfficeCLI path.",
            409,
        )
    result = _run(
        runner,
        document,
        ["watch", "goto", str(document), path, "--json"],
    )
    if getattr(result, "returncode", 1) != 0:
        raise HistoricalFocusError(
            "That section moved or was removed. Select it again in the current preview.",
            409,
        )


def _clear_owned_marks(
    runner: Runner,
    document: Path,
    state: HistoricalFocusState,
) -> None:
    marks = _marks_payload(runner, document)
    state_ids = {item.mark_id for item in state.marks}
    owned = [
        item
        for item in marks
        if str(item.get("note") or "").startswith(state.note_prefix)
        or str(item.get("id") or "") in state_ids
    ]
    for mark_id in dict.fromkeys(str(item.get("id") or "") for item in owned):
        if not mark_id:
            continue
        _unmark_id(runner, document, mark_id)
    state.marks = ()
    state.selection_id = None


def focus_historical_target(
    runner: Runner,
    document: Path,
    target: ResolvedFocusTarget,
    state: HistoricalFocusState,
) -> HistoricalFocusResult:
    """Replace Ogent-owned marks, center the target, and preserve other marks."""
    _clear_owned_marks(runner, document, state)
    focus_paths = _focus_paths(target)
    owner_note = f"{state.note_prefix}{target.reference.selection_id}"
    primary_path = focus_paths[0]
    primary_node = target.node
    if primary_path != target.watch_path:
        primary_node = _get_node(runner, document, primary_path) or {}
    primary_text = _node_text(primary_node)
    primary_find = _meaningful_anchor(primary_text)
    created: list[OwnedFocusMark] = []
    try:
        for index, path in enumerate(focus_paths):
            mark = _add_mark(
                runner,
                document,
                path=path,
                find=primary_find if index == 0 else None,
                color=FOCUS_COLOR,
                note=owner_note,
                tofix=None,
            )
            created.append(
                OwnedFocusMark(
                    mark_id=str(mark["id"]),
                    path=path,
                    find=primary_find if index == 0 else None,
                )
            )
    except Exception:
        # Remove only marks created by this failed attempt.
        with_context = HistoricalFocusState(state.session_id)
        with_context.marks = tuple(created)
        _clear_owned_marks(runner, document, with_context)
        raise
    state.marks = tuple(created)
    state.selection_id = target.reference.selection_id
    primary = created[0]
    if primary.find:
        _goto_mark(runner, document, primary.mark_id)
        center_strategy = "meaningful-text-mark"
    else:
        _goto_path(
            runner,
            document,
            document_format=target.reference.document_format,
            path=primary_path,
        )
        center_strategy = "validated-element-path"
    return HistoricalFocusResult(
        message_sequence=target.reference.message_sequence,
        selection_id=target.reference.selection_id,
        label=target.reference.label,
        document_name=target.reference.document_name,
        document_format=target.reference.document_format,
        path=target.canonical_path,
        watch_path=target.watch_path,
        relocated=target.relocated,
        mark_ids=tuple(item.mark_id for item in created),
        highlight_color=FOCUS_COLOR,
        center_strategy=center_strategy,
    )

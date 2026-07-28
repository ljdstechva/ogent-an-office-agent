from __future__ import annotations

import copy
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

import ogent  # noqa: E402
import ogent_selection_focus as focus  # noqa: E402
from ogent_selection_focus import (  # noqa: E402
    HistoricalFocusError,
    HistoricalFocusResult,
    HistoricalFocusState,
    HistoricalSelectionReference,
    ResolvedFocusTarget,
    focus_historical_target,
    resolve_current_target,
    resolve_memory_selection,
    validate_focus_payload,
)
from ogent_session_memory import SessionMemoryStore  # noqa: E402


def completed(arguments: list[str], payload: object, returncode: int = 0):
    return subprocess.CompletedProcess(
        arguments,
        returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


class FixedClock:
    def __call__(self) -> dt.datetime:
        return dt.datetime(2026, 7, 27, 8, 0, tzinfo=dt.timezone.utc)


class NodeRunner:
    def __init__(
        self,
        *,
        exact: dict[str, object] | None,
        positional: dict[str, object] | None = None,
        relocated: list[dict[str, object]] | None = None,
        body_children: list[dict[str, object]] | None = None,
    ) -> None:
        self.exact = exact
        self.positional = positional
        self.relocated = relocated or []
        self.body_children = body_children or []
        self.calls: list[list[str]] = []

    def __call__(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        timeout: float,
    ):
        del cwd, timeout
        self.calls.append(list(arguments))
        command = arguments[1]
        if command == "query":
            return completed(
                arguments,
                {"data": {"results": copy.deepcopy(self.relocated)}},
            )
        if command != "get":
            raise AssertionError(arguments)
        path = arguments[3]
        if path == "/body":
            node = {
                "path": "/body",
                "type": "body",
                "children": copy.deepcopy(self.body_children),
            }
        elif "@paraId=" in path:
            node = copy.deepcopy(self.exact)
        else:
            node = copy.deepcopy(self.positional)
        return completed(
            arguments,
            {"data": {"results": [node] if node is not None else []}},
        )


class MarkRunner:
    def __init__(self, marks: list[dict[str, object]]) -> None:
        self.marks = copy.deepcopy(marks)
        self.next_id = 20
        self.calls: list[list[str]] = []

    def __call__(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        timeout: float,
    ):
        del cwd, timeout
        self.calls.append(list(arguments))
        self.assert_safe(arguments)
        operation = arguments[1:3]
        if operation == ["watch", "marks"]:
            return completed(arguments, {"marks": copy.deepcopy(self.marks)})
        if operation == ["watch", "unmark"]:
            mark_id = arguments[arguments.index("--id") + 1]
            self.marks = [
                item for item in self.marks if str(item.get("id")) != mark_id
            ]
            return completed(arguments, {"removed": True})
        if operation == ["watch", "goto"]:
            return completed(arguments, {"data": {"text": "centered"}})
        if operation == ["watch", "mark"]:
            path = arguments[4]
            props: dict[str, str] = {}
            for index, value in enumerate(arguments):
                if value == "--prop":
                    key, prop_value = arguments[index + 1].split("=", 1)
                    props[key] = prop_value
            mark = {
                "id": str(self.next_id),
                "path": path,
                **props,
            }
            self.next_id += 1
            self.marks.append(mark)
            return completed(
                arguments,
                {"id": mark["id"], "stale": False},
            )
        if arguments[1] == "get":
            path = arguments[3]
            return completed(
                arguments,
                {
                    "data": {
                        "results": [
                            {
                                "path": path,
                                "type": "cell",
                                "text": "Primary cell value",
                            }
                        ]
                    }
                },
            )
        raise AssertionError(arguments)

    @staticmethod
    def assert_safe(arguments: list[str]) -> None:
        if not isinstance(arguments, list) or arguments[0] != "officecli":
            raise AssertionError("OfficeCLI must be invoked as an argument array.")


class PreviewFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.document = self.root / "report.docx"
        self.document.write_bytes(b"synthetic-office-package")
        self.session_id = "a" * 32
        self.document_id = "document-12345678"
        self.selection_id = "b" * 32
        self.store = SessionMemoryStore(
            self.root / "memory",
            clock=FixedClock(),
            launch_id="c" * 32,
        )
        self.store.initialize()
        self.memory = self.store.create(self.session_id)
        self.memory.set_active_document(
            document_id=self.document_id,
            active_name=self.document.name,
            format="docx",
            revision=4,
        )
        self.selection = {
            "selection_id": self.selection_id,
            "session_id": self.session_id,
            "document_id": self.document_id,
            "document_name": self.document.name,
            "document_format": "docx",
            "path": "/body/p[@paraId=00100050]",
            "watch_path": "/body/p[32]",
            "kind": "paragraph",
            "label": "Executive Summary target",
            "order": 0,
            "primary": True,
            "watch_id": "d" * 32,
            "revision": 4,
            "selected_at": "2026-07-27T08:00:00Z",
            "excerpt": (
                "Executive Summary target sentence with enough meaningful words."
            ),
            "stale": False,
        }
        self.turn = self.memory.append_turn(
            "user",
            "Update the submitted paragraph.",
            preview_selections=[self.selection],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def reference(self, **changes: object) -> HistoricalSelectionReference:
        values: dict[str, object] = {
            "message_sequence": self.turn.sequence,
            "selection_id": self.selection_id,
            "session_id": self.session_id,
            "document_id": self.document_id,
            "document_name": self.document.name,
            "document_format": "docx",
            "canonical_path": "/body/p[@paraId=00100050]",
            "watch_path": "/body/p[32]",
            "kind": "paragraph",
            "label": "Executive Summary target",
            "stored_revision": 4,
            "excerpt": (
                "Executive Summary target sentence with enough meaningful words."
            ),
        }
        values.update(changes)
        return HistoricalSelectionReference(**values)  # type: ignore[arg-type]

    def test_focus_payload_is_an_exact_two_field_capability(self) -> None:
        self.assertEqual(
            validate_focus_payload(
                {
                    "message_sequence": self.turn.sequence,
                    "selection_id": self.selection_id,
                }
            ),
            (self.turn.sequence, self.selection_id),
        )
        for field, value in (
            ("path", "/../../secret"),
            ("selector", "body"),
            ("url", "http://evil.invalid"),
            ("port", 1),
            ("document_id", "other-document"),
            ("session_id", "other-session"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    HistoricalFocusError,
                    "accepts only",
                ):
                    validate_focus_payload(
                        {
                            "message_sequence": self.turn.sequence,
                            "selection_id": self.selection_id,
                            field: value,
                        }
                    )
        for payload in (
            {},
            {"message_sequence": True, "selection_id": self.selection_id},
            {"message_sequence": 0, "selection_id": self.selection_id},
            {
                "message_sequence": self.turn.sequence,
                "selection_id": "../../../../etc/passwd",
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(HistoricalFocusError):
                    validate_focus_payload(payload)

    def test_canonical_memory_resolution_rejects_cross_session_and_wrong_turn(self) -> None:
        resolved = resolve_memory_selection(
            self.memory,
            expected_session_id=self.session_id,
            message_sequence=self.turn.sequence,
            selection_id=self.selection_id,
        )
        self.assertEqual(resolved.canonical_path, self.selection["path"])
        self.assertEqual(resolved.watch_path, self.selection["watch_path"])

        with self.assertRaisesRegex(HistoricalFocusError, "another Ogent"):
            resolve_memory_selection(
                self.memory,
                expected_session_id="e" * 32,
                message_sequence=self.turn.sequence,
                selection_id=self.selection_id,
            )
        with self.assertRaisesRegex(HistoricalFocusError, "does not belong"):
            resolve_memory_selection(
                self.memory,
                expected_session_id=self.session_id,
                message_sequence=self.turn.sequence,
                selection_id="f" * 32,
            )
        assistant = self.memory.append_turn(
            "assistant",
            "Done.",
            preview_selections=[self.selection],
        )
        with self.assertRaisesRegex(HistoricalFocusError, "no longer available"):
            resolve_memory_selection(
                self.memory,
                expected_session_id=self.session_id,
                message_sequence=assistant.sequence,
                selection_id=self.selection_id,
                )

    def test_officecli_10143_is_enforced_before_watch_start(self) -> None:
        original_version = ogent.OFFICECLI_RUNTIME_VERSION
        original_text = ogent.OFFICECLI_RUNTIME_VERSION_TEXT
        self.addCleanup(
            setattr,
            ogent,
            "OFFICECLI_RUNTIME_VERSION",
            original_version,
        )
        self.addCleanup(
            setattr,
            ogent,
            "OFFICECLI_RUNTIME_VERSION_TEXT",
            original_text,
        )
        ogent.OFFICECLI_RUNTIME_VERSION = None
        ogent.OFFICECLI_RUNTIME_VERSION_TEXT = None
        too_old = subprocess.CompletedProcess(
            ["officecli", "--version"],
            0,
            stdout="1.0.142\n",
            stderr="",
        )
        with (
            mock.patch.object(ogent, "run_quiet", return_value=too_old),
            self.assertRaisesRegex(ogent.UserFacingError, "found 1.0.142"),
        ):
            ogent.ensure_officecli_compatible()

        ogent.OFFICECLI_RUNTIME_VERSION = None
        ogent.OFFICECLI_RUNTIME_VERSION_TEXT = None
        compatible = subprocess.CompletedProcess(
            ["officecli", "--version"],
            0,
            stdout="1.0.143-ogent-preview\n",
            stderr="",
        )
        with mock.patch.object(ogent, "run_quiet", return_value=compatible):
            self.assertEqual(ogent.ensure_officecli_compatible(), "1.0.143")

    def test_stable_word_identity_maps_back_to_the_rendered_watch_path(self) -> None:
        stable = {
            "path": "/body/p[@paraId=00100050]",
            "type": "paragraph",
            "style": "Normal",
            "text": self.selection["excerpt"],
        }
        runner = NodeRunner(exact=stable, positional=stable)

        target = resolve_current_target(
            runner,
            self.document,
            self.reference(),
            current_revision=9,
        )

        self.assertEqual(target.canonical_path, stable["path"])
        self.assertEqual(target.watch_path, "/body/p[32]")
        self.assertFalse(target.relocated)
        self.assertTrue(all(isinstance(call, list) for call in runner.calls))

    def test_relocation_is_unique_or_it_fails_closed(self) -> None:
        moved_path = "/body/p[@paraId=00100060]"
        moved = {
            "path": moved_path,
            "type": "paragraph",
            "style": "Normal",
            "text": self.selection["excerpt"],
        }
        runner = NodeRunner(
            exact=None,
            positional=None,
            relocated=[moved],
            body_children=[moved],
        )
        target = resolve_current_target(
            runner,
            self.document,
            self.reference(),
            current_revision=9,
        )
        self.assertTrue(target.relocated)
        self.assertEqual(target.canonical_path, moved_path)
        self.assertEqual(target.watch_path, "/body/p[1]")

        ambiguous = NodeRunner(
            exact=None,
            relocated=[
                moved,
                {
                    **moved,
                    "path": "/body/p[@paraId=00100070]",
                },
            ],
        )
        with self.assertRaisesRegex(HistoricalFocusError, "moved or was removed"):
            resolve_current_target(
                ambiguous,
                self.document,
                self.reference(),
                current_revision=9,
            )

    def test_owned_highlight_replaces_itself_and_preserves_unrelated_marks(self) -> None:
        state = HistoricalFocusState(self.session_id)
        owner_note = f"{state.note_prefix}old-selection"
        runner = MarkRunner(
            [
                {
                    "id": "7",
                    "path": "/body/p[32]",
                    "find": "Reviewer note",
                    "color": "#8BC34A",
                    "note": "reviewer-owned",
                    "tofix": "keep",
                },
                {
                    "id": "8",
                    "path": "/body/p[32]",
                    "find": "Old focus",
                    "color": "#FFD54F",
                    "note": owner_note,
                },
            ]
        )
        target = ResolvedFocusTarget(
            reference=self.reference(),
            canonical_path="/body/p[@paraId=00100050]",
            watch_path="/body/p[32]",
            node={
                "path": "/body/p[@paraId=00100050]",
                "type": "paragraph",
                "text": self.selection["excerpt"],
            },
            relocated=False,
        )
        before = self.document.read_bytes()

        result = focus_historical_target(
            runner,
            self.document,
            target,
            state,
        )

        self.assertEqual(self.document.read_bytes(), before)
        self.assertEqual(result.center_strategy, "meaningful-text-mark")
        self.assertEqual(result.highlight_color, "#FFD54F")
        self.assertIn(
            [
                "officecli",
                "watch",
                "goto",
                str(self.document),
                "--mark-id",
                result.mark_ids[0],
                "--json",
            ],
            runner.calls,
        )
        notes = [str(item.get("note") or "") for item in runner.marks]
        self.assertIn("reviewer-owned", notes)
        self.assertNotIn(owner_note, notes)
        self.assertIn(
            f"{state.note_prefix}{self.selection_id}",
            notes,
        )
        self.assertTrue(
            all(call[0] == "officecli" for call in runner.calls)
        )

    def test_large_excel_range_centers_and_marks_only_its_primary_cell(self) -> None:
        reference = self.reference(
            document_name="data.xlsx",
            document_format="xlsx",
            canonical_path="/Data/A1:Z200",
            watch_path="/Data/A1:Z200",
            kind="range",
        )
        target = ResolvedFocusTarget(
            reference=reference,
            canonical_path="/Data/A1:Z200",
            watch_path="/Data/A1:Z200",
            node={"path": "/Data/A1:Z200", "type": "range", "text": "range"},
            relocated=False,
        )
        runner = MarkRunner([])
        state = HistoricalFocusState(self.session_id)

        result = focus_historical_target(
            runner,
            self.document,
            target,
            state,
        )

        self.assertEqual(len(result.mark_ids), 1)
        owned = [
            item
            for item in runner.marks
            if str(item.get("note") or "").startswith(state.note_prefix)
        ]
        self.assertEqual([item["path"] for item in owned], ["/Data/A1"])
        self.assertEqual(owned[0]["find"], "Primary cell value")

    def test_runtime_navigation_uses_only_public_cli_argument_arrays(self) -> None:
        runner = MarkRunner([])
        focus._goto_mark(runner, self.document, "42")
        focus._goto_path(
            runner,
            self.document,
            document_format="pptx",
            path="/slide[9]/shape[@id=100034]",
        )
        self.assertEqual(
            runner.calls[0],
            [
                "officecli",
                "watch",
                "goto",
                str(self.document),
                "--mark-id",
                "42",
                "--json",
            ],
        )
        self.assertEqual(
            runner.calls[1],
            [
                "officecli",
                "watch",
                "goto",
                str(self.document),
                "/slide[9]/shape[@id=100034]",
                "--json",
            ],
        )
        with self.assertRaises(HistoricalFocusError):
            focus._goto_mark(runner, self.document, '42"] body')
        with self.assertRaises(HistoricalFocusError):
            focus._goto_path(
                runner,
                self.document,
                document_format="docx",
                path="/../../secret",
            )
        preview_source = (
            OGENT_DIR / "ogent_preview_selection.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("NamedPipe", preview_source)
        self.assertNotIn("CoreFxPipe_", preview_source)

    def test_coordinator_does_not_mutate_memory_transcript_or_composer(self) -> None:
        session = ogent.SessionState(self.session_id, memory=self.memory)
        with session.lock:
            session.active_doc = self.document.resolve()
            session.document_id = self.document_id
            session.document_revision = 4
            session.watch_port = 26320
            session.watch_generation = "1" * 32
            session.transcript = self.memory.public_transcript()
        session.preview_selection.reset_for_watch(
            document_id=self.document_id,
            document_name=self.document.name,
            document_format="docx",
            revision=4,
        )
        session.preview_selection.apply_paths(
            ["/body/p[2]"],
            lambda paths: [
                {
                    "path": paths[0],
                    "type": "paragraph",
                    "style": "Normal",
                    "text": "Current unsent composer selection",
                }
            ],
        )
        transcript_before = copy.deepcopy(session.transcript)
        composer_before = copy.deepcopy(session.preview_selection.public_state())
        memory_before = self.memory.memory_path.read_bytes()
        target = SimpleNamespace()
        result = HistoricalFocusResult(
            message_sequence=self.turn.sequence,
            selection_id=self.selection_id,
            label="Executive Summary target",
            document_name=self.document.name,
            document_format="docx",
            path="/body/p[@paraId=00100050]",
            watch_path="/body/p[32]",
            relocated=False,
            mark_ids=("21",),
            highlight_color="#FFD54F",
            center_strategy="meaningful-text-mark",
        )

        with (
            mock.patch.object(ogent, "watch_http_alive", return_value=True),
            mock.patch.object(
                ogent,
                "resolve_current_target",
                return_value=target,
            ),
            mock.patch.object(
                ogent,
                "focus_historical_target",
                return_value=result,
            ),
        ):
            response = ogent.focus_submitted_selection(
                session,
                {
                    "message_sequence": self.turn.sequence,
                    "selection_id": self.selection_id,
                },
            )

        self.assertEqual(response["selection_id"], self.selection_id)
        self.assertEqual(session.transcript, transcript_before)
        self.assertEqual(
            session.preview_selection.public_state(),
            composer_before,
        )
        self.assertEqual(self.memory.memory_path.read_bytes(), memory_before)
        self.assertEqual(
            response["document_sha256"],
            focus.package_sha256(self.document),
        )

    def test_coordinator_rejects_focus_while_a_document_is_opening(self) -> None:
        session = ogent.SessionState(self.session_id, memory=self.memory)
        with session.lock:
            session.active_doc = self.document.resolve()
            session.document_id = self.document_id
            session.document_revision = 4
            session.watch_port = 26320
            session.watch_generation = "1" * 32
            session.opening_source = self.root / "next.docx"

        with self.assertRaisesRegex(
            ogent.UserFacingError,
            "finish opening",
        ) as captured:
            ogent.focus_submitted_selection(
                session,
                {
                    "message_sequence": self.turn.sequence,
                    "selection_id": self.selection_id,
                },
            )

        self.assertEqual(captured.exception.status, 409)

    def test_stable_preview_identity_and_ui_contract_have_no_run_cache_buster(
        self,
    ) -> None:
        generation = "9" * 32
        self.assertEqual(
            ogent.stable_watch_url(26320, generation),
            f"http://127.0.0.1:26320/?generation={generation}",
        )
        html = ogent.HTML_TEMPLATE
        for expected in (
            "function canonicalPreviewUrl(url)",
            "function previewIdentityKey(identity, path, url)",
            "loadedPreviewKey !== key",
            'api("/selection/focus"',
            'card.type = "button"',
            "Focus submitted selection:",
            'headers["X-Ogent-Client"] = CLIENT_ID',
            '["v", "refresh", "revision", "cache", "_"]',
        ):
            self.assertIn(expected, html)
        self.assertNotIn("${state.watch_url}?v=", html)
        self.assertNotIn("${result.watch_url}?v=", html)
        self.assertNotRegex(
            html,
            re.compile(r"preview\.src\s*=\s*`[^`]*Date\.now"),
        )


if __name__ == "__main__":
    unittest.main()

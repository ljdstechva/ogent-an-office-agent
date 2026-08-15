"""Workspace agent command: recursive DOCX discovery and console picker."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


OGENT_PATH = Path(__file__).resolve().parents[1] / "ogent.py"
SPEC = importlib.util.spec_from_file_location("ogent_workspace_agent_test", OGENT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {OGENT_PATH}")
ogent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ogent)


class WorkspaceDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _touch(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_recursive_discovery_ignores_locks_and_checkpoints(self) -> None:
        keep_a = self._touch("report.docx")
        keep_b = self._touch("sub/deeper/chapter.docx")
        self._touch("~$report.docx")
        self._touch("sub/~$chapter.docx")
        self._touch(".officecli-checkpoints/report.docx/20260815-120000-manual.docx")
        self._touch("sub/.officecli-checkpoints/x.docx/20260815-120000-manual.docx")
        self._touch("notes.txt")
        found = ogent.discover_workspace_documents(self.root)
        self.assertEqual(found, sorted([keep_a, keep_b], key=lambda p: str(p).casefold()))

    def test_missing_folder_is_a_user_facing_error(self) -> None:
        with self.assertRaises(ogent.UserFacingError):
            ogent.discover_workspace_documents(self.root / "absent")

    def test_empty_workspace_prompt_fails_with_hint(self) -> None:
        with self.assertRaises(ogent.UserFacingError) as ctx:
            ogent.prompt_workspace_document(self.root)
        self.assertIn("No .docx files", str(ctx.exception))

    def test_single_document_is_auto_selected(self) -> None:
        only = self._touch("only.docx")
        with mock.patch("builtins.input", side_effect=AssertionError("no prompt")):
            self.assertEqual(ogent.prompt_workspace_document(self.root), only.resolve())

    def test_prompt_selects_by_number_and_rejects_bad_input(self) -> None:
        self._touch("a.docx")
        second = self._touch("b/b.docx")
        with mock.patch("builtins.input", side_effect=["0", "nope", "2"]):
            self.assertEqual(ogent.prompt_workspace_document(self.root), second.resolve())

    def test_prompt_cancel(self) -> None:
        self._touch("a.docx")
        self._touch("b.docx")
        with mock.patch("builtins.input", side_effect=["q"]):
            with self.assertRaises(ogent.UserFacingError):
                ogent.prompt_workspace_document(self.root)


def _keys(*sequence: str):
    """Scripted key reader for the interactive picker."""
    iterator = iter(sequence)
    return lambda: next(iterator)


class InteractivePickerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = [
            ("alpha.docx", Path("alpha.docx")),
            ("report-final.docx", Path("report-final.docx")),
            ("sub/beta.docx", Path("sub/beta.docx")),
        ]

    def test_filter_matches_substring_case_insensitively(self) -> None:
        matches = ogent._filter_workspace_choices(self.entries, "REPORT")
        self.assertEqual([label for label, _ in matches], ["report-final.docx"])
        self.assertEqual(
            ogent._filter_workspace_choices(self.entries, ""),
            self.entries,
        )
        self.assertEqual(ogent._filter_workspace_choices(self.entries, "zzz"), [])

    def test_arrow_keys_move_selection_and_enter_opens(self) -> None:
        # Down, Down, Enter -> third entry; wraps past the end back to first.
        selected = ogent.interactive_workspace_pick(
            self.entries,
            read_key=_keys("\xe0", "P", "\xe0", "P", "\r"),
        )
        self.assertEqual(selected, Path("sub/beta.docx"))
        wrapped = ogent.interactive_workspace_pick(
            self.entries,
            read_key=_keys("\xe0", "P", "\xe0", "P", "\xe0", "P", "\r"),
        )
        self.assertEqual(wrapped, Path("alpha.docx"))
        up = ogent.interactive_workspace_pick(
            self.entries,
            read_key=_keys("\xe0", "H", "\r"),
        )
        self.assertEqual(up, Path("sub/beta.docx"))

    def test_typing_filters_and_backspace_edits(self) -> None:
        selected = ogent.interactive_workspace_pick(
            self.entries,
            read_key=_keys("b", "e", "t", "\r"),
        )
        self.assertEqual(selected, Path("sub/beta.docx"))
        # "bx" matches nothing; Enter is ignored; backspace repairs it.
        repaired = ogent.interactive_workspace_pick(
            self.entries,
            read_key=_keys("b", "x", "\r", "\x08", "\r"),
        )
        self.assertEqual(repaired, Path("sub/beta.docx"))

    def test_escape_cancels(self) -> None:
        with self.assertRaises(ogent.UserFacingError):
            ogent.interactive_workspace_pick(self.entries, read_key=_keys("\x1b"))


class StartupRequirementTests(unittest.TestCase):
    def test_missing_officecli_gives_exact_hint(self) -> None:
        with mock.patch.object(ogent.shutil, "which", return_value=None):
            with self.assertRaises(ogent.UserFacingError) as ctx:
                ogent.verify_agent_startup_requirements()
        self.assertIn("officecli --version", str(ctx.exception))

    def test_failed_skill_load_gives_exact_hint(self) -> None:
        completed = mock.Mock(returncode=1, stdout="", stderr="skill store broken")
        with (
            mock.patch.object(
                ogent.shutil, "which", return_value=r"C:\x\officecli.exe"
            ),
            mock.patch.object(ogent.subprocess, "run", return_value=completed),
        ):
            with self.assertRaises(ogent.UserFacingError) as ctx:
                ogent.verify_agent_startup_requirements()
        message = str(ctx.exception)
        self.assertIn("load_skill word", message)
        self.assertIn("skill store broken", message)

    def test_healthy_environment_passes(self) -> None:
        completed = mock.Mock(returncode=0, stdout="SKILL TEXT", stderr="")
        with (
            mock.patch.object(
                ogent.shutil, "which", return_value=r"C:\x\officecli.exe"
            ),
            mock.patch.object(ogent.subprocess, "run", return_value=completed),
        ):
            ogent.verify_agent_startup_requirements()


if __name__ == "__main__":
    unittest.main()

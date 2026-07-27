from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


OGENT_PATH = Path(__file__).resolve().parents[1] / "ogent.py"
SPEC = importlib.util.spec_from_file_location("ogent_under_test", OGENT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {OGENT_PATH}")
ogent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ogent)


class ShellOpenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_state = ogent.STATE
        self.original_dispatch = ogent.dispatch_open_path
        self.original_server_info = ogent.SERVER_INFO_PATH
        self.original_import_root = ogent.IMPORT_ROOT
        self.original_port_available = ogent.port_available
        self.state = ogent.OgentState()
        ogent.STATE = self.state
        self.server = ogent.OgentServer((ogent.HOST, 0), ogent.OgentHandler)
        self.port = int(self.server.server_address[1])
        self.state.server_port = self.port
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="ogent-shell-open-test",
            daemon=True,
        )
        self.thread.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        ogent.SERVER_INFO_PATH = Path(self.temp_dir.name) / "server.json"
        ogent.IMPORT_ROOT = Path(self.temp_dir.name) / "imports"
        ogent.SERVER_INFO_PATH.write_text(
            json.dumps(
                {
                    "app": ogent.APP_NAME,
                    "version": ogent.APP_VERSION,
                    "pid": 0,
                    "port": self.port,
                    "token": self.state.token,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "test server did not stop")
        self.temp_dir.cleanup()
        ogent.STATE = self.original_state
        ogent.dispatch_open_path = self.original_dispatch
        ogent.SERVER_INFO_PATH = self.original_server_info
        ogent.IMPORT_ROOT = self.original_import_root
        ogent.port_available = self.original_port_available

    def test_shell_selection_prefers_latest_connected_focus(self) -> None:
        older = self.state.create_session()
        newer = self.state.create_session()
        disconnected = self.state.create_session()
        older.connect_sse("older-client")
        newer.connect_sse("newer-client")
        with older.lock:
            older.last_browser_activity = 100.0
        with newer.lock:
            newer.last_browser_activity = 200.0
        with disconnected.lock:
            disconnected.last_browser_activity = 300.0

        selected, created = self.state.select_shell_session()

        self.assertIs(selected, newer)
        self.assertFalse(created)

    def test_shell_selection_creates_workspace_for_resident_backend(self) -> None:
        selected, created = self.state.select_shell_session()

        self.assertTrue(created)
        self.assertIs(self.state.get_session(selected.session_id), selected)

    def test_replacement_watch_reserves_a_different_port(self) -> None:
        session = self.state.create_session()
        with session.lock:
            session.watch_port = ogent.WATCH_PORT_FIRST
        ogent.port_available = lambda _port: True

        replacement = self.state.allocate_watch_port(session, replace=True)

        self.assertEqual(replacement, ogent.WATCH_PORT_FIRST + 1)
        with session.lock:
            self.assertEqual(session.watch_port, replacement)

    def test_officecli_commands_use_direct_mode(self) -> None:
        environment = ogent.command_env()

        self.assertEqual(environment["OFFICECLI_NO_AUTO_RESIDENT"], "1")
        self.assertEqual(environment["OFFICECLI_RESIDENT_FLUSH"], "each")

    def test_busy_shell_open_returns_session_and_transcript_message(self) -> None:
        session = self.state.create_session()
        session.connect_sse("busy-client")
        with session.lock:
            session.run_status = "working"

        with self.assertRaises(ogent.UserFacingError) as caught:
            ogent.post_open_to_existing_server(self.port, r"C:\test\busy.docx")

        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.session_id, session.session_id)
        with session.lock:
            self.assertEqual(
                session.transcript[-1]["text"],
                "Ogent is still working. Stop that run or wait for it to finish.",
            )
        self.assertEqual(len(self.state.sessions), 1)

    def test_warm_shell_open_reuses_connected_workspace(self) -> None:
        first = self.state.create_session()
        target = self.state.create_session()
        first.connect_sse("first-client")
        target.connect_sse("target-client")
        with first.lock:
            first.last_browser_activity = 100.0
        with target.lock:
            target.last_browser_activity = 200.0
        calls: list[tuple[Any, str]] = []

        def fake_dispatch(session: Any, raw_path: str) -> dict[str, Any]:
            calls.append((session, raw_path))
            return {
                "action": "document_opened",
                "session_id": session.session_id,
                "message": "Working copy opened.",
            }

        ogent.dispatch_open_path = fake_dispatch

        result = ogent.post_open_to_existing_server(
            self.port,
            r"C:\test\warm switch.docx",
        )

        self.assertEqual(result["session_id"], target.session_id)
        self.assertEqual(calls, [(target, r"C:\test\warm switch.docx")])
        self.assertEqual(len(self.state.sessions), 2)

    def test_upload_preserves_bytes_and_dispatches_import_copy(self) -> None:
        session = self.state.create_session()
        session.connect_sse("upload-client")
        content = b"fake-office-package"
        calls: list[tuple[Any, str, bytes, str]] = []

        def fake_dispatch(
            current: Any,
            raw_path: str,
            *,
            origin: str = "local_path",
        ) -> dict[str, Any]:
            path = Path(raw_path)
            calls.append((current, raw_path, path.read_bytes(), origin))
            return {
                "action": "document_opened",
                "session_id": current.session_id,
                "message": "Working copy opened.",
            }

        ogent.dispatch_open_path = fake_dispatch
        filename = "résumé test.docx"
        request = urllib.request.Request(
            f"http://{ogent.HOST}:{self.port}/upload",
            data=content,
            headers={
                "X-Ogent-Token": self.state.token,
                "X-Ogent-Session": session.session_id,
                "X-Ogent-Filename": urllib.parse.quote(filename, safe=""),
                "Content-Type": "application/octet-stream",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))

        self.assertTrue(result["uploaded"])
        self.assertEqual(result["uploaded_name"], filename)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], session)
        self.assertEqual(calls[0][2], content)
        self.assertEqual(calls[0][3], "browser_upload")
        imported = Path(calls[0][1])
        self.assertEqual(imported.name, filename)
        self.assertTrue(imported.is_relative_to(ogent.IMPORT_ROOT))
        self.assertEqual(Path(result["import_source"]), imported)

    def test_safe_upload_filename_blocks_traversal_and_unsupported_types(self) -> None:
        self.assertEqual(
            ogent.safe_upload_filename(r"..\..\Quarterly Report.XLSX"),
            "Quarterly Report.xlsx",
        )
        self.assertEqual(ogent.safe_upload_filename("CON.pdf"), "_CON.pdf")
        with self.assertRaises(ogent.UserFacingError) as caught:
            ogent.safe_upload_filename("notes.txt")
        self.assertEqual(caught.exception.status, 415)


if __name__ == "__main__":
    unittest.main()

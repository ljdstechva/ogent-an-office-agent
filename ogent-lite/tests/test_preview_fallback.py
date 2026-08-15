from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

import ogent  # noqa: E402


class FakeConverterProcess:
    def __init__(
        self,
        arguments: list[str],
        *,
        returncode: int = 0,
        output: str = "converted",
    ) -> None:
        self.arguments = list(arguments)
        self.returncode = returncode
        self.output = output
        self.pid = 987654
        if returncode == 0:
            destination = Path(self.arguments[self.arguments.index("-OutPdf") + 1])
            destination.write_bytes(b"%PDF-1.7\nsynthetic-word-view\n%%EOF")

    def communicate(self, timeout: int) -> tuple[str, None]:
        if timeout != 120:
            raise AssertionError(f"unexpected timeout: {timeout}")
        return self.output, None

    def poll(self) -> int:
        return self.returncode


class PreviewFallbackTemplateTests(unittest.TestCase):
    @staticmethod
    def preview_source() -> str:
        return (
            OGENT_DIR / "web" / "src" / "components" / "document" / "PreviewSurface.tsx"
        ).read_text(encoding="utf-8")

    def test_preview_state_and_mode_unions_are_closed(self) -> None:
        source = self.preview_source()
        match = re.search(
            r"type PreviewState\s*=\s*(?P<states>[^;]+);",
            source,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(
            re.findall(r'"([^"]+)"', match.group("states")),
            [
                "empty",
                "loading",
                "ready",
                "degraded",
                "error",
            ],
        )
        self.assertIn('type PreviewMode = "live" | "word";', source)

    def test_live_view_waits_for_event_stream_and_uses_same_origin(self) -> None:
        source = self.preview_source()
        workspace_hook = (
            OGENT_DIR / "web" / "src" / "hooks" / "useWorkspace.ts"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'new URL("/preview", window.location.origin)',
            source,
        )
        self.assertNotIn("http://localhost", source)
        self.assertIn(
            "!workspace.preview_identity || !workspace.stream_connected",
            source,
        )
        self.assertIn(
            "source.onopen = () => dispatch",
            workspace_hook,
        )
        source = (
            Path(ogent.__file__).parent / "ogent_app" / "api" / "http_get_routes.py"
        ).read_text(encoding="utf-8")
        serve_events = source[
            source.index("    def _serve_events(") : source.index(
                "    def _write_event("
            )
        ]
        self.assertLess(
            serve_events.index("session.connect_sse(client_id)"),
            serve_events.index('self.send_header("Content-Type", "text/event-stream'),
        )

    def test_meaningful_live_content_and_complex_fallback_are_explicit(self) -> None:
        source = self.preview_source()
        self.assertIn('data.protocol === "ogent-preview-status"', source)
        self.assertIn('data.type === "preview.ready"', source)
        self.assertIn("data.meaningful === true", source)
        self.assertIn(
            'setStatus(approximate ? "degraded" : "ready")',
            source,
        )
        self.assertIn(
            'reportPreview("meaningless", detail, metrics)',
            source,
        )
        self.assertIn("void openWord();", source)

    def test_warning_banner_and_retry_controls_never_cover_the_iframe(self) -> None:
        source = self.preview_source()
        styles = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((OGENT_DIR / "web" / "src" / "styles").glob("*.css"))
        )
        warning = (
            "Live View is approximate for this complex Word layout. "
            "Use Exact Word View for authoritative rendering."
        )
        self.assertIn(warning, source)
        self.assertLess(
            source.index('className="preview-banner"'),
            source.index('className="preview-stage"'),
        )
        self.assertIn("Retry Live View", source)
        self.assertIn("Open Exact Word View", source)
        self.assertIn("@media (max-width: 760px)", styles)

    def test_manual_word_view_reuses_the_document_iframe(self) -> None:
        source = self.preview_source()
        self.assertIn("const openWord = useCallback", source)
        self.assertIn("setFrameUrl(value.href)", source)
        self.assertNotIn("window.open", source)
        self.assertIn("result.document_id !== identity.document_id", source)
        self.assertIn("result.document_revision", source)
        self.assertIn("wordIdentityRef.current = requestedKey", source)

    def test_live_and_word_failures_have_visible_recovery_paths(self) -> None:
        source = self.preview_source()
        self.assertIn('setStatus("error")', source)
        self.assertIn('"Live View unavailable"', source)
        self.assertIn('"Exact Word View unavailable"', source)
        self.assertIn(
            "onClick={() => void openLive(true)}",
            source,
        )
        self.assertIn(
            "onError={() =>",
            source,
        )

    def test_stale_preview_messages_and_snapshot_results_are_ignored(self) -> None:
        source = self.preview_source()
        self.assertIn(
            "data.document_id !== identity.document_id",
            source,
        )
        self.assertIn(
            "data.watch_generation !== identity.watch_generation",
            source,
        )
        self.assertIn(
            "result.document_id !== identity.document_id",
            source,
        )
        self.assertIn(
            "Number(result.document_revision ?? 0)",
            source,
        )


class WordViewSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.originals = {
            "WORK_ROOT": ogent.WORK_ROOT,
            "RUNTIME_LOG_PATH": ogent.RUNTIME_LOG_PATH,
            "STATE": ogent.STATE,
        }
        ogent.WORK_ROOT = self.root / "work"
        ogent.WORK_ROOT.mkdir()
        ogent.RUNTIME_LOG_PATH = self.root / "ogent.log"
        self.source = self.root / "complex-source.docx"
        self.source.write_bytes(b"PK-synthetic-docx-source")
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.session = ogent.SessionState("a" * 32)
        with self.session.lock:
            self.session.active_doc = self.source
            self.session.active_source = self.source
            self.session.document_id = "document-complex-1234"
            self.session.document_revision = 7
            self.session.watch_generation = "b" * 32

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(ogent, name, value)
        self.temporary.cleanup()

    def test_snapshot_is_owned_read_only_cached_and_revision_bound(self) -> None:
        calls: list[list[str]] = []

        def launch(arguments: list[str], **_kwargs: object) -> FakeConverterProcess:
            calls.append(list(arguments))
            return FakeConverterProcess(arguments)

        with mock.patch.object(ogent.subprocess, "Popen", side_effect=launch):
            first = ogent.generate_word_snapshot(self.session)
            second = ogent.generate_word_snapshot(self.session)

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            ogent.path_is_within(
                first,
                ogent.WORK_ROOT / self.session.session_id / "word-view",
            )
        )
        self.assertNotEqual(first.parent, self.source.parent)
        self.assertTrue(ogent.valid_pdf_file(first))
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
            self.source_hash,
        )
        self.assertEqual(
            self.session.snapshot_document_id,
            self.session.document_id,
        )
        self.assertEqual(
            self.session.snapshot_document_revision,
            self.session.document_revision,
        )
        self.assertEqual(
            self.session.snapshot_package_sha256,
            self.source_hash,
        )
        self.assertIsNotNone(self.session.snapshot_cache_key)

        with self.session.lock:
            ogent.invalidate_word_snapshot_locked(self.session)
        self.assertIsNone(self.session.snapshot_path)
        self.assertIsNone(self.session.snapshot_cache_key)

    def test_snapshot_failure_is_visible_and_logged(self) -> None:
        def launch(arguments: list[str], **_kwargs: object) -> FakeConverterProcess:
            return FakeConverterProcess(
                arguments,
                returncode=7,
                output="converter diagnostic",
            )

        with (
            mock.patch.object(ogent.subprocess, "Popen", side_effect=launch),
            self.assertRaises(ogent.UserFacingError) as caught,
        ):
            ogent.generate_word_snapshot(self.session)

        self.assertEqual(caught.exception.status, 500)
        self.assertIn("converter diagnostic", str(caught.exception))
        self.assertIn("converter diagnostic", self.session.snapshot_error or "")
        events = [
            json.loads(line)
            for line in ogent.RUNTIME_LOG_PATH.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events[-1]["event"], "word_view_failed")
        self.assertEqual(events[-1]["document_id"], self.session.document_id)

    def test_preview_client_status_is_logged_and_stale_identity_is_rejected(
        self,
    ) -> None:
        result = ogent.record_preview_client_status(
            self.session,
            "client-preview-1234",
            {
                "status": "ready",
                "document_id": self.session.document_id,
                "watch_generation": self.session.watch_generation,
                "message": "usable",
                "metrics": {
                    "text_length": 412,
                    "visible_visuals": 3,
                    "visible_paths": 22,
                    "document_height": 1400,
                },
            },
        )
        self.assertEqual(result["status"], "ready")
        log = ogent.RUNTIME_LOG_PATH.read_text(encoding="utf-8")
        self.assertIn('"event":"preview_client_status"', log)
        self.assertIn('"status":"ready"', log)

        with self.assertRaises(ogent.UserFacingError) as caught:
            ogent.record_preview_client_status(
                self.session,
                "client-preview-1234",
                {
                    "status": "ready",
                    "document_id": "stale-document",
                    "watch_generation": self.session.watch_generation,
                },
            )
        self.assertEqual(caught.exception.status, 409)

    def test_snapshot_pdf_endpoint_rejects_stale_revision(self) -> None:
        state = ogent.OgentState()
        state.sessions[self.session.session_id] = self.session
        ogent.STATE = state
        snapshot_root = ogent.WORK_ROOT / self.session.session_id / "word-view"
        snapshot_root.mkdir(parents=True)
        snapshot = snapshot_root / "cached.pdf"
        snapshot.write_bytes(b"%PDF-1.7\nendpoint-proof\n%%EOF")
        with self.session.lock:
            self.session.snapshot_path = snapshot
            self.session.snapshot_cache_key = "cache-proof"
            self.session.snapshot_document_id = self.session.document_id
            self.session.snapshot_document_revision = self.session.document_revision
            self.session.snapshot_package_sha256 = self.source_hash

        server = ogent.OgentServer((ogent.HOST, 0), ogent.OgentHandler)
        port = int(server.server_address[1])
        state.server_port = port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        query = urllib.parse.urlencode(
            {
                "s": self.session.session_id,
                "token": state.token,
            }
        )
        url = f"http://{ogent.HOST}:{port}/snapshot.pdf?{query}"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read().startswith(b"%PDF-"))
            with self.session.lock:
                self.session.document_revision += 1
            with self.assertRaises(urllib.error.HTTPError) as stale:
                urllib.request.urlopen(url, timeout=10)
            self.assertEqual(stale.exception.code, 404)
            stale.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

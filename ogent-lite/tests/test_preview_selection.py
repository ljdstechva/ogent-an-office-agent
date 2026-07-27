from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_preview_selection import (  # noqa: E402
    MAX_PREVIEW_SELECTION_TARGETS,
    SELECTION_PROTOCOL,
    SELECTION_PROTOCOL_VERSION,
    OfficeCLISelectionBroker,
    PreviewSelectionError,
    PreviewSelectionState,
    compact_excel_rectangle,
    post_watch_selection,
)


class FixedClock:
    def __call__(self) -> dt.datetime:
        return dt.datetime(2026, 3, 4, 5, 6, 7, tzinfo=dt.timezone.utc)


class PreviewSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = PreviewSelectionState("a1b2c3d4", clock=FixedClock())
        self.state.reset_for_watch(
            document_id="doc-12345678",
            document_name="report.docx",
            document_format="docx",
            revision=12,
        )

    def payload(self, selected: list[dict[str, object]]) -> dict[str, object]:
        return {
            "protocol": SELECTION_PROTOCOL,
            "version": SELECTION_PROTOCOL_VERSION,
            "type": "selection.changed",
            "channel_id": self.state.channel_id,
            "watch_id": self.state.watch_id,
            "document_id": self.state.document_id,
            "session_id": self.state.session_id,
            "revision": self.state.revision,
            "primary_path": selected[0]["path"] if selected else None,
            "selected": selected,
        }

    @staticmethod
    def resolver(paths: list[str]) -> list[dict[str, object]]:
        return [
            {
                "path": path,
                "type": "paragraph",
                "text": f"Server excerpt for {path}",
                "preview": f"Server label for {path}",
                "style": "Heading1" if path.endswith("[1]") else "Normal",
            }
            for path in paths
        ]

    def test_valid_event_is_accepted_with_server_generated_metadata(self) -> None:
        payload = self.payload(
            [
                {
                    "path": "/body/p[1]",
                    "kind": "script",
                    "label": "<img onerror=alert(1)>",
                    "excerpt": "FORGED CLIENT EXCERPT",
                    "order": 999,
                }
            ]
        )
        paths = self.state.validate_bridge_envelope(
            payload,
            event_origin="http://127.0.0.1:26320",
            expected_origin="http://127.0.0.1:26320",
            source_matches=True,
        )
        targets = self.state.apply_paths(paths, self.resolver)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].kind, "heading")
        self.assertEqual(targets[0].label, "Server label for /body/p[1]")
        self.assertEqual(
            targets[0].excerpt,
            "Server excerpt for /body/p[1]",
        )
        self.assertNotIn("FORGED", targets[0].excerpt)
        self.assertTrue(targets[0].primary)

    def test_wrong_origin_and_wrong_iframe_source_are_rejected(self) -> None:
        payload = self.payload([{"path": "/body/p[1]"}])
        with self.assertRaisesRegex(PreviewSelectionError, "origin mismatch"):
            self.state.validate_bridge_envelope(
                payload,
                event_origin="http://evil.invalid",
                expected_origin="http://127.0.0.1:26320",
                source_matches=True,
            )
        with self.assertRaisesRegex(PreviewSelectionError, "iframe mismatch"):
            self.state.validate_bridge_envelope(
                payload,
                event_origin="http://127.0.0.1:26320",
                expected_origin="http://127.0.0.1:26320",
                source_matches=False,
            )

    def test_wrong_channel_watch_document_session_and_revision_fail_closed(self) -> None:
        checks = {
            "channel_id": ("wrong-channel-1234567890", "channel"),
            "watch_id": ("wrong-watch", "generation"),
            "document_id": ("other-doc-12345678", "document"),
            "session_id": ("deadbeef", "session"),
            "revision": (11, "revision"),
        }
        for field, (value, expected) in checks.items():
            with self.subTest(field=field):
                payload = self.payload([{"path": "/body/p[1]"}])
                payload[field] = value
                with self.assertRaisesRegex(PreviewSelectionError, expected):
                    self.state.validate_bridge_envelope(
                        payload,
                        event_origin="http://127.0.0.1:26320",
                        expected_origin="http://127.0.0.1:26320",
                        source_matches=True,
                    )

    def test_malformed_arbitrary_and_cross_format_paths_are_rejected(self) -> None:
        for path in (
            "/../../secret.txt",
            "<script>alert(1)</script>",
            "/Sheet1/A1",
            "/body/p[1]/../../../settings",
            "",
        ):
            with self.subTest(path=path):
                payload = self.payload([{"path": path}])
                with self.assertRaisesRegex(PreviewSelectionError, "invalid"):
                    self.state.validate_bridge_envelope(
                        payload,
                        event_origin="http://127.0.0.1:26320",
                        expected_origin="http://127.0.0.1:26320",
                        source_matches=True,
                    )

    def test_duplicate_paths_are_deduplicated_and_order_is_preserved(self) -> None:
        payload = self.payload(
            [
                {"path": "/body/p[3]"},
                {"path": "/body/p[1]"},
                {"path": "/body/p[3]"},
                {"path": "/body/p[2]"},
            ]
        )
        paths = self.state.validate_bridge_envelope(
            payload,
            event_origin="http://127.0.0.1:26320",
            expected_origin="http://127.0.0.1:26320",
            source_matches=True,
        )
        targets = self.state.apply_paths(paths, self.resolver)
        self.assertEqual(
            [item.path for item in targets],
            ["/body/p[3]", "/body/p[1]", "/body/p[2]"],
        )
        self.assertEqual([item.order for item in targets], [0, 1, 2])

    def test_20_targets_are_accepted_and_target_21_is_not_retained(self) -> None:
        paths = [f"/body/p[{index}]" for index in range(1, 22)]
        resolved: list[list[str]] = []

        def bounded_resolver(selected: list[str]) -> list[dict[str, object]]:
            resolved.append(list(selected))
            return self.resolver(selected)

        targets = self.state.apply_paths(paths, bounded_resolver)
        self.assertEqual(len(targets), MAX_PREVIEW_SELECTION_TARGETS)
        self.assertEqual(targets[-1].path, "/body/p[20]")
        self.assertEqual(resolved, [paths[:20]])
        self.assertIn("limited to 20", self.state.limit_message or "")

    def test_excel_cells_are_compacted_into_one_rectangular_range(self) -> None:
        excel = PreviewSelectionState("a1b2c3d4", clock=FixedClock())
        excel.reset_for_watch(
            document_id="xlsx-12345678",
            document_name="data.xlsx",
            document_format="xlsx",
            revision=4,
        )
        paths = ["/Sheet1/B2", "/Sheet1/C2", "/Sheet1/B3", "/Sheet1/C3"]
        self.assertEqual(compact_excel_rectangle(paths), "/Sheet1/B2:C3")

        def range_resolver(values: list[str]) -> list[dict[str, object]]:
            return [
                {
                    "path": value,
                    "type": "range",
                    "text": "10 | 20 | 30 | 40",
                }
                for value in values
            ]

        targets = excel.apply_paths(paths, range_resolver)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].path, "/Sheet1/B2:C3")
        self.assertEqual(targets[0].kind, "range")
        self.assertEqual(targets[0].label, "Sheet1!B2:C3")

    def test_remove_clear_restart_and_revision_staleness(self) -> None:
        targets = self.state.apply_paths(
            ["/body/p[1]", "/body/p[2]"],
            self.resolver,
        )
        remaining = self.state.remove(targets[0].selection_id)
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0].primary)

        self.state.restart_watch()
        self.assertTrue(self.state.targets[0].stale)
        with self.assertRaisesRegex(PreviewSelectionError, "stale"):
            self.state.snapshot_for_send()

        self.state.reset_for_watch(
            document_id="doc-12345678",
            document_name="report.docx",
            document_format="docx",
            revision=12,
        )
        self.assertEqual(self.state.targets, [])
        self.state.apply_paths(["/body/p[1]"], self.resolver)
        self.state.advance_revision(13)
        self.assertTrue(self.state.targets[0].stale)
        self.state.clear()
        self.assertEqual(self.state.targets, [])

    def test_send_snapshot_is_immutable(self) -> None:
        self.state.apply_paths(["/body/p[1]", "/body/p[2]"], self.resolver)
        snapshot = self.state.snapshot_for_send()
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.state.clear()
        self.assertEqual(len(snapshot.targets), 2)
        self.assertEqual(snapshot.revision, 12)
        self.assertEqual(snapshot.targets[0].document_name, "report.docx")

    def test_claim_for_send_atomically_clears_composer_targets(self) -> None:
        self.state.apply_paths(["/body/p[1]"], self.resolver)
        snapshot = self.state.claim_for_send()
        self.assertIsNotNone(snapshot)
        self.assertEqual(self.state.targets, [])
        assert snapshot is not None
        self.assertEqual(snapshot.targets[0].path, "/body/p[1]")

    def test_broker_dispatches_only_selection_and_document_events(self) -> None:
        selections: list[list[str]] = []
        documents: list[dict[str, object]] = []
        broker = OfficeCLISelectionBroker(
            26320,
            on_selection=selections.append,
            on_document_event=documents.append,
        )
        broker._dispatch(
            json.dumps(
                {
                    "action": "selection-update",
                    "paths": ["/body/p[1]", "/body/p[2]"],
                }
            )
        )
        broker._dispatch(json.dumps({"action": "mark-update", "marks": []}))
        broker._dispatch(json.dumps({"action": "replace", "html": "ignored"}))
        broker._dispatch("not json")

        self.assertEqual(selections, [["/body/p[1]", "/body/p[2]"]])
        self.assertEqual(documents, [{"action": "replace", "html": "ignored"}])

    def test_broker_does_not_expire_an_idle_sse_stream(self) -> None:
        response = mock.MagicMock()
        response.readline.return_value = b""
        urlopen = mock.MagicMock(return_value=response)
        broker = OfficeCLISelectionBroker(
            26320,
            on_selection=lambda _paths: None,
            urlopen=urlopen,
        )

        broker._run()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:26320/events")
        self.assertNotIn("timeout", urlopen.call_args.kwargs)

    def test_native_selection_update_posts_only_to_fixed_loopback_endpoint(self) -> None:
        response = mock.MagicMock()
        response.status = 204
        response.__enter__.return_value = response
        with mock.patch(
            "ogent_preview_selection.urllib.request.urlopen",
            return_value=response,
        ) as urlopen:
            post_watch_selection(26320, ["/body/p[1]"])
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:26320/api/selection",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"paths": ["/body/p[1]"]},
        )


if __name__ == "__main__":
    unittest.main()

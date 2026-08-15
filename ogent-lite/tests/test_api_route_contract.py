from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

import ogent  # noqa: E402


GET_ROUTES = {
    "/",
    "/health",
    "/agent-capabilities",
    "/api/agent-capabilities",
    "/events",
    "/preview",
    "/preview/events",
    "/preview/control",
    "/snapshot.pdf",
}
POST_ROUTES = {
    "/agent-capabilities/refresh",
    "/api/agent-capabilities/refresh",
    "/chat",
    "/conversation/reset",
    "/open",
    "/pick",
    "/preview/ack",
    "/preview/api/selection",
    "/preview/api/send",
    "/preview/status",
    "/reference/clear",
    "/reference/forget",
    "/reference/remove",
    "/reference/upload",
    "/selection/bridge",
    "/selection/clear",
    "/selection/focus",
    "/selection/multi-mode",
    "/selection/remove",
    "/session/close",
    "/session/focus",
    "/settings/memory/clear",
    "/settings/recovery/delete-expired",
    "/settings/recovery/open-folder",
    "/shutdown",
    "/snapshot",
    "/stop",
    "/upload",
    "/watch/restart",
}


class ApiRouteContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_state = ogent.STATE
        self.state = ogent.OgentState()
        ogent.STATE = self.state
        self.session = ogent.SessionState("cafebabe")
        with self.state.registry_lock:
            self.state.sessions[self.session.session_id] = self.session
        self.server = ogent.OgentServer((ogent.HOST, 0), ogent.OgentHandler)
        self.port = int(self.server.server_address[1])
        self.state.server_port = self.port
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.state.shutdown_requested = True
        with self.session.condition:
            self.session.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        ogent.STATE = self.original_state
        self.assertFalse(self.thread.is_alive(), "route test server did not stop")

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        authorized: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers.update(
                {
                    "X-Ogent-Token": self.state.token,
                    "X-Ogent-Session": self.session.session_id,
                }
            )
        request = urllib.request.Request(
            f"http://{ogent.HOST}:{self.port}{path}",
            data=(
                json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
            ),
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read()
                return (
                    response.status,
                    json.loads(body.decode("utf-8")) if body else {},
                )
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_public_health_and_capability_routes_remain_available(self) -> None:
        health_status, health = self.request("/health")
        capability_status, capabilities = self.request("/api/agent-capabilities")

        self.assertEqual(health_status, 200)
        self.assertEqual(health["app"], ogent.APP_NAME)
        self.assertEqual(capability_status, 200)
        self.assertIn("providers", capabilities)

    def test_standard_post_routes_remain_token_gated(self) -> None:
        for path in (
            "/chat",
            "/conversation/reset",
            "/pick",
            "/reference/clear",
            "/selection/clear",
            "/snapshot",
            "/stop",
            "/watch/restart",
        ):
            with self.subTest(path=path):
                status, payload = self.request(path, method="POST")
                self.assertEqual(status, 403)
                self.assertEqual(payload["error"], "Forbidden.")

    def test_unknown_routes_remain_404(self) -> None:
        get_status, _ = self.request("/not-a-route")
        post_status, _ = self.request(
            "/not-a-route",
            method="POST",
            authorized=True,
        )
        self.assertEqual((get_status, post_status), (404, 404))

    def test_workspace_write_routes_are_owned_and_validate_identifiers(
        self,
    ) -> None:
        selection_path = f"/api/workspaces/{self.session.session_id}/document-selection"
        unauthorized, _ = self.request(
            selection_path,
            method="POST",
            payload={"node_ids": ["a" * 32]},
        )
        foreign, _ = self.request(
            "/api/workspaces/deadbeef/document-selection",
            method="POST",
            authorized=True,
            payload={"node_ids": ["a" * 32]},
        )
        invalid_selection, selection_error = self.request(
            selection_path,
            method="POST",
            authorized=True,
            payload={"node_ids": ["not-a-node"]},
        )
        invalid_undo, undo_error = self.request(
            f"/api/workspaces/{self.session.session_id}/undo",
            method="POST",
            authorized=True,
            payload={"changeset_id": "not-a-changeset"},
        )

        self.assertEqual(unauthorized, 403)
        self.assertEqual(foreign, 403)
        self.assertEqual(invalid_selection, 400)
        self.assertIn("valid map nodes", selection_error["error"])
        self.assertEqual(invalid_undo, 400)
        self.assertIn("change identifier", undo_error["error"])

    def test_document_map_selection_route_delegates_to_revision_resolver(
        self,
    ) -> None:
        node_id = "a" * 32
        result = {
            "message": "Focused the indexed target.",
            "preview_selection": {"targets": []},
        }
        with mock.patch.object(
            ogent,
            "select_indexed_document_nodes",
            return_value=result,
        ) as resolver:
            status, payload = self.request(
                (f"/api/workspaces/{self.session.session_id}/document-selection"),
                method="POST",
                authorized=True,
                payload={"node_ids": [node_id]},
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, result)
        resolver.assert_called_once_with(self.session, [node_id])

    def test_partition_resume_route_is_workspace_owned_and_delegates(
        self,
    ) -> None:
        prior_run_id = "a" * 32
        result = {
            "run_id": "b" * 32,
            "resumed_from_run_id": prior_run_id,
            "completed_partitions": 2,
            "partition_count": 5,
        }
        path = f"/api/workspaces/{self.session.session_id}/runs/{prior_run_id}/resume"
        with mock.patch.object(
            ogent,
            "resume_agent_run",
            return_value=result,
        ) as resume:
            status, payload = self.request(
                path,
                method="POST",
                authorized=True,
            )

        self.assertEqual(status, 202)
        self.assertEqual(payload, result)
        resume.assert_called_once_with(
            self.session,
            prior_run_id,
            client_id=None,
        )

    def test_unexpected_route_failure_hides_traceback_and_internal_path(
        self,
    ) -> None:
        prior_run_id = "a" * 32
        secret_path = r"C:\Users\person\Confidential\monitoring-report.docx"
        path = f"/api/workspaces/{self.session.session_id}/runs/{prior_run_id}/resume"
        with mock.patch.object(
            ogent,
            "resume_agent_run",
            side_effect=RuntimeError(f"failed while reading {secret_path}"),
        ):
            status, payload = self.request(
                path,
                method="POST",
                authorized=True,
            )

        serialized = json.dumps(payload)
        self.assertEqual(status, 500)
        self.assertEqual(
            payload["error"],
            "Ogent encountered an internal error. No internal path or "
            "traceback was exposed.",
        )
        self.assertNotIn(secret_path, serialized)
        self.assertNotIn("RuntimeError", serialized)

    def test_undo_route_reports_verified_success_and_advances_revision(
        self,
    ) -> None:
        changeset_id = "c" * 32
        document = OGENT_DIR / "tests" / "route-undo-fixture.docx"
        with self.session.lock:
            self.session.active_doc = document
            self.session.document_id = "d" * 32
            self.session.document_revision = 7
        service = mock.Mock()
        service.undo.return_value = {
            "changeset_id": changeset_id,
            "affected_paths": ["/body/p[1]"],
            "assertions": {"officecli_validate": True},
            "can_undo": False,
            "undone": True,
        }
        gateway = mock.Mock()
        gateway.validate.return_value = SimpleNamespace(exit_code=0)
        gateway.safe_result.return_value = {
            "success": True,
            "exit_code": 0,
        }
        with (
            mock.patch.object(ogent, "CHANGE_REVIEW_SERVICE", service),
            mock.patch.object(ogent, "OFFICECLI_TYPED_GATEWAY", gateway),
            mock.patch.object(
                ogent,
                "package_sha256",
                return_value="b" * 64,
            ),
            mock.patch.object(
                ogent,
                "advance_document_revision",
                return_value=(8, True),
            ) as advance,
            mock.patch.object(ogent, "ensure_watch"),
        ):
            status, payload = self.request(
                f"/api/workspaces/{self.session.session_id}/undo",
                method="POST",
                authorized=True,
                payload={"changeset_id": changeset_id},
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["change_review"]["undone"])
        self.assertEqual(payload["document_revision"], 8)
        service.undo.assert_called_once()
        advance.assert_called_once_with(
            self.session,
            document,
            "b" * 64,
        )
        with self.session.lock:
            self.assertEqual(self.session.run_status, "idle")
            self.assertEqual(
                self.session.last_run_outcome,
                "edit_completed",
            )

    def test_extracted_route_sources_keep_the_characterized_inventory(self) -> None:
        get_source = (OGENT_DIR / "ogent_app" / "api" / "http_get_routes.py").read_text(
            encoding="utf-8"
        )
        post_source = (
            OGENT_DIR / "ogent_app" / "api" / "http_post_routes.py"
        ).read_text(encoding="utf-8")

        for path in GET_ROUTES:
            with self.subTest(method="GET", path=path):
                self.assertIn(f'"{path}"', get_source)
        for path in POST_ROUTES:
            with self.subTest(method="POST", path=path):
                self.assertIn(f'"{path}"', post_source)


if __name__ == "__main__":
    unittest.main()

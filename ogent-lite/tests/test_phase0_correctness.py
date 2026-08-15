from __future__ import annotations

import http.client
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
OGENT_PATH = OGENT_DIR / "ogent.py"
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

SPEC = importlib.util.spec_from_file_location("ogent_phase0_under_test", OGENT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {OGENT_PATH}")
ogent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ogent)


class AttemptSignallingLock:
    def __init__(self) -> None:
        self.inner = threading.RLock()
        self.attempted = threading.Event()

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        self.attempted.set()
        return self.inner.acquire(*args, **kwargs)

    def release(self) -> None:
        self.inner.release()

    def __enter__(self) -> AttemptSignallingLock:
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


class Phase0CorrectnessTests(unittest.TestCase):
    @staticmethod
    def _fixture_selection() -> Any:
        return ogent.AgentSelection(
            provider_id="codex",
            model="fixture",
            effort="automatic",
        )

    def test_backend_log_rotates_at_configured_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ogent.log"
            quotas = mock.Mock(max_log_bytes=240, log_backup_count=2)
            with (
                mock.patch.object(ogent, "RUNTIME_LOG_PATH", path),
                mock.patch.object(ogent, "RESOURCE_QUOTAS", quotas),
            ):
                for index in range(20):
                    ogent.backend_log("rotation.test", sequence=index)

            self.assertTrue(path.is_file())
            self.assertLessEqual(path.stat().st_size, 240)
            self.assertTrue(path.with_name("ogent.log.1").is_file())
            self.assertFalse(path.with_name("ogent.log.3").exists())

    def test_backend_log_rejects_content_paths_and_free_form_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ogent.log"
            secret = "PRIVATE-DOCUMENT-CONTENT-7429"
            internal_path = rf"C:\Users\person\Reports\{secret}.docx"
            with mock.patch.object(ogent, "RUNTIME_LOG_PATH", path):
                ogent.backend_log(
                    "content.safety",
                    session_id="deadbeef",
                    status="ready",
                    document_text=secret,
                    document_name=f"{secret}.docx",
                    message=f"Rendered {secret}",
                    internal_path=internal_path,
                    error=RuntimeError(f"failed at {internal_path}"),
                    error_type="RuntimeError",
                )

            payload = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, payload)
            self.assertNotIn(internal_path, payload)
            self.assertNotIn("document_text", payload)
            self.assertNotIn("document_name", payload)
            self.assertNotIn("internal_path", payload)
            self.assertNotIn('"error"', payload)
            self.assertIn('"session_id":"deadbeef"', payload)
            self.assertIn('"error_type":"RuntimeError"', payload)

    def test_http_sse_reconnect_and_chat_send_cannot_deadlock(self) -> None:
        original_state = ogent.STATE
        state = ogent.OgentState()
        ogent.STATE = state
        server = ogent.OgentServer((ogent.HOST, 0), ogent.OgentHandler)
        port = int(server.server_address[1])
        state.server_port = port
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="phase0-http-server",
            daemon=True,
        )
        server_thread.start()
        session = ogent.SessionState("deadbeef")
        with state.registry_lock:
            state.sessions[session.session_id] = session
        reconnect_entered_snapshot = threading.Event()
        send_holds_reference_lock = threading.Event()
        route_errors: list[BaseException] = []
        reconnect_results: list[int] = []
        send_results: list[tuple[int, dict[str, Any]]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            document = Path(temp_dir) / "working.docx"
            document.write_bytes(b"office-package")
            with session.lock:
                session.active_doc = document
                session.active_source = document

            original_public_snapshot = session.public_snapshot
            original_handle_chat = ogent.handle_chat_message

            def coordinated_public_snapshot(
                include_watch_probe: bool = True,
            ) -> dict[str, Any]:
                reconnect_entered_snapshot.set()
                if not send_holds_reference_lock.wait(timeout=3):
                    raise AssertionError("Send route never acquired reference_lock.")
                return original_public_snapshot(include_watch_probe=include_watch_probe)

            def coordinated_handle_chat(
                current: Any,
                *args: Any,
                **kwargs: Any,
            ) -> tuple[int, dict[str, Any]]:
                with current.reference_lock:
                    send_holds_reference_lock.set()
                    return original_handle_chat(current, *args, **kwargs)

            session.public_snapshot = coordinated_public_snapshot  # type: ignore[method-assign]

            def reconnect_route() -> None:
                connection = http.client.HTTPConnection(
                    ogent.HOST,
                    port,
                    timeout=5,
                )
                try:
                    connection.request(
                        "GET",
                        (
                            f"/events?s={session.session_id}"
                            f"&token={state.token}"
                            "&client=reconnect-client"
                        ),
                    )
                    response = connection.getresponse()
                    reconnect_results.append(response.status)
                    while True:
                        line = response.readline()
                        if line in {b"", b"\n", b"\r\n"}:
                            break
                except BaseException as exc:  # pragma: no cover - assertion below
                    route_errors.append(exc)
                finally:
                    connection.close()

            def send_route() -> None:
                request = urllib.request.Request(
                    f"http://{ogent.HOST}:{port}/chat",
                    data=json.dumps(
                        {
                            "message": "Review without editing.",
                            "provider": "codex",
                            "model": "fixture",
                            "effort": "automatic",
                        }
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Ogent-Token": state.token,
                        "X-Ogent-Session": session.session_id,
                        "X-Ogent-Client": "send-client",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(
                        request,
                        timeout=5,
                    ) as response:
                        send_results.append(
                            (
                                response.status,
                                json.loads(response.read().decode("utf-8")),
                            )
                        )
                except BaseException as exc:  # pragma: no cover - assertion below
                    route_errors.append(exc)

            reconnect_thread = threading.Thread(
                target=reconnect_route,
                name="phase0-reconnect-route",
                daemon=True,
            )
            send_thread = threading.Thread(
                target=send_route,
                name="phase0-send-route",
                daemon=True,
            )
            try:
                with (
                    mock.patch.object(
                        ogent,
                        "validate_agent_selection",
                        return_value=self._fixture_selection(),
                    ),
                    mock.patch.object(ogent, "_agent_worker", return_value=None),
                    mock.patch.object(
                        ogent,
                        "handle_chat_message",
                        side_effect=coordinated_handle_chat,
                    ),
                ):
                    reconnect_thread.start()
                    self.assertTrue(
                        reconnect_entered_snapshot.wait(timeout=3),
                        "SSE route did not enter reconnect snapshot capture.",
                    )
                    send_thread.start()
                    self.assertTrue(
                        send_holds_reference_lock.wait(timeout=3),
                        "Send route did not acquire reference_lock.",
                    )
                    reconnect_thread.join(timeout=5)
                    send_thread.join(timeout=5)

                self.assertFalse(
                    reconnect_thread.is_alive(),
                    "SSE reconnect deadlocked against Send.",
                )
                self.assertFalse(
                    send_thread.is_alive(),
                    "Send deadlocked against SSE reconnect.",
                )
                self.assertEqual(route_errors, [])
                self.assertEqual(reconnect_results, [200])
                self.assertEqual(send_results[0][0], 202)
                self.assertEqual(send_results[0][1]["mode"], "review")
            finally:
                state.shutdown_requested = True
                with session.condition:
                    session.condition.notify_all()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)
                ogent.STATE = original_state

        self.assertFalse(server_thread.is_alive(), "HTTP test server did not stop.")

    def test_concurrency_stress_reconnect_and_send_paths_do_not_deadlock(
        self,
    ) -> None:
        session_count = 12
        iterations = 150
        barrier = threading.Barrier((session_count * 2) + 1)
        errors: list[BaseException] = []
        send_statuses: list[int] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            document = Path(temp_dir) / "working.docx"
            document.write_bytes(b"office-package")
            sessions = [
                ogent.SessionState(f"stress-{index}") for index in range(session_count)
            ]
            for session in sessions:
                with session.lock:
                    session.active_doc = document
                    session.active_source = document

            def reconnect_worker(current: Any) -> None:
                try:
                    barrier.wait(timeout=5)
                    for _ in range(iterations):
                        cursor, snapshot = ogent.capture_sse_snapshot(current)
                        if cursor > snapshot["sequence"]:
                            raise AssertionError(
                                "SSE cursor exceeded snapshot sequence."
                            )
                except BaseException as exc:  # pragma: no cover - assertion below
                    errors.append(exc)

            def send_worker(current: Any) -> None:
                try:
                    barrier.wait(timeout=5)
                    for _ in range(iterations):
                        with current.reference_lock:
                            with current.lock:
                                if current.closed:
                                    raise AssertionError("Fixture session closed.")
                                current.preview_selection.snapshot_for_send()
                    status, _result = ogent.handle_chat_message(
                        current,
                        "Review without editing.",
                        "codex",
                        "fixture",
                        "automatic",
                    )
                    send_statuses.append(status)
                except BaseException as exc:  # pragma: no cover - assertion below
                    errors.append(exc)

            threads: list[threading.Thread] = []
            for session in sessions:
                threads.extend(
                    (
                        threading.Thread(
                            target=reconnect_worker,
                            args=(session,),
                            daemon=True,
                        ),
                        threading.Thread(
                            target=send_worker,
                            args=(session,),
                            daemon=True,
                        ),
                    )
                )
            with (
                mock.patch.object(
                    ogent,
                    "validate_agent_selection",
                    return_value=self._fixture_selection(),
                ),
                mock.patch.object(ogent, "_agent_worker", return_value=None),
            ):
                for thread in threads:
                    thread.start()
                barrier.wait(timeout=5)
                deadline = time.monotonic() + 10
                for thread in threads:
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))

            self.assertFalse(
                any(thread.is_alive() for thread in threads),
                "Concurrency stress left a reconnect or Send thread deadlocked.",
            )
            self.assertEqual(errors, [])
            self.assertEqual(send_statuses, [202] * session_count)

    def test_sse_snapshot_never_holds_session_lock_while_waiting_for_references(
        self,
    ) -> None:
        session = ogent.SessionState("fixture")
        reference_lock = AttemptSignallingLock()
        session.reference_lock = reference_lock
        reference_lock.acquire()
        reference_lock.attempted.clear()
        result: list[tuple[int, dict[str, Any]]] = []

        worker = threading.Thread(
            target=lambda: result.append(ogent.capture_sse_snapshot(session)),
            daemon=True,
        )
        worker.start()
        self.assertTrue(reference_lock.attempted.wait(timeout=1))

        acquired_state_lock = session.lock.acquire(timeout=1)
        self.assertTrue(
            acquired_state_lock,
            "SSE reconnect held session.lock while blocked on reference_lock.",
        )
        if acquired_state_lock:
            session.lock.release()
        reference_lock.release()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertLessEqual(result[0][0], result[0][1]["sequence"])

    def test_snapshot_never_holds_session_lock_while_waiting_for_selection(
        self,
    ) -> None:
        session = ogent.SessionState("fixture")
        selection_lock = AttemptSignallingLock()
        session.preview_selection.lock = selection_lock
        selection_lock.acquire()
        selection_lock.attempted.clear()
        result: list[tuple[int, dict[str, Any]]] = []

        worker = threading.Thread(
            target=lambda: result.append(ogent.capture_sse_snapshot(session)),
            daemon=True,
        )
        worker.start()
        self.assertTrue(selection_lock.attempted.wait(timeout=1))

        acquired_state_lock = session.lock.acquire(timeout=1)
        self.assertTrue(
            acquired_state_lock,
            "Snapshot held session.lock while blocked on preview selection.",
        )
        if acquired_state_lock:
            session.lock.release()
        selection_lock.release()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)

    def test_handle_chat_persists_exact_200k_raw_turn_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ogent.SessionMemoryStore(
                Path(temp_dir) / "memory",
                launch_id="1" * 32,
            )
            store.initialize()
            memory = store.create("a1b2c3d4")
            session = ogent.SessionState("a1b2c3d4", memory=memory)
            document = Path(temp_dir) / "working.docx"
            document.write_bytes(b"office-package")
            session.active_doc = document
            session.active_source = document
            finished = threading.Event()
            middle = "x" * 199_966
            message = "  Review without editing:\r\n" + middle + "\r\nEND  "
            self.assertEqual(len(message), 200_000)

            def fake_worker(*args: Any, **_kwargs: Any) -> None:
                current = args[0]
                run_id = args[7]
                ogent._finish_session_run(
                    current,
                    run_id,
                    "completed",
                    kind="codex",
                )
                finished.set()

            with (
                mock.patch.object(
                    ogent,
                    "validate_agent_selection",
                    return_value=ogent.AgentSelection(
                        provider_id="codex",
                        model="fixture",
                        effort="automatic",
                    ),
                ),
                mock.patch.object(
                    ogent,
                    "_agent_worker",
                    side_effect=fake_worker,
                ),
            ):
                status, _result = ogent.handle_chat_message(
                    session,
                    message,
                    "codex",
                    "fixture",
                    "automatic",
                )
                self.assertEqual(status, 202)
                self.assertTrue(finished.wait(timeout=2))

            self.assertEqual(memory.turns[0].text, message)
            self.assertEqual(session.transcript[0]["text"], message)

    def test_persistence_failure_leaves_send_retryable_and_restores_selection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ogent.SessionMemoryStore(
                Path(temp_dir) / "memory",
                launch_id="2" * 32,
            )
            store.initialize()
            memory = store.create("b1c2d3e4")
            session = ogent.SessionState("b1c2d3e4", memory=memory)
            document = Path(temp_dir) / "working.docx"
            document.write_bytes(b"office-package")
            with session.lock:
                session.active_doc = document
                session.active_source = document
                session.document_id = "doc-12345678"
                session.document_revision = 7
            session.preview_selection.reset_for_watch(
                document_id="doc-12345678",
                document_name=document.name,
                document_format="docx",
                revision=7,
            )
            session.preview_selection.apply_paths(
                ["/body/p[2]"],
                lambda paths: [
                    {
                        "path": path,
                        "type": "paragraph",
                        "text": "Selected paragraph",
                        "preview": "Selected paragraph",
                        "style": "Normal",
                    }
                    for path in paths
                ],
            )
            original_selection_id = session.preview_selection.targets[0].selection_id

            with (
                mock.patch.object(
                    ogent,
                    "validate_agent_selection",
                    return_value=self._fixture_selection(),
                ),
                mock.patch.object(
                    memory,
                    "_persist",
                    side_effect=OSError("disk unavailable"),
                ),
            ):
                with self.assertRaises(ogent.UserFacingError) as caught:
                    ogent.handle_chat_message(
                        session,
                        "Review the selection without editing.",
                        "codex",
                        "fixture",
                        "automatic",
                    )

            self.assertEqual(caught.exception.status, 500)
            self.assertIn("Nothing was sent", str(caught.exception))
            with session.lock:
                self.assertEqual(session.run_status, "idle")
                self.assertEqual(session.last_run_outcome, "neutral")
                self.assertIsNone(session.run_id)
                self.assertIsNone(session.run_contract)
                self.assertTrue(session.run_complete.is_set())
                self.assertEqual(session.transcript, [])
            self.assertEqual(memory.sequence, 0)
            self.assertEqual(memory.turns, [])
            self.assertEqual(
                [target.selection_id for target in session.preview_selection.targets],
                [original_selection_id],
            )

    def test_edit_without_package_change_is_not_reported_as_success(self) -> None:
        for suffix in (".docx", ".xlsx", ".pptx"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp_dir:
                document = Path(temp_dir) / f"working{suffix}"
                document.write_bytes(b"unchanged-office-package")
                session = ogent.SessionState(f"fixture-{suffix[1:]}")
                session.active_doc = document
                session.active_source = document
                session.document_id = "document-id"
                session.document_revision = 1
                session.preview_sync.activate_watch(
                    "document-id",
                    "a" * 32,
                )
                session.run_id = "run-id"
                session.run_status = "starting"
                session.run_complete.clear()
                timing = ogent.RunTiming(
                    provider="codex",
                    model="fixture",
                    effort="automatic",
                    context_mode="fresh",
                    prompt_bytes=0,
                    attachment_count=0,
                    materialized_bytes=0,
                )

                with (
                    mock.patch.object(ogent, "ensure_watch", return_value=None),
                    mock.patch.object(
                        ogent,
                        "_run_codex_once",
                        return_value=(0, None, "Edited successfully.", []),
                    ),
                    mock.patch.object(
                        ogent,
                        "run_quiet",
                        return_value=subprocess.CompletedProcess([], 0, "{}", ""),
                    ),
                ):
                    ogent._agent_worker(
                        session,
                        "Edit the document title.",
                        document,
                        document,
                        "codex",
                        "fixture",
                        "automatic",
                        "run-id",
                        [],
                        None,
                        None,
                        timing,
                    )

                self.assertEqual(session.last_run_outcome, "no_change")
                self.assertEqual(session.run_status, "idle")
                self.assertEqual(session.transcript[-1]["text"], "No change was made.")
                terminal = [
                    event
                    for event in session.events
                    if event["type"] == "run"
                    and event["data"].get("run_id") == "run-id"
                ][-1]
                self.assertEqual(terminal["data"]["outcome"], "no_change")

    def test_analysis_run_is_read_only_and_can_complete_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            document = Path(temp_dir) / "working.docx"
            document.write_bytes(b"unchanged-office-package")
            session = ogent.SessionState("fixture-analysis")
            session.active_doc = document
            session.active_source = document
            session.document_id = "document-id"
            session.document_revision = 1
            session.preview_sync.activate_watch(
                "document-id",
                "a" * 32,
            )
            session.run_id = "run-id"
            session.run_status = "starting"
            session.run_complete.clear()
            captured: dict[str, Any] = {}
            timing = ogent.RunTiming(
                provider="codex",
                model="fixture",
                effort="automatic",
                context_mode="fresh",
                prompt_bytes=0,
                attachment_count=0,
                materialized_bytes=0,
            )

            def fake_run(
                *_args: Any, **kwargs: Any
            ) -> tuple[int, None, str, list[str]]:
                captured.update(kwargs)
                return 0, None, "Review complete.", []

            with (
                mock.patch.object(ogent, "ensure_watch", return_value=None),
                mock.patch.object(ogent, "_run_codex_once", side_effect=fake_run),
                mock.patch.object(
                    ogent,
                    "run_quiet",
                    return_value=subprocess.CompletedProcess([], 0, "{}", ""),
                ),
            ):
                ogent._agent_worker(
                    session,
                    "Review the document without editing it.",
                    document,
                    document,
                    "codex",
                    "fixture",
                    "automatic",
                    "run-id",
                    [],
                    None,
                    None,
                    timing,
                )

            self.assertEqual(captured["sandbox"], "read-only")
            self.assertFalse(captured["allow_document_mutation"])
            self.assertEqual(session.last_run_outcome, "analysis_completed")
            self.assertEqual(session.transcript[-1]["text"], "Review complete.")

    def test_mutated_edit_has_distinct_edit_completed_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            document = Path(temp_dir) / "working.xlsx"
            document.write_bytes(b"before-office-package")
            session = ogent.SessionState("fixture-edit")
            session.active_doc = document
            session.active_source = document
            session.document_id = "document-id"
            session.document_revision = 1
            session.run_id = "run-id"
            session.run_status = "starting"
            session.run_complete.clear()
            timing = ogent.RunTiming(
                provider="codex",
                model="fixture",
                effort="automatic",
                context_mode="fresh",
                prompt_bytes=0,
                attachment_count=0,
                materialized_bytes=0,
            )

            def fake_run(
                *_args: Any, **kwargs: Any
            ) -> tuple[int, None, str, list[str]]:
                kwargs["office_document"].write_bytes(b"after-office-package")
                return 0, None, "Edit complete.", []

            with (
                mock.patch.object(ogent, "ensure_watch", return_value=None),
                mock.patch.object(ogent, "_run_codex_once", side_effect=fake_run),
                mock.patch.object(
                    ogent,
                    "run_quiet",
                    return_value=subprocess.CompletedProcess([], 0, "{}", ""),
                ),
            ):
                ogent._agent_worker(
                    session,
                    "Change cell A1 to Approved.",
                    document,
                    document,
                    "codex",
                    "fixture",
                    "automatic",
                    "run-id",
                    [],
                    None,
                    None,
                    timing,
                )

            self.assertEqual(session.last_run_outcome, "edit_completed")
            terminal = [
                event
                for event in session.events
                if event["type"] == "run" and event["data"].get("run_id") == "run-id"
            ][-1]
            self.assertEqual(terminal["data"]["outcome"], "edit_completed")


if __name__ == "__main__":
    unittest.main()

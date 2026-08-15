"""Manual live acceptance for provider isolation, Stop, and references.

This file is excluded from unittest discovery because it invokes real
authenticated agent CLIs. Model IDs are always chosen from the live catalogs.
Source documents and the reference source are read-only inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


OGENT_PATH = Path(__file__).resolve().parents[1] / "ogent.py"
AUTOMATIC = "automatic"


def load_ogent() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ogent_live_isolation_test",
        OGENT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {OGENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_for_run(session: Any, provider_id: str, timeout: float) -> None:
    if not session.run_complete.wait(timeout=timeout):
        raise TimeoutError(f"{provider_id} live run did not finish in time.")
    with session.lock:
        status = session.run_status
        error = session.last_error
    if status != "idle":
        raise RuntimeError(error or f"{provider_id} run ended with {status}.")


def choose_selection(ogent: Any, provider_id: str) -> Any:
    catalog = ogent.AGENT_CATALOG.refresh_now(provider_id)
    if catalog.status != "ready" or catalog.stale or not catalog.models:
        raise RuntimeError(
            catalog.warning or f"{provider_id} has no live model catalog."
        )
    model = next(
        (item for item in catalog.models if item.is_default),
        catalog.models[0],
    )
    return ogent.validate_agent_selection(
        provider_id,
        model.id,
        AUTOMATIC,
    )


def preview_text(ogent: Any, session: Any) -> str:
    with session.lock:
        port = session.watch_port
    if not port:
        raise RuntimeError("A live document session has no preview port.")
    with urllib.request.urlopen(
        f"http://{ogent.HOST}:{port}/",
        timeout=20,
    ) as response:
        return response.read().decode("utf-8", errors="replace")


def start_document_edit(
    ogent: Any,
    session: Any,
    source: Path,
    selection: Any,
    marker: str,
) -> str:
    ogent.dispatch_open_path(session, str(source))
    return ogent.start_agent_run(
        session,
        (
            "Use OfficeCLI to append one Normal paragraph containing exactly "
            f"{marker}. Do not change existing text. Match the preceding Normal "
            "paragraph's formatting, including its first-line indent, size, and "
            "spacing. Verify the marker and validate the working document."
        ),
        selection.model,
        selection.effort,
        selection.provider_id,
        selection=selection,
    )


def live_stop(
    ogent: Any,
    state: Any,
    source: Path,
    selection: Any,
    timeout: float,
) -> dict[str, Any]:
    provider = ogent.AGENT_PROVIDER_BY_ID[selection.provider_id]
    original_run_agent = provider.run_agent
    process_started = threading.Event()
    process_box: list[Any] = []

    def wrapped_run_agent(request: Any, **kwargs: Any) -> Any:
        original_on_process = kwargs["on_process"]

        def on_process(process: Any) -> None:
            original_on_process(process)
            process_box.append(process)
            process_started.set()

        kwargs["on_process"] = on_process
        return original_run_agent(request, **kwargs)

    session = state.create_session()
    provider.run_agent = wrapped_run_agent
    try:
        ogent.dispatch_open_path(session, str(source))
        ogent.start_agent_run(
            session,
            "Read the working document carefully and report its structure. Do not edit it.",
            selection.model,
            selection.effort,
            selection.provider_id,
            selection=selection,
        )
        if not process_started.wait(timeout=60):
            raise TimeoutError(
                f"{selection.provider_id} did not start a cancellable process."
            )
        stop_returned = ogent.stop_active_run(session)
        if not session.run_complete.wait(timeout=timeout):
            raise TimeoutError(f"{selection.provider_id} did not stop in time.")
        with session.lock:
            status = session.run_status
        process_stopped = bool(process_box and process_box[0].poll() is not None)
        if not stop_returned or status != "stopped" or not process_stopped:
            raise RuntimeError(
                f"{selection.provider_id} Stop did not terminate its process."
            )
        return {
            "stopReturned": stop_returned,
            "runStatus": status,
            "processExited": process_stopped,
        }
    finally:
        provider.run_agent = original_run_agent
        ogent.close_session(session)


def upload_reference(
    ogent: Any,
    state: Any,
    session: Any,
    source: Path,
) -> Path:
    request = urllib.request.Request(
        f"http://{ogent.HOST}:{state.server_port}/reference/upload",
        data=source.read_bytes(),
        headers={
            "X-Ogent-Token": state.token,
            "X-Ogent-Session": session.session_id,
            "X-Ogent-Filename": urllib.parse.quote(source.name, safe=""),
            "Content-Type": "application/octet-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if response.status != 201 or payload["attachment"]["kind"] != "Text":
        raise RuntimeError("The live reference upload was not accepted as text.")
    with session.reference_lock:
        return session.pending_references[0].source_path


def live_reference(
    ogent: Any,
    state: Any,
    document_source: Path,
    reference_source: Path,
    selection: Any,
    marker: str,
    timeout: float,
) -> dict[str, Any]:
    session = state.create_session()
    reference_hash = sha256(reference_source)
    try:
        stored_copy = upload_reference(
            ogent,
            state,
            session,
            reference_source,
        )
        ogent.start_agent_run(
            session,
            (
                "Read the temporary reference and report its exact unique "
                f"marker. The expected marker starts with {marker.split('-')[0]}."
            ),
            selection.model,
            selection.effort,
            selection.provider_id,
            selection=selection,
        )
        wait_for_run(session, selection.provider_id, timeout)
        with session.lock:
            reference_messages = [item["text"] for item in session.transcript]
            provider_context_after_reference = (
                session.codex_thread_id
                if selection.provider_id == "codex"
                else session.claude_session_id
            )
        if not any(marker in text for text in reference_messages):
            raise RuntimeError("The provider did not report the reference marker.")
        if provider_context_after_reference is not None:
            raise RuntimeError("A reference run persisted a provider context.")
        if stored_copy.parent.exists():
            raise RuntimeError("The temporary reference copy was not deleted.")
        if sha256(reference_source) != reference_hash:
            raise RuntimeError("The reference source changed.")

        ogent.dispatch_open_path(session, str(document_source))
        transcript_start = len(session.transcript)
        ogent.start_agent_run(
            session,
            (
                "Reply exactly NO_PRIOR_REFERENCE_CONTEXT. Do not inspect other "
                "files and do not infer a prior temporary-reference value."
            ),
            selection.model,
            selection.effort,
            selection.provider_id,
            selection=selection,
        )
        wait_for_run(session, selection.provider_id, timeout)
        with session.lock:
            next_messages = [
                item["text"]
                for item in session.transcript[transcript_start:]
                if item.get("role") == "assistant"
            ]
            provider_context_after_normal = (
                session.codex_thread_id
                if selection.provider_id == "codex"
                else session.claude_session_id
            )
        joined = "\n".join(next_messages)
        if (
            "NO_PRIOR_REFERENCE_CONTEXT" not in joined
            or marker in joined
            or provider_context_after_normal is None
        ):
            raise RuntimeError(
                "The next normal provider context was not clean and resumable."
            )
        return {
            "provider": selection.provider_id,
            "sourceHashUnchanged": True,
            "temporaryCopyDeleted": True,
            "referenceRunNonResumable": True,
            "nextNormalRunCreatedContext": True,
            "nextNormalRunExcludedMarker": True,
        }
    finally:
        ogent.close_session(session)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-source", required=True, type=Path)
    parser.add_argument("--claude-source", required=True, type=Path)
    parser.add_argument("--reference-source", required=True, type=Path)
    parser.add_argument("--reference-marker", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=360)
    args = parser.parse_args()

    sources = {
        "codex": args.codex_source.expanduser().resolve(strict=True),
        "claude": args.claude_source.expanduser().resolve(strict=True),
    }
    reference_source = args.reference_source.expanduser().resolve(strict=True)
    source_hashes = {
        provider_id: sha256(source) for provider_id, source in sources.items()
    }
    ogent = load_ogent()
    state = ogent.OgentState()
    ogent.STATE = state
    server = ogent.OgentServer((ogent.HOST, 0), ogent.OgentHandler)
    state.server_port = int(server.server_address[1])
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="ogent-live-isolation-http",
        daemon=True,
    )
    server_thread.start()
    sessions: dict[str, Any] = {}
    try:
        selections = {
            provider_id: choose_selection(ogent, provider_id)
            for provider_id in ("codex", "claude")
        }
        sessions = {
            provider_id: state.create_session() for provider_id in ("codex", "claude")
        }
        markers = {
            "codex": "CODEX-ISOLATION-V090-PASS",
            "claude": "CLAUDE-ISOLATION-V090-PASS",
        }
        run_ids = {
            provider_id: start_document_edit(
                ogent,
                sessions[provider_id],
                sources[provider_id],
                selections[provider_id],
                markers[provider_id],
            )
            for provider_id in ("codex", "claude")
        }
        for provider_id in ("codex", "claude"):
            wait_for_run(
                sessions[provider_id],
                provider_id,
                args.timeout_seconds,
            )

        previews = {
            provider_id: preview_text(ogent, sessions[provider_id])
            for provider_id in ("codex", "claude")
        }
        for provider_id, other_id in (("codex", "claude"), ("claude", "codex")):
            if (
                markers[provider_id] not in previews[provider_id]
                or markers[other_id] in previews[provider_id]
            ):
                raise RuntimeError("Live document previews were not isolated.")
            if sha256(sources[provider_id]) != source_hashes[provider_id]:
                raise RuntimeError(f"{provider_id} source document changed.")

        with sessions["codex"].lock:
            codex_context = sessions["codex"].codex_thread_id
            codex_cross_context = sessions["codex"].claude_session_id
            codex_working = sessions["codex"].active_doc
            codex_port = sessions["codex"].watch_port
        with sessions["claude"].lock:
            claude_context = sessions["claude"].claude_session_id
            claude_cross_context = sessions["claude"].codex_thread_id
            claude_working = sessions["claude"].active_doc
            claude_port = sessions["claude"].watch_port
        if (
            not codex_context
            or not claude_context
            or codex_cross_context
            or claude_cross_context
            or codex_context == claude_context
            or codex_working == claude_working
            or codex_port == claude_port
        ):
            raise RuntimeError("Provider or document session isolation failed.")

        isolation_result = {
            "runIdsDistinct": run_ids["codex"] != run_ids["claude"],
            "workingDocumentsDistinct": codex_working != claude_working,
            "watchPortsDistinct": codex_port != claude_port,
            "providerContextsDistinct": codex_context != claude_context,
            "contextIdsNeverCrossed": not (codex_cross_context or claude_cross_context),
            "sourceHashesUnchanged": True,
            "livePreviewsIsolated": True,
            "workingDocuments": {
                "codex": str(codex_working),
                "claude": str(claude_working),
            },
        }

        for session in sessions.values():
            ogent.close_session(session)
        sessions = {}

        stop_result = {
            provider_id: live_stop(
                ogent,
                state,
                sources[provider_id],
                selections[provider_id],
                args.timeout_seconds,
            )
            for provider_id in ("codex", "claude")
        }
        reference_result = live_reference(
            ogent,
            state,
            sources["claude"],
            reference_source,
            selections["claude"],
            args.reference_marker,
            args.timeout_seconds,
        )
        if any(
            sha256(sources[provider_id]) != source_hashes[provider_id]
            for provider_id in ("codex", "claude")
        ):
            raise RuntimeError("A source document changed during later live tests.")
        print(
            json.dumps(
                {
                    "isolation": isolation_result,
                    "stop": stop_result,
                    "reference": reference_result,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        for session in list(sessions.values()):
            ogent.close_session(session)
        for session in list(state.sessions.values()):
            ogent.close_session(session)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=10)
        ogent.AGENT_CATALOG.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

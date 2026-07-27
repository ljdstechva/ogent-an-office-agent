"""Manual live test: let one installed agent CLI edit an Ogent working DOCX.

This is intentionally excluded from unittest discovery because it invokes a
real authenticated model. It never edits the supplied source document.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import urllib.request
from pathlib import Path


OGENT_PATH = Path(__file__).resolve().parents[1] / "ogent.py"


def load_ogent() -> object:
    spec = importlib.util.spec_from_file_location("ogent_live_provider_test", OGENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {OGENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True, choices=("codex", "claude"))
    parser.add_argument("--model")
    parser.add_argument("--effort", default="automatic")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()

    ogent = load_ogent()
    source = args.source.expanduser().resolve(strict=True)
    source_hash_before = hashlib.sha256(source.read_bytes()).hexdigest()
    state = ogent.OgentState()
    ogent.STATE = state
    session = state.create_session()
    try:
        ogent.dispatch_open_path(session, str(source))
        catalog = ogent.AGENT_CATALOG.refresh_now(args.provider)
        if catalog.status != "ready":
            raise RuntimeError(catalog.warning or f"{args.provider} is not ready")
        if not catalog.models:
            raise RuntimeError(f"{args.provider} did not report a model")
        selected_capability = next(
            (model for model in catalog.models if model.is_default),
            catalog.models[0],
        )
        selection = ogent.validate_agent_selection(
            args.provider,
            args.model or selected_capability.id,
            args.effort,
        )
        run_id = ogent.start_agent_run(
            session,
            (
                "Use OfficeCLI to append one Normal paragraph at the end with "
                f"exactly this text: {args.marker}\n"
                "Do not change any existing text. Match the preceding Normal "
                "paragraph's formatting, including its first-line indent, size, "
                "and spacing. Verify the exact marker with officecli view and "
                "validate before reporting completion."
            ),
            selection.model,
            selection.effort,
            selection.provider_id,
            selection=selection,
        )
        if not session.run_complete.wait(timeout=args.timeout_seconds):
            ogent.stop_active_run(session)
            session.run_complete.wait(timeout=30)
            raise TimeoutError(
                f"{args.provider} did not complete run {run_id} in time"
            )
        with session.lock:
            status = session.run_status
            working = session.active_doc
            error = session.last_error
            transcript_count = len(session.transcript)
            assistant_text = next(
                (
                    item["text"]
                    for item in reversed(session.transcript)
                    if item.get("role") == "assistant"
                ),
                None,
            )
            activity_tail = [
                str(event.get("data", {}).get("text", ""))
                for event in session.events
                if event.get("type") == "activity"
            ][-12:]
            watch_port = session.watch_port
            codex_context = bool(session.codex_thread_id)
            claude_context = bool(session.claude_session_id)
        if status != "idle" or working is None:
            raise RuntimeError(error or f"Agent run ended with status {status}")
        if source_hash_before != hashlib.sha256(source.read_bytes()).hexdigest():
            raise RuntimeError("The source document changed during the live test.")
        if not watch_port:
            raise RuntimeError("The live preview did not have a watch port.")
        with urllib.request.urlopen(
            f"http://{ogent.HOST}:{watch_port}/",
            timeout=20,
        ) as response:
            preview_html = response.read().decode("utf-8", errors="replace")
        if args.marker not in preview_html:
            raise RuntimeError("The live preview did not contain the expected marker.")
        print(
            json.dumps(
                {
                    "provider": args.provider,
                    "model": selection.model,
                    "effort": selection.effort,
                    "run_id": run_id,
                    "run_status": status,
                    "source_document": str(source),
                    "working_document": str(working),
                    "transcript_messages": transcript_count,
                    "assistant_text": assistant_text,
                    "activity_tail": activity_tail,
                    "source_sha256_unchanged": True,
                    "live_preview_contains_marker": True,
                    "codex_context_created": codex_context,
                    "claude_context_created": claude_context,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        ogent.close_session(session)
        ogent.AGENT_CATALOG.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

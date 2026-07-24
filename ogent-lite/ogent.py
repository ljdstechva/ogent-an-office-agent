#!/usr/bin/env python3
"""Ogent Lite: a local, single-file document workspace and Codex chat bridge.

Standard-library only. The server binds to 127.0.0.1, owns the OfficeCLI watch
lifecycle, preserves source documents by editing working copies, and runs one
Codex process at a time.
"""

from __future__ import annotations

import argparse
import atexit
import collections
import contextlib
import datetime as dt
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

try:
    import winreg
except ImportError:  # pragma: no cover - Ogent is a Windows app.
    winreg = None  # type: ignore[assignment]


APP_NAME = "Ogent Lite"
APP_VERSION = "0.5.0"
HOST = "127.0.0.1"
BASE_PORT = 8765
WATCH_PORT_FIRST = 26320
WATCH_PORT_LAST = 26380
DEFAULT_SESSION_GRACE_SECONDS = 120.0
DEFAULT_REAPER_TICK_SECONDS = 30.0
DEFAULT_IDLE_EXIT_MINUTES = 10.0
SNAPSHOT_SHUTDOWN_GRACE_SECONDS = 55.0
SUPPORTED_OFFICE = {".docx", ".xlsx", ".pptx"}
SHELL_EXTENSIONS = (".docx", ".xlsx", ".pptx")
ACTIVE_RUN_STATUSES = {"starting", "working", "stopping"}
REAPABLE_RUN_STATUSES = {"idle", "error", "stopped"}
MAX_BODY_BYTES = 64 * 1024
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING = "medium"
ALLOWED_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra")
ALLOWED_REASONING = ("low", "medium", "high", "xhigh", "max", "ultra")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ASSETS_DIR = SCRIPT_DIR / "assets"
ICON_PATH = ASSETS_DIR / "ogent.ico"
PDF_TO_DOCX = REPO_ROOT / "tools" / "pdf2docx.ps1"
DOCX_TO_PDF = REPO_ROOT / "tools" / "docx2pdf.ps1"
LOCAL_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "OgentLite"
WORK_ROOT = LOCAL_DATA / "work"
RECENT_PATH = LOCAL_DATA / "recent.json"
SERVER_INFO_PATH = LOCAL_DATA / "server.json"

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS_CHILD_FLAGS = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


class UserFacingError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned[:80] or "document"


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def command_env() -> dict[str, str]:
    env = os.environ.copy()
    env["OFFICECLI_RESIDENT_FLUSH"] = "each"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def codex_launch_prefix() -> list[str]:
    """Resolve Codex without asking CreateProcess to execute an npm shim."""
    cmd_path = shutil.which("codex.cmd")
    node_path = shutil.which("node.exe") or shutil.which("node")
    if cmd_path and node_path:
        codex_js = Path(cmd_path).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if codex_js.is_file():
            return [node_path, str(codex_js)]
    exe_path = shutil.which("codex.exe")
    if exe_path:
        return [exe_path]
    raise UserFacingError(
        "Codex CLI was not found. Install or repair Codex, then confirm `codex --version` works.",
        500,
    )


def validate_agent_settings(model: Any, reasoning: Any) -> tuple[str, str]:
    selected_model = str(model or DEFAULT_MODEL).strip()
    selected_reasoning = str(reasoning or DEFAULT_REASONING).strip().casefold()
    if selected_model not in ALLOWED_MODELS:
        raise UserFacingError(
            f"Unsupported model. Choose one of: {', '.join(ALLOWED_MODELS)}."
        )
    if selected_reasoning not in ALLOWED_REASONING:
        raise UserFacingError(
            f"Unsupported reasoning effort. Choose one of: {', '.join(ALLOWED_REASONING)}."
        )
    return selected_model, selected_reasoning


def build_codex_command(
    prompt: str,
    session_id: str | None,
    model: str,
    reasoning: str,
) -> list[str]:
    selected_model, selected_reasoning = validate_agent_settings(model, reasoning)
    effort_config = f"model_reasoning_effort={json.dumps(selected_reasoning)}"
    if session_id:
        return [
            *codex_launch_prefix(),
            "exec",
            "resume",
            "-m",
            selected_model,
            "-c",
            effort_config,
            "--json",
            "--skip-git-repo-check",
            session_id,
            prompt,
        ]
    return [
        *codex_launch_prefix(),
        "exec",
        "-m",
        selected_model,
        "-c",
        effort_config,
        "-s",
        "danger-full-access",
        "--color",
        "never",
        "--json",
        "--skip-git-repo-check",
        prompt,
    ]


def run_quiet(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 20,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=command_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        check=False,
    )


def terminate_process_tree(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


def cleanup_word_snapshot_process(
    process: subprocess.Popen[str] | None,
    pid_file: Path | None,
) -> None:
    terminate_process_tree(process)
    if (
        pid_file is None
        or not path_is_within(pid_file, WORK_ROOT)
        or not pid_file.is_file()
    ):
        return
    try:
        word_pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        word_pid = 0
    if os.name == "nt" and word_pid > 0:
        # Word COM is activated through svchost, so it is not reliably part of
        # PowerShell's child tree. Validate the exact converter-recorded PID
        # before terminating an automation instance after forced cancellation.
        script = (
            f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId={word_pid}\"; "
            "if ($p -and $p.Name -ieq 'WINWORD.EXE' -and "
            "$p.CommandLine -match '(?i)(/Automation|-Embedding)') { "
            f"Stop-Process -Id {word_pid} -Force -ErrorAction SilentlyContinue "
            "}"
        )
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            run_quiet(
                ["powershell.exe", "-NoProfile", "-Command", script],
                timeout=10,
            )
    with contextlib.suppress(OSError):
        pid_file.unlink()


def http_json(url: str, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def watch_http_alive(port: int | None) -> bool:
    if port is None:
        return False
    try:
        request = urllib.request.Request(f"http://{HOST}:{port}/", method="GET")
        with urllib.request.urlopen(request, timeout=1.25) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            return False


def wait_for_port_closed(port: int | None, timeout: float = 3.0) -> bool:
    if port is None:
        return True
    deadline = time.monotonic() + timeout
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            if sock.connect_ex((HOST, port)) != 0:
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # This is bounded process-shutdown synchronization, not a background
        # polling loop. It keeps "session removed" and "port released" atomic
        # from the browser's point of view.
        threading.Event().wait(min(0.05, remaining))


def load_recent() -> list[str]:
    try:
        data = json.loads(RECENT_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(item) for item in data if isinstance(item, str)][:12]
    except (OSError, ValueError):
        pass
    return []


def save_recent(paths: list[str]) -> None:
    LOCAL_DATA.mkdir(parents=True, exist_ok=True)
    temp = RECENT_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(paths[:12], ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, RECENT_PATH)


class SessionState:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.created_at = time.time()
        self.created_at_iso = now_iso()
        self.lock = threading.RLock()
        self.watch_lock = threading.RLock()
        self.close_lock = threading.Lock()
        self.close_complete = threading.Event()
        self.condition = threading.Condition(self.lock)
        self.events: collections.deque[dict[str, Any]] = collections.deque(maxlen=2000)
        self.sequence = 0
        self.transcript: list[dict[str, Any]] = []
        self.active_source: Path | None = None
        self.active_doc: Path | None = None
        self.opening_source: Path | None = None
        self.watch_process: subprocess.Popen[str] | None = None
        self.watch_port: int | None = None
        self.watch_tail: collections.deque[str] = collections.deque(maxlen=40)
        self.run_process: subprocess.Popen[str] | None = None
        self.run_thread: threading.Thread | None = None
        self.run_status = "idle"
        self.run_id: str | None = None
        self.stop_requested = False
        self.codex_thread_id: str | None = None
        self.pending_pdf = False
        self.last_error: str | None = None
        self.sse_clients = 0
        self.sse_client_refs: collections.Counter[str] = collections.Counter()
        self.orphan_since: float | None = time.time()
        self.complex_layout = False
        self.complex_layout_detail: str | None = None
        self.snapshot_in_progress = False
        self.snapshot_process: subprocess.Popen[str] | None = None
        self.snapshot_complete = threading.Event()
        self.snapshot_complete.set()
        self.snapshot_pid_file: Path | None = None
        self.snapshot_path: Path | None = None
        self.closed = False

    def emit(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        with self.condition:
            self.sequence += 1
            event = {
                "seq": self.sequence,
                "type": event_type,
                "time": now_iso(),
                "data": data,
            }
            self.events.append(event)
            self.condition.notify_all()
            return event

    def add_message(self, role: str, text: str) -> None:
        message = {"role": role, "text": text, "time": now_iso()}
        with self.lock:
            if self.closed:
                return
            self.transcript.append(message)
            self.transcript = self.transcript[-100:]
        self.emit("message", message)

    def add_activity(self, stream: str, text: str) -> None:
        if text:
            self.emit("activity", {"stream": stream, "text": text[-4000:]})

    def set_run_status(self, status: str, **extra: Any) -> None:
        with self.lock:
            if self.closed:
                return
            self.run_status = status
        self.emit("run", {"status": status, **extra})

    def public_snapshot(self, include_watch_probe: bool = True) -> dict[str, Any]:
        with self.lock:
            active_doc = str(self.active_doc) if self.active_doc else None
            active_source = str(self.active_source) if self.active_source else None
            watch_port = self.watch_port
            snapshot = {
                "session_id": self.session_id,
                "created_at": self.created_at_iso,
                "active_document": active_doc,
                "source_document": active_source,
                "watch_port": watch_port,
                "watch_url": f"http://{HOST}:{watch_port}/" if watch_port else None,
                "run_status": self.run_status,
                "run_id": self.run_id,
                "transcript": list(self.transcript),
                "last_error": self.last_error,
                "codex_context": bool(self.codex_thread_id),
                "sequence": self.sequence,
                "sse_clients": self.sse_clients,
                "orphan_since": self.orphan_since,
                "complex_layout": self.complex_layout,
                "complex_layout_detail": self.complex_layout_detail,
                "snapshot_in_progress": self.snapshot_in_progress,
                "snapshot_available": bool(
                    self.snapshot_path and self.snapshot_path.is_file()
                ),
            }
        snapshot["watch_alive"] = (
            bool(active_doc) and watch_http_alive(watch_port)
            if include_watch_probe
            else False
        )
        return snapshot

    def current_events_after(self, sequence: int) -> list[dict[str, Any]]:
        with self.lock:
            return [event for event in self.events if event["seq"] > sequence]

    def connect_sse(self, client_id: str) -> None:
        with self.lock:
            if self.closed:
                raise UserFacingError("This Ogent session has closed.", 410)
            self.sse_client_refs[client_id] += 1
            self.sse_clients = sum(self.sse_client_refs.values())
            self.orphan_since = None

    def disconnect_sse(self, client_id: str) -> None:
        with self.lock:
            if self.sse_client_refs[client_id] > 1:
                self.sse_client_refs[client_id] -= 1
            else:
                self.sse_client_refs.pop(client_id, None)
            self.sse_clients = sum(self.sse_client_refs.values())
            if self.sse_clients == 0 and self.orphan_since is None:
                self.orphan_since = time.time()

    def mark_page_closed(self, client_id: str) -> None:
        with self.lock:
            # The close beacon and the SSE disconnect can arrive in either
            # order. Removing by stable tab id makes both operations idempotent
            # and preserves any other tab connected to this deduped session.
            self.sse_client_refs.pop(client_id, None)
            self.sse_clients = sum(self.sse_client_refs.values())
            if self.sse_clients == 0:
                self.orphan_since = time.time()


class OgentState:
    def __init__(self) -> None:
        self.registry_lock = threading.RLock()
        self.recent_lock = threading.RLock()
        self.pick_lock = threading.Lock()
        self.pick_process: subprocess.Popen[str] | None = None
        self.sessions: dict[str, SessionState] = {}
        self.path_index: dict[str, str] = {}
        self.recent = load_recent()
        self.server_port = BASE_PORT
        self.token = secrets.token_urlsafe(32)
        self.shutdown_requested = False
        self.cleanup_started = False
        self.empty_since: float | None = time.time()
        self.session_grace_seconds = DEFAULT_SESSION_GRACE_SECONDS
        self.reaper_tick_seconds = DEFAULT_REAPER_TICK_SECONDS
        self.idle_exit_minutes = DEFAULT_IDLE_EXIT_MINUTES
        self.shutdown_callback: Callable[[], None] | None = None

    @staticmethod
    def path_key(path: Path) -> str:
        return os.path.normcase(str(path.resolve()))

    def create_session(self) -> SessionState:
        with self.registry_lock:
            if self.shutdown_requested:
                raise UserFacingError("Ogent is shutting down. Launch it again in a moment.", 503)
            while True:
                session_id = uuid.uuid4().hex[:8]
                if session_id not in self.sessions:
                    break
            session = SessionState(session_id)
            self.sessions[session_id] = session
            self.empty_since = None
        self.broadcast_sessions()
        return session

    def get_session(self, session_id: str) -> SessionState:
        with self.registry_lock:
            session = self.sessions.get(session_id)
        if session is None or session.closed:
            raise UserFacingError("This Ogent session no longer exists.", 410)
        return session

    def summaries(self) -> list[dict[str, Any]]:
        with self.registry_lock:
            sessions = list(self.sessions.values())
        summaries: list[dict[str, Any]] = []
        for session in sorted(sessions, key=lambda item: item.created_at):
            with session.lock:
                source = session.active_source
                document = session.active_doc
                summaries.append(
                    {
                        "id": session.session_id,
                        "document_name": (
                            source.name
                            if source
                            else document.name
                            if document
                            else "New workspace"
                        ),
                        "run_status": session.run_status,
                        "watch_port": session.watch_port,
                        "created_at": session.created_at_iso,
                        "sse_clients": session.sse_clients,
                    }
                )
        return summaries

    def snapshot_for(
        self,
        session: SessionState,
        *,
        include_watch_probe: bool = True,
    ) -> dict[str, Any]:
        snapshot = session.public_snapshot(include_watch_probe=include_watch_probe)
        with self.recent_lock:
            recent = list(self.recent)
        snapshot.update(
            {
                "app": APP_NAME,
                "version": APP_VERSION,
                "server_port": self.server_port,
                "recent": recent,
                "sessions": self.summaries(),
                "idle_exit_minutes": self.idle_exit_minutes,
                "session_grace_seconds": self.session_grace_seconds,
            }
        )
        return snapshot

    def global_snapshot(self) -> dict[str, Any]:
        with self.recent_lock:
            recent = list(self.recent)
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "server_port": self.server_port,
            "recent": recent,
            "sessions": self.summaries(),
            "idle_exit_minutes": self.idle_exit_minutes,
            "session_grace_seconds": self.session_grace_seconds,
        }

    def broadcast_sessions(self) -> None:
        summaries = self.summaries()
        with self.registry_lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            with session.lock:
                if session.closed:
                    continue
            session.emit("sessions", {"items": summaries})

    def remember(self, source: Path) -> None:
        value = str(source)
        with self.recent_lock:
            self.recent = [
                item for item in self.recent if item.casefold() != value.casefold()
            ]
            self.recent.insert(0, value)
            self.recent = self.recent[:12]
            recent = list(self.recent)
            save_recent(recent)
        with self.registry_lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.emit("recent", {"items": recent})

    def claim_source(
        self,
        session: SessionState,
        source: Path,
    ) -> SessionState | None:
        key = self.path_key(source)
        with self.registry_lock:
            existing_id = self.path_index.get(key)
            existing = self.sessions.get(existing_id) if existing_id else None
            if existing:
                if existing.closed:
                    raise UserFacingError(
                        "That document's previous session is still closing. Try again.",
                        409,
                    )
                return existing
            with session.lock:
                if session.closed:
                    raise UserFacingError("This Ogent session has closed.", 410)
                if session.opening_source is not None:
                    raise UserFacingError(
                        "This Ogent session is already opening a document. "
                        "Wait for it to finish.",
                        409,
                    )
                self.path_index[key] = session.session_id
                session.opening_source = source
        return None

    def release_claim(self, session: SessionState, source: Path) -> None:
        key = self.path_key(source)
        with self.registry_lock:
            with session.lock:
                if session.opening_source and self.path_key(session.opening_source) == key:
                    session.opening_source = None
                if (
                    self.path_index.get(key) == session.session_id
                    and (
                        session.active_source is None
                        or self.path_key(session.active_source) != key
                    )
                ):
                    self.path_index.pop(key, None)

    def clear_document(self, session: SessionState) -> None:
        with self.registry_lock:
            for key, owner in list(self.path_index.items()):
                if owner == session.session_id:
                    self.path_index.pop(key, None)
            with session.lock:
                session.active_doc = None
                session.active_source = None
                session.opening_source = None
                session.codex_thread_id = None
                session.pending_pdf = False
                session.snapshot_path = None
                session.complex_layout = False
                session.complex_layout_detail = None

    def commit_document(
        self,
        session: SessionState,
        source: Path,
        working: Path,
        *,
        preserve_transcript: bool,
        reset_run: bool,
        complex_layout: bool,
        complex_layout_detail: str | None,
    ) -> None:
        source_key = self.path_key(source)
        working_key = self.path_key(working)
        with self.registry_lock:
            if session.closed or self.sessions.get(session.session_id) is not session:
                raise UserFacingError("This Ogent session has closed.", 410)
            for key, owner in list(self.path_index.items()):
                if owner == session.session_id and key not in {source_key, working_key}:
                    self.path_index.pop(key, None)
            self.path_index[source_key] = session.session_id
            self.path_index[working_key] = session.session_id
            with session.lock:
                session.active_source = source
                session.active_doc = working
                session.opening_source = None
                session.codex_thread_id = None
                session.pending_pdf = False
                session.last_error = None
                session.complex_layout = complex_layout
                session.complex_layout_detail = complex_layout_detail
                session.snapshot_path = None
                if session.sse_clients == 0:
                    # Opening can outlast the original orphan grace (notably
                    # PDF conversion and large DOCX inspection). Give the
                    # completed workspace a full reconnect window.
                    session.orphan_since = time.time()
                if reset_run:
                    session.run_status = "idle"
                    session.run_id = None
                    session.stop_requested = False
                if not preserve_transcript:
                    session.transcript = []
        session.emit("snapshot", self.snapshot_for(session, include_watch_probe=False))
        self.broadcast_sessions()

    def allocate_watch_port(self, session: SessionState) -> int:
        with self.registry_lock:
            with session.lock:
                if session.watch_port is not None:
                    return session.watch_port
            used = {
                item.watch_port
                for item in self.sessions.values()
                if item.watch_port is not None and item is not session
            }
            for port in range(WATCH_PORT_FIRST, WATCH_PORT_LAST + 1):
                if port in used or not port_available(port):
                    continue
                with session.lock:
                    session.watch_port = port
                return port
        raise UserFacingError(
            f"No free OfficeCLI preview port is available from "
            f"{WATCH_PORT_FIRST} through {WATCH_PORT_LAST}.",
            503,
        )

    def release_watch_port(self, session: SessionState, port: int | None = None) -> None:
        with self.registry_lock:
            with session.lock:
                if port is None or session.watch_port == port:
                    session.watch_port = None

    def begin_session_close(
        self,
        session: SessionState,
        *,
        require_reapable_at: float | None = None,
    ) -> bool:
        with self.registry_lock:
            if self.sessions.get(session.session_id) is not session:
                return False
            with session.lock:
                if session.closed:
                    return False
                if require_reapable_at is not None:
                    if (
                        session.orphan_since is None
                        or session.sse_clients != 0
                        or session.run_status not in REAPABLE_RUN_STATUSES
                        or session.opening_source is not None
                        or session.snapshot_in_progress
                        or require_reapable_at - session.orphan_since
                        < self.session_grace_seconds
                    ):
                        return False
                session.closed = True
                session.condition.notify_all()
        return True

    def finish_session_close(self, session: SessionState) -> None:
        removed = False
        with self.registry_lock:
            if self.sessions.get(session.session_id) is session:
                self.sessions.pop(session.session_id, None)
                for key, owner in list(self.path_index.items()):
                    if owner == session.session_id:
                        self.path_index.pop(key, None)
                if not self.sessions:
                    self.empty_since = time.time()
                removed = True
        session.close_complete.set()
        if removed:
            self.broadcast_sessions()


STATE = OgentState()


def stop_watch(
    session: SessionState,
    *,
    clear_document: bool = False,
    release_port: bool = False,
) -> None:
    with session.watch_lock:
        with session.lock:
            document = session.active_doc
            process = session.watch_process
            port = session.watch_port
            session.watch_process = None
        if clear_document:
            STATE.clear_document(session)

        if process and process.poll() is None:
            terminate_process_tree(process)
        if document:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                run_quiet(
                    ["officecli", "unwatch", str(document)],
                    cwd=document.parent,
                    timeout=12,
                )
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                run_quiet(
                    ["officecli", "close", str(document)],
                    cwd=document.parent,
                    timeout=12,
                )
        wait_for_port_closed(port)
        if release_port:
            STATE.release_watch_port(session, port)
        if process or port:
            session.emit("watch", {"status": "stopped", "port": port})
        if clear_document and not session.closed:
            session.emit(
                "snapshot",
                STATE.snapshot_for(session, include_watch_probe=False),
            )
    STATE.broadcast_sessions()


def _watch_output_reader(
    session: SessionState,
    process: subprocess.Popen[str],
    ready_queue: queue.Queue[tuple[str, str]],
    port: int,
) -> None:
    assert process.stdout is not None
    for raw in iter(process.stdout.readline, ""):
        line = raw.rstrip()
        if not line:
            continue
        with session.lock:
            session.watch_tail.append(line)
        session.add_activity("watch", line)
        ready_queue.put(("line", line))
        if "http://" in line or "https://" in line or "watching" in line.casefold():
            ready_queue.put(("ready", line))
    code = process.wait()
    ready_queue.put(("exit", str(code)))
    with session.lock:
        is_current = session.watch_process is process
        if is_current:
            session.watch_process = None
        closed = session.closed
    if is_current and not STATE.shutdown_requested and not closed:
        session.emit("watch", {"status": "dead", "exit_code": code, "port": port})
        STATE.broadcast_sessions()


def start_watch(session: SessionState, document: Path) -> None:
    with session.watch_lock:
        stop_watch(session, clear_document=False, release_port=False)
        if not document.exists():
            raise UserFacingError(f"The working document no longer exists: {document}", 404)

        port = STATE.allocate_watch_port(session)
        if not port_available(port):
            STATE.release_watch_port(session, port)
            port = STATE.allocate_watch_port(session)

        ready_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        process = subprocess.Popen(
            ["officecli", "watch", str(document), "--port", str(port)],
            cwd=str(document.parent),
            env=command_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            creationflags=WINDOWS_CHILD_FLAGS if os.name == "nt" else 0,
        )
        with session.lock:
            if session.closed:
                terminate_process_tree(process)
                raise UserFacingError("This Ogent session has closed.", 410)
            session.watch_process = process
            session.watch_tail.clear()
        reader = threading.Thread(
            target=_watch_output_reader,
            args=(session, process, ready_queue, port),
            name=f"ogent-watch-{session.session_id}",
            daemon=True,
        )
        reader.start()

        deadline = time.monotonic() + 18
        last_line = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                kind, value = ready_queue.get(timeout=remaining)
            except queue.Empty:
                break
            last_line = value or last_line
            if kind == "exit":
                terminate_process_tree(process)
                with session.lock:
                    if session.watch_process is process:
                        session.watch_process = None
                if "port" in last_line.casefold() and "use" in last_line.casefold():
                    STATE.release_watch_port(session, port)
                    raise UserFacingError(
                        f"Preview port {port} was claimed by another process. Try again.",
                        409,
                    )
                raise UserFacingError(
                    f"OfficeCLI watch exited before it became ready (exit {value}). "
                    f"{last_line}",
                    500,
                )
            if kind == "ready":
                session.emit(
                    "watch",
                    {"status": "ready", "port": port, "document": str(document)},
                )
                STATE.broadcast_sessions()
                return

        if watch_http_alive(port):
            session.emit(
                "watch",
                {"status": "ready", "port": port, "document": str(document)},
            )
            STATE.broadcast_sessions()
            return
        terminate_process_tree(process)
        with session.lock:
            if session.watch_process is process:
                session.watch_process = None
        raise UserFacingError(f"OfficeCLI watch did not become ready. {last_line}", 504)


def ensure_watch(session: SessionState) -> None:
    with session.watch_lock:
        with session.lock:
            document = session.active_doc
            port = session.watch_port
        if not document:
            raise UserFacingError("Open an Office document first.", 409)
        if not document.exists():
            stop_watch(session, clear_document=True, release_port=True)
            raise UserFacingError(
                "The active working file was moved or deleted. Paste its new path and open it again.",
                404,
            )
        if watch_http_alive(port):
            return
        session.emit("watch", {"status": "restarting", "port": port})
        start_watch(session, document)


def make_working_copy(session: SessionState, source: Path) -> Path:
    session_root = WORK_ROOT / session.session_id
    session_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(str(source).casefold().encode("utf-8")).hexdigest()[:8]
    nonce = uuid.uuid4().hex[:6]
    filename = (
        f"{safe_name(source.stem)}-ogent-{stamp}-{digest}-{nonce}"
        f"{source.suffix.lower()}"
    )
    destination = session_root / filename
    shutil.copy2(source, destination)
    return destination


def normalize_existing_path(raw_path: str) -> Path:
    value = raw_path.strip().strip('"').strip("'")
    if not value:
        raise UserFacingError("Paste an absolute document path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise UserFacingError("Use an absolute path, such as D:\\Reports\\report.docx.")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise UserFacingError(f"File not found: {path}", 404) from None
    if not resolved.is_file():
        raise UserFacingError(f"Not a file: {resolved}")
    return resolved


def detect_complex_layout(document: Path) -> tuple[bool, str | None]:
    if document.suffix.lower() != ".docx":
        return False, None
    try:
        depth_result = run_quiet(
            ["officecli", "get", str(document), "/body", "--depth", "1"],
            cwd=document.parent,
            timeout=45,
        )
        depth_text = f"{depth_result.stdout}\n{depth_result.stderr}".casefold()
        markers = ("[textbox]", "[shape]", "[drawing]", "(textbox)", "(shape)", "(drawing)")
        if depth_result.returncode == 0 and any(marker in depth_text for marker in markers):
            return True, "Floating textboxes, shapes, or anchored drawings were detected."

        # OfficeCLI 1.0.141 does not surface nested fallback shapes in a depth-1
        # body listing, so use its structured query as an honest fallback.
        query_result = run_quiet(
            [
                "officecli",
                "query",
                str(document),
                "textbox, shape, drawing",
                "--compact",
            ],
            cwd=document.parent,
            timeout=45,
        )
        query_text = f"{query_result.stdout}\n{query_result.stderr}"
        matches = re.findall(r"\[(textbox|shape|drawing)\]", query_text, re.IGNORECASE)
        if query_result.returncode != 0:
            return True, "Layout inspection was inconclusive; Word view is recommended."
        if matches:
            counts = collections.Counter(item.casefold() for item in matches)
            plural = {"textbox": "textboxes", "shape": "shapes", "drawing": "drawings"}
            detail = ", ".join(
                f"{counts[name]} {name if counts[name] == 1 else plural[name]}"
                for name in ("textbox", "shape", "drawing")
                if counts[name]
            )
            return True, f"Detected {detail}."
        return False, None
    except (OSError, subprocess.TimeoutExpired):
        return True, "Layout inspection was inconclusive; Word view is recommended."


def open_document(
    session: SessionState,
    raw_path: str,
    *,
    make_copy: bool = True,
    state_source: Path | None = None,
    preserve_transcript: bool = False,
    remember_source: bool = True,
    announce: bool = True,
    reset_run: bool = True,
) -> dict[str, Any]:
    source = normalize_existing_path(raw_path)
    extension = source.suffix.lower()
    if extension == ".pdf":
        raise UserFacingError(
            "PDFs open via the pipeline. Ask me in chat to edit one and I will produce a working DOCX.",
            415,
        )
    if extension not in SUPPORTED_OFFICE:
        raise UserFacingError("Supported document types are .docx, .xlsx, and .pptx.", 415)

    session_root = WORK_ROOT / session.session_id
    if make_copy and not path_is_within(source, session_root):
        working = make_working_copy(session, source)
    else:
        working = source

    with session.lock:
        previous_document = session.active_doc
        previous_source = session.active_source
        previous_complex_layout = session.complex_layout
        previous_complex_detail = session.complex_layout_detail
    try:
        start_watch(session, working)
        protected_source = state_source.resolve() if state_source else source
        complex_layout, complex_detail = detect_complex_layout(working)
        STATE.commit_document(
            session,
            protected_source,
            working,
            preserve_transcript=preserve_transcript,
            reset_run=reset_run,
            complex_layout=complex_layout,
            complex_layout_detail=complex_detail,
        )
    except Exception:
        stop_watch(session, clear_document=False, release_port=previous_document is None)
        if working != source:
            with contextlib.suppress(OSError):
                working.unlink()
        if previous_document and previous_document.exists():
            with contextlib.suppress(Exception):
                start_watch(session, previous_document)
            with session.lock:
                session.active_doc = previous_document
                session.active_source = previous_source
                session.complex_layout = previous_complex_layout
                session.complex_layout_detail = previous_complex_detail
        raise

    if remember_source:
        STATE.remember(source)
    if announce:
        session.add_message(
            "assistant",
            f"Opened a protected working copy: {working.name}. The source file remains untouched.",
        )
    session.emit(
        "document",
        {
            "session_id": session.session_id,
            "source": str(protected_source),
            "working": str(working),
            "watch_url": (
                f"http://{HOST}:{session.watch_port}/"
                if session.watch_port
                else None
            ),
            "complex_layout": complex_layout,
            "complex_layout_detail": complex_detail,
        },
    )
    return {
        "message": "Working copy opened.",
        "session_id": session.session_id,
        "source": str(protected_source),
        "active_document": str(working),
        "watch_url": (
            f"http://{HOST}:{session.watch_port}/"
            if session.watch_port
            else None
        ),
        "complex_layout": complex_layout,
        "complex_layout_detail": complex_detail,
    }


def extract_pdf_path(message: str) -> Path | None:
    stripped = message.strip().strip('"').strip("'")
    candidates: list[str] = []
    if stripped.lower().endswith(".pdf"):
        candidates.append(stripped)
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r'([A-Za-z]:[\\/][^\r\n"]*?\.pdf)', message, re.IGNORECASE)
    )
    for candidate in candidates:
        path = Path(candidate)
        if path.is_absolute() and path.exists() and path.is_file():
            return path.resolve()
    return None


def _stream_plain_process(
    session: SessionState,
    process: subprocess.Popen[str],
    *,
    stream_name: str,
) -> tuple[int, list[str]]:
    lines: list[str] = []
    assert process.stdout is not None
    for raw in iter(process.stdout.readline, ""):
        line = raw.rstrip()
        if not line:
            continue
        lines.append(line)
        session.add_activity(stream_name, line)
    return process.wait(), lines


def _finish_session_run(
    session: SessionState,
    run_id: str,
    status: str,
    *,
    process: subprocess.Popen[str] | None = None,
    **extra: Any,
) -> bool:
    with session.lock:
        if session.run_id != run_id:
            return False
        if process is None or session.run_process is process:
            session.run_process = None
        if session.run_thread is threading.current_thread():
            session.run_thread = None
        session.stop_requested = False
        session.run_status = status
        if session.sse_clients == 0:
            # A tab may close while Codex is working. Never consume the user's
            # reconnect grace while that run is protected from reaping.
            session.orphan_since = time.time()
    session.emit("run", {"status": status, "run_id": run_id, **extra})
    STATE.broadcast_sessions()
    return True


def _pdf_import_worker(
    session: SessionState,
    source: Path,
    request_text: str,
    run_id: str,
) -> None:
    work_dir = (
        WORK_ROOT
        / session.session_id
        / f"pdf-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    )
    copied_pdf = work_dir / f"{safe_name(source.stem)}-source-copy.pdf"
    working_docx = work_dir / f"{safe_name(source.stem)}-working.docx"
    process: subprocess.Popen[str] | None = None
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copied_pdf)
        with session.lock:
            if session.closed or session.stop_requested or session.run_id != run_id:
                session.add_message("assistant", "PDF conversion stopped.")
                _finish_session_run(session, run_id, "stopped", kind="pdf")
                return
            session.run_status = "working"
        session.emit(
            "run",
            {
                "status": "working",
                "kind": "pdf",
                "run_id": run_id,
                "label": "Converting a protected PDF copy",
            },
        )
        STATE.broadcast_sessions()
        with session.lock:
            if session.closed or session.stop_requested or session.run_id != run_id:
                session.add_message("assistant", "PDF conversion stopped.")
                _finish_session_run(session, run_id, "stopped", kind="pdf")
                return
            process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(PDF_TO_DOCX),
                    "-Pdf",
                    str(copied_pdf),
                    "-OutDocx",
                    str(working_docx),
                ],
                cwd=str(REPO_ROOT),
                env=command_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                creationflags=WINDOWS_CHILD_FLAGS if os.name == "nt" else 0,
            )
            session.run_process = process
        code, lines = _stream_plain_process(session, process, stream_name="pdf")
        with session.lock:
            stopped = session.stop_requested or session.run_id != run_id
        if stopped:
            session.add_message("assistant", "PDF conversion stopped.")
            _finish_session_run(
                session,
                run_id,
                "stopped",
                process=process,
                kind="pdf",
            )
            return
        if code != 0 or not working_docx.exists():
            tail = "\n".join(lines[-8:])
            if "SCANNED_PDF" in tail:
                message = "This PDF is image-only and needs OCR before it can be edited."
            else:
                message = f"PDF conversion failed with exit code {code}. {tail}".strip()
            with session.lock:
                session.last_error = message
            session.add_message("assistant", message)
            _finish_session_run(
                session,
                run_id,
                "error",
                process=process,
                kind="pdf",
                exit_code=code,
            )
            return
        open_document(
            session,
            str(working_docx),
            make_copy=False,
            state_source=source,
            preserve_transcript=True,
            remember_source=False,
            announce=False,
            reset_run=False,
        )
        with session.lock:
            stopped = (
                session.closed
                or session.stop_requested
                or session.run_id != run_id
            )
        if stopped:
            session.add_message("assistant", "PDF preparation stopped.")
            _finish_session_run(
                session,
                run_id,
                "stopped",
                process=process,
                kind="pdf",
            )
            return
        session.add_message(
            "assistant",
            "The source PDF was preserved, its working DOCX is open on the left, and it is ready for your edit request.",
        )
        _finish_session_run(
            session,
            run_id,
            "idle",
            process=process,
            kind="pdf",
            exit_code=0,
        )
    except Exception as exc:
        with session.lock:
            session.last_error = str(exc)
        session.add_message("assistant", f"PDF preparation failed: {exc}")
        _finish_session_run(
            session,
            run_id,
            "error",
            process=process,
            kind="pdf",
        )
    finally:
        STATE.release_claim(session, source)


def start_pdf_import(
    session: SessionState,
    source: Path,
    request_text: str,
) -> str:
    with session.lock:
        if session.closed:
            raise UserFacingError("This Ogent session has closed.", 410)
        if session.run_status in ACTIVE_RUN_STATUSES:
            raise UserFacingError("Ogent is still working. Stop that run or wait for it to finish.", 409)
        if session.snapshot_in_progress:
            raise UserFacingError("Word view is still being generated. Wait for it to finish.", 409)
        session.run_status = "starting"
        session.run_id = uuid.uuid4().hex
        session.stop_requested = False
        session.pending_pdf = False
        run_id = session.run_id
    session.emit("run", {"status": "starting", "kind": "pdf", "run_id": run_id})
    STATE.broadcast_sessions()
    thread = threading.Thread(
        target=_pdf_import_worker,
        args=(session, source, request_text, run_id),
        name=f"ogent-pdf-{session.session_id}-{run_id[:8]}",
        daemon=True,
    )
    with session.lock:
        session.run_thread = thread
    thread.start()
    return run_id


def dispatch_open_path(session: SessionState, raw_path: str) -> dict[str, Any]:
    source = normalize_existing_path(raw_path)
    if source.suffix.lower() not in {*SUPPORTED_OFFICE, ".pdf"}:
        raise UserFacingError(
            "Supported document types are .docx, .xlsx, .pptx, and .pdf.",
            415,
        )
    deduped = STATE.claim_source(session, source)
    if deduped is not None:
        return {
            "action": "focus_session",
            "session_id": deduped.session_id,
            "message": "That document is already open. Switched to its Ogent session.",
            "url": f"/?s={deduped.session_id}",
        }
    if source.suffix.lower() == ".pdf":
        try:
            run_id = start_pdf_import(
                session,
                source,
                f"Open this PDF in Ogent: {source}",
            )
            message = (
                "Preparing a protected PDF working copy. "
                "The original PDF will remain untouched."
            )
            session.add_message("assistant", message)
            return {
                "action": "pdf_import",
                "session_id": session.session_id,
                "message": message,
                "source": str(source),
                "run_id": run_id,
            }
        except Exception:
            STATE.release_claim(session, source)
            raise
    try:
        result = open_document(session, str(source))
        result["action"] = "document_opened"
        return result
    except Exception:
        STATE.release_claim(session, source)
        raise


def agent_prompt(message: str, document: Path, source: Path | None) -> str:
    source_note = str(source) if source and source != document else "(the current file is already a working copy)"
    return f"""You are editing this absolute Office document:
{document}

Ogent Lite owns the live preview and source preservation.
- Work single-agent with officecli. Never spawn a team, teammate, or subagent.
- Do NOT run officecli watch, unwatch, open, close, save, or start a preview server.
- Do not use Start-Sleep, sleep, or polling loops.
- Edit only the active working document above. Do not modify the source document: {source_note}
- Preserve existing content and formatting. Before adding a row, section, page, or slide,
  inspect the nearest comparable element and match its fonts, colors, spacing, geometry, and
  visual language; never fall back to an unrelated default layout.
- For PowerPoint additions, prefer cloning and adapting a visually related slide when that
  best preserves the deck's design system. Keep the result presentation-ready, not skeletal.
- The live HTML preview does not render every floating textbox, shape, or anchored image
  exactly. Never conclude that content is missing from the preview alone: verify it with
  officecli get/query before any "restore missing content" action, and never delete or
  rebuild textbox or shape elements unless the user explicitly asked.
- Use officecli help when syntax is uncertain. Apply the requested change, then verify it with
  one targeted officecli readback and officecli validate. For straightforward edits, minimize
  tool calls: inspect only the target, make one focused mutation, read it back, and validate.
  Keep your final answer under six lines and state the concrete change and verification result.

User request:
{message}
"""


def _pipe_reader(
    pipe: Any,
    name: str,
    output_queue: queue.Queue[tuple[str, str | None]],
) -> None:
    for raw in iter(pipe.readline, ""):
        output_queue.put((name, raw.rstrip("\r\n")))
    output_queue.put((name, None))


def _activity_from_codex_event(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type", ""))
    item = event.get("item")
    if event_type == "thread.started":
        return "Codex context started."
    if event_type == "turn.started":
        return "Codex is working."
    if event_type == "turn.completed":
        usage = event.get("usage") or {}
        output = usage.get("output_tokens")
        return f"Codex turn completed{f' ({output} output tokens)' if output is not None else ''}."
    if isinstance(item, dict):
        item_type = item.get("type")
        if item_type == "command_execution":
            command = item.get("command") or item.get("title") or "OfficeCLI command"
            status = item.get("status") or event_type
            return f"{status}: {command}"
        if item_type == "reasoning":
            text = item.get("text") or item.get("summary")
            return str(text) if text else "Reasoning step complete."
        if item_type == "error":
            message = str(item.get("message") or "")
            if "Skill descriptions were shortened" in message:
                return None
            return message
    if event_type == "error":
        return str(event.get("message") or event)
    return None


def _run_codex_once(
    session: SessionState,
    prompt: str,
    document: Path,
    codex_thread_id: str | None,
    model: str,
    reasoning: str,
    run_id: str,
) -> tuple[int, str | None, str | None, list[str]]:
    args = build_codex_command(prompt, codex_thread_id, model, reasoning)

    with session.lock:
        if session.stop_requested or session.run_id != run_id or session.closed:
            return 130, None, None, []
        process = subprocess.Popen(
            args,
            cwd=str(document.parent),
            env=command_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            creationflags=WINDOWS_CHILD_FLAGS if os.name == "nt" else 0,
        )
        session.run_process = process
    assert process.stdout is not None
    assert process.stderr is not None
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stdout_thread = threading.Thread(
        target=_pipe_reader,
        args=(process.stdout, "stdout", output_queue),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pipe_reader,
        args=(process.stderr, "stderr", output_queue),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    closed_streams = 0
    thread_id: str | None = None
    final_text: str | None = None
    stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
    while closed_streams < 2:
        stream_name, line = output_queue.get()
        if line is None:
            closed_streams += 1
            continue
        if not line:
            continue
        if stream_name == "stderr":
            stderr_tail.append(line)
            session.add_activity("stderr", line)
            continue
        try:
            event = json.loads(line)
        except ValueError:
            session.add_activity("codex", line)
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            thread_id = str(event["thread_id"])
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
        ):
            final_text = str(item.get("text") or "").strip() or final_text
        activity = _activity_from_codex_event(event)
        if activity:
            session.add_activity("codex", activity)

    code = process.wait()
    return code, thread_id, final_text, list(stderr_tail)


def _agent_worker(
    session: SessionState,
    message: str,
    document: Path,
    source: Path | None,
    model: str,
    reasoning: str,
    run_id: str,
) -> None:
    started = time.perf_counter()
    try:
        with session.lock:
            if session.stop_requested or session.run_id != run_id or session.closed:
                session.add_message(
                    "assistant",
                    "Stopped. No further agent work is running.",
                )
                _finish_session_run(session, run_id, "stopped", kind="codex")
                return
        ensure_watch(session)
        with session.lock:
            codex_thread_id = session.codex_thread_id
            if session.run_id != run_id:
                return
            session.run_status = "working"
        session.emit("run", {"status": "working", "kind": "codex", "run_id": run_id})
        STATE.broadcast_sessions()
        session.add_activity("codex", f"Using {model} with {reasoning} reasoning.")
        code, new_thread_id, final_text, stderr_tail = _run_codex_once(
            session,
            agent_prompt(message, document, source),
            document,
            codex_thread_id,
            model,
            reasoning,
            run_id,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        with session.lock:
            stopped = session.stop_requested or session.run_id != run_id
            if new_thread_id and session.run_id == run_id:
                session.codex_thread_id = new_thread_id
        if stopped:
            session.add_message("assistant", "Stopped. No further agent work is running.")
            _finish_session_run(
                session,
                run_id,
                "stopped",
                kind="codex",
                elapsed_ms=elapsed_ms,
            )
            return
        if code != 0:
            detail = "\n".join(stderr_tail[-6:]).strip()
            message_text = f"Codex exited with code {code}."
            if detail:
                message_text += f" {detail}"
            with session.lock:
                session.last_error = message_text
            session.add_message("assistant", message_text)
            _finish_session_run(
                session,
                run_id,
                "error",
                kind="codex",
                exit_code=code,
                elapsed_ms=elapsed_ms,
            )
            return
        if not final_text:
            final_text = "The document task completed. Review the live document on the left."
        session.add_message("assistant", final_text)
        ensure_watch(session)
        session.emit(
            "document",
            {
                "source": str(source) if source else None,
                "working": str(document),
                "watch_url": (
                    f"http://{HOST}:{session.watch_port}/?refresh={time.time_ns()}"
                    if session.watch_port
                    else None
                ),
                "complex_layout": session.complex_layout,
                "complex_layout_detail": session.complex_layout_detail,
            },
        )
        _finish_session_run(
            session,
            run_id,
            "idle",
            kind="codex",
            exit_code=0,
            elapsed_ms=elapsed_ms,
        )
    except Exception as exc:
        with session.lock:
            stopped = session.stop_requested or session.run_id != run_id
        if stopped:
            session.add_message("assistant", "Stopped. No further agent work is running.")
            _finish_session_run(session, run_id, "stopped", kind="codex")
        else:
            with session.lock:
                session.last_error = str(exc)
            session.add_message("assistant", f"The document run failed: {exc}")
            _finish_session_run(session, run_id, "error", kind="codex")


def start_agent_run(
    session: SessionState,
    message: str,
    model: str,
    reasoning: str,
) -> str:
    selected_model, selected_reasoning = validate_agent_settings(model, reasoning)
    with session.lock:
        if session.closed:
            raise UserFacingError("This Ogent session has closed.", 410)
        if session.run_status in ACTIVE_RUN_STATUSES:
            raise UserFacingError("Ogent is still working. Stop that run or wait for it to finish.", 409)
        if session.snapshot_in_progress:
            raise UserFacingError("Word view is still being generated. Wait for it to finish.", 409)
        document = session.active_doc
        source = session.active_source
        if not document:
            raise UserFacingError("Open an Office document first.", 409)
        session.run_status = "starting"
        session.run_id = uuid.uuid4().hex
        session.stop_requested = False
        run_id = session.run_id
    session.add_message("user", message)
    session.emit(
        "run",
        {
            "status": "starting",
            "kind": "codex",
            "run_id": run_id,
            "model": selected_model,
            "reasoning": selected_reasoning,
        },
    )
    STATE.broadcast_sessions()
    thread = threading.Thread(
        target=_agent_worker,
        args=(
            session,
            message,
            document,
            source,
            selected_model,
            selected_reasoning,
            run_id,
        ),
        name=f"ogent-codex-{session.session_id}-{run_id[:8]}",
        daemon=True,
    )
    with session.lock:
        session.run_thread = thread
    thread.start()
    return run_id


def handle_chat_message(
    session: SessionState,
    message: str,
    model: Any = DEFAULT_MODEL,
    reasoning: Any = DEFAULT_REASONING,
) -> tuple[int, dict[str, Any]]:
    text = message.strip()
    if not text:
        raise UserFacingError("Type a request first.")
    selected_model, selected_reasoning = validate_agent_settings(model, reasoning)
    with session.lock:
        has_document = session.active_doc is not None
    if has_document:
        run_id = start_agent_run(
            session,
            text,
            selected_model,
            selected_reasoning,
        )
        return 202, {
            "message": "Run started.",
            "run_id": run_id,
            "model": selected_model,
            "reasoning": selected_reasoning,
        }

    session.add_message("user", text)
    pdf_path = extract_pdf_path(text)
    if pdf_path:
        deduped = STATE.claim_source(session, pdf_path)
        if deduped is not None:
            return 200, {
                "action": "focus_session",
                "session_id": deduped.session_id,
                "url": f"/?s={deduped.session_id}",
                "message": "That PDF is already open in another Ogent session.",
            }
        try:
            run_id = start_pdf_import(session, pdf_path, text)
        except Exception:
            STATE.release_claim(session, pdf_path)
            raise
        return 202, {
            "message": "Preparing a protected PDF working copy.",
            "run_id": run_id,
        }
    if "pdf" in text.casefold():
        with session.lock:
            session.pending_pdf = True
        response = (
            "Paste the absolute PDF path here. I will copy it, convert the copy through the "
            "Word-first PDF pipeline, and open the working DOCX on the left. The original will remain untouched."
        )
    else:
        response = (
            "Open a .docx, .xlsx, or .pptx using the path field above. "
            "For a PDF, ask me to edit it and then paste its absolute path."
        )
    session.add_message("assistant", response)
    return 200, {"message": response}


def stop_active_run(session: SessionState) -> bool:
    with session.lock:
        process = session.run_process
        active = session.run_status in ACTIVE_RUN_STATUSES
        if not active:
            return False
        session.stop_requested = True
        session.run_status = "stopping"
        run_id = session.run_id
    session.emit("run", {"status": "stopping", "run_id": run_id})
    STATE.broadcast_sessions()
    terminate_process_tree(process)
    return True


def generate_word_snapshot(session: SessionState) -> Path:
    with session.lock:
        if session.closed:
            raise UserFacingError("This Ogent session has closed.", 410)
        if session.run_status in ACTIVE_RUN_STATUSES:
            raise UserFacingError(
                "Wait for the active Codex run to finish before creating Word view.",
                409,
            )
        if session.snapshot_in_progress:
            raise UserFacingError("Word view is already being generated.", 409)
        document = session.active_doc
        if document is None:
            raise UserFacingError("Open a Word document first.", 409)
        if document.suffix.lower() != ".docx":
            raise UserFacingError("Word view is currently available only for DOCX files.", 415)
        if not document.exists():
            raise UserFacingError(f"The working document no longer exists: {document}", 404)
        session.snapshot_in_progress = True
        output = document.parent / f"{safe_name(document.stem)}-word-view.pdf"
        pid_file = document.parent / f".{safe_name(document.stem)}-word-process.pid"
        with contextlib.suppress(OSError):
            pid_file.unlink()
        session.snapshot_complete.clear()
        session.snapshot_pid_file = pid_file
        session.snapshot_path = None
    session.emit("snapshot_status", {"status": "working"})

    process: subprocess.Popen[str] | None = None
    try:
        with session.lock:
            if session.closed:
                raise UserFacingError("This Ogent session has closed.", 410)
            process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(DOCX_TO_PDF),
                    "-Docx",
                    str(document),
                    "-OutPdf",
                    str(output),
                    "-Engine",
                    "Word",
                    "-Force",
                    "-WordPidFile",
                    str(pid_file),
                ],
                cwd=str(REPO_ROOT),
                env=command_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=WINDOWS_CHILD_FLAGS if os.name == "nt" else 0,
            )
            session.snapshot_process = process
        try:
            stdout, _ = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            raise UserFacingError("Word view timed out after 120 seconds.", 504) from None
        if process.returncode != 0:
            tail = "\n".join((stdout or "").splitlines()[-8:])
            raise UserFacingError(
                f"Word view failed with exit code {process.returncode}. {tail}".strip(),
                500,
            )
        if not output.is_file() or output.stat().st_size <= 5:
            raise UserFacingError("Word view did not create a valid PDF.", 500)
        with output.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise UserFacingError("Word view output is not a valid PDF.", 500)
        with session.lock:
            session.snapshot_path = output
            session.snapshot_in_progress = False
            session.snapshot_process = None
            if session.sse_clients == 0:
                session.orphan_since = time.time()
        session.emit(
            "snapshot_status",
            {
                "status": "ready",
                "url": f"/snapshot.pdf?s={session.session_id}",
            },
        )
        return output
    except UserFacingError:
        with session.lock:
            session.snapshot_in_progress = False
            if session.snapshot_process is process:
                session.snapshot_process = None
            if session.sse_clients == 0:
                session.orphan_since = time.time()
        session.emit("snapshot_status", {"status": "error"})
        raise
    except Exception as exc:
        with session.lock:
            session.snapshot_in_progress = False
            if session.snapshot_process is process:
                session.snapshot_process = None
            session.last_error = str(exc)
            if session.sse_clients == 0:
                session.orphan_since = time.time()
        session.emit("snapshot_status", {"status": "error"})
        raise UserFacingError(f"Word view failed: {exc}", 500) from exc
    finally:
        cleanup_word_snapshot_process(process, pid_file)
        with session.lock:
            if session.snapshot_pid_file == pid_file:
                session.snapshot_pid_file = None
        session.snapshot_complete.set()


def pick_document_path() -> str | None:
    if not STATE.pick_lock.acquire(blocking=False):
        raise UserFacingError("A document picker is already open.", 409)
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form
$owner.Text = 'Ogent document picker'
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedToolWindow
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0.01
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Open in Ogent'
$dialog.Filter = 'Supported documents (*.docx;*.xlsx;*.pptx;*.pdf)|*.docx;*.xlsx;*.pptx;*.pdf|Word (*.docx)|*.docx|Excel (*.xlsx)|*.xlsx|PowerPoint (*.pptx)|*.pptx|PDF (*.pdf)|*.pdf'
$dialog.Multiselect = $false
try {
    $owner.Show()
    $owner.Activate()
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Out.Write($dialog.FileName)
    }
}
finally {
    $dialog.Dispose()
    $owner.Close()
    $owner.Dispose()
}
"""
    process: subprocess.Popen[str] | None = None
    try:
        with STATE.registry_lock:
            if STATE.shutdown_requested:
                raise UserFacingError("Ogent is shutting down.", 503)
            process = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                cwd=str(REPO_ROOT),
                env=command_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            STATE.pick_process = process
        stdout, stderr = process.communicate(timeout=600)
        if process.returncode != 0:
            detail = "\n".join((stderr or "").splitlines()[-6:])
            raise UserFacingError(f"Document picker failed. {detail}".strip(), 500)
        selected = (stdout or "").strip()
        return selected or None
    except subprocess.TimeoutExpired:
        terminate_process_tree(process)
        raise UserFacingError("Document picker timed out.", 504) from None
    finally:
        with STATE.registry_lock:
            if STATE.pick_process is process:
                STATE.pick_process = None
        STATE.pick_lock.release()


def close_session(
    session: SessionState,
    *,
    require_reapable_at: float | None = None,
) -> bool:
    # Mark the session closed before resource cleanup so no new request can
    # enter it, but keep it in summaries until its watch port is actually
    # released. This avoids both premature "removed" announcements and holding
    # the global registry lock while waiting for child processes.
    initiated = STATE.begin_session_close(
        session,
        require_reapable_at=require_reapable_at,
    )
    if not initiated and require_reapable_at is not None:
        return False
    # Explicit shutdown may arrive while the reaper is already closing this
    # session. Serialize cleanup so shutdown waits for (or safely takes over)
    # the bounded child-process/watch teardown instead of exiting around it.
    with session.close_lock:
        if session.close_complete.is_set():
            return True
        try:
            with session.lock:
                run_process = session.run_process
                snapshot_process = session.snapshot_process
                snapshot_busy = session.snapshot_in_progress
                snapshot_pid_file = session.snapshot_pid_file
            terminate_process_tree(run_process)
            if snapshot_busy:
                # Let Word COM reach the converter's finally block and quit
                # cleanly. If it exceeds the bounded grace, terminate both the
                # PowerShell wrapper and its exact tracked automation instance.
                snapshot_finished = session.snapshot_complete.wait(
                    SNAPSHOT_SHUTDOWN_GRACE_SECONDS
                )
                if not snapshot_finished:
                    with session.lock:
                        snapshot_process = session.snapshot_process
                        snapshot_pid_file = session.snapshot_pid_file
                    cleanup_word_snapshot_process(
                        snapshot_process,
                        snapshot_pid_file,
                    )
                    session.snapshot_complete.wait(timeout=5)
            else:
                cleanup_word_snapshot_process(
                    snapshot_process,
                    snapshot_pid_file,
                )
            stop_watch(session, clear_document=False, release_port=True)
        finally:
            STATE.finish_session_close(session)
    return True


def reaper_loop(server: "OgentServer") -> None:
    while not STATE.shutdown_requested:
        if threading.Event().wait(STATE.reaper_tick_seconds):
            return
        now = time.time()
        with STATE.registry_lock:
            sessions = list(STATE.sessions.values())
        for session in sessions:
            close_session(session, require_reapable_at=now)

        should_shutdown = False
        with STATE.registry_lock:
            empty_since = STATE.empty_since
            idle_exit_minutes = STATE.idle_exit_minutes
            if (
                not STATE.sessions
                and idle_exit_minutes > 0
                and empty_since is not None
                and now - empty_since >= idle_exit_minutes * 60
            ):
                STATE.shutdown_requested = True
                should_shutdown = True
        if should_shutdown:
            server.shutdown()
            return


def cleanup() -> None:
    with STATE.registry_lock:
        if STATE.cleanup_started:
            return
        STATE.cleanup_started = True
        STATE.shutdown_requested = True
        sessions = list(STATE.sessions.values())
        pick_process = STATE.pick_process
    terminate_process_tree(pick_process)
    for session in sessions:
        close_session(session)
    try:
        if SERVER_INFO_PATH.exists():
            info = json.loads(SERVER_INFO_PATH.read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                SERVER_INFO_PATH.unlink()
    except (OSError, ValueError):
        pass


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%2317324d'/%3E%3Cstop offset='1' stop-color='%230d9488'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect x='8' y='8' width='240' height='240' rx='56' fill='url(%23g)'/%3E%3Ccircle cx='128' cy='120' r='66' fill='none' stroke='white' stroke-width='30'/%3E%3Ccircle cx='175' cy='167' r='16' fill='%2314b8a6' stroke='white' stroke-width='3'/%3E%3C/svg%3E">
  <title>Ogent Lite</title>
  <style>
    :root {
      color-scheme: light dark;
      --navy: #17324d;
      --navy-2: #0e2235;
      --teal: #0d9488;
      --teal-2: #14b8a6;
      --paper: #f8fafc;
      --panel: rgba(255,255,255,.94);
      --ink: #172033;
      --muted: #667085;
      --line: #d8e0ea;
      --soft: #eef3f8;
      --danger: #c63c4a;
      --shadow: 0 18px 50px rgba(15, 35, 55, .13);
      --left: 68%;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --paper: #0b1420;
        --panel: rgba(16,27,41,.96);
        --ink: #edf3f9;
        --muted: #9fb0c2;
        --line: #293b4e;
        --soft: #142334;
        --shadow: 0 20px 60px rgba(0,0,0,.38);
      }
    }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; }
    body {
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      background: var(--paper);
      color: var(--ink);
    }
    button, textarea, input, select { font: inherit; }
    button { cursor: pointer; }
    .workspace { display: flex; width: 100vw; height: 100vh; }
    .document-pane {
      flex: 0 0 var(--left);
      min-width: 45%;
      background:
        radial-gradient(circle at 12% 5%, rgba(20,184,166,.10), transparent 25%),
        linear-gradient(145deg, #e8eef5 0%, #f4f7fa 55%, #e8edf3 100%);
      display: flex;
      flex-direction: column;
      position: relative;
    }
    @media (prefers-color-scheme: dark) {
      .document-pane {
        background:
          radial-gradient(circle at 12% 5%, rgba(20,184,166,.14), transparent 25%),
          linear-gradient(145deg, #0a1521, #101c29 55%, #0b1622);
      }
    }
    .document-toolbar {
      min-height: 50px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 7px 14px;
      background: var(--navy);
      color: #fff;
      border-bottom: 3px solid var(--teal-2);
      box-shadow: 0 4px 18px rgba(9, 30, 48, .16);
      z-index: 2;
    }
    .brand-mark {
      width: 28px;
      height: 28px;
      flex: 0 0 28px;
      display: block;
    }
    .brand-mark svg, .empty-document .symbol svg { display: block; width: 100%; height: 100%; }
    .doc-title {
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
      font-size: 13px;
      font-weight: 650;
      letter-spacing: .01em;
    }
    .doc-title small {
      display: block;
      font-size: 10px;
      color: #bcd0df;
      font-weight: 500;
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    .complex-note {
      display: none;
      margin-top: 2px;
      max-width: 560px;
      overflow: hidden;
      color: #fde68a;
      font-size: 9px;
      font-weight: 550;
      letter-spacing: 0;
      text-overflow: ellipsis;
      text-transform: none;
    }
    .complex-note.visible { display: block; }
    .session-controls {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-left: auto;
    }
    .session-select {
      width: min(220px, 18vw);
      border: 1px solid rgba(255,255,255,.22);
      border-radius: 8px;
      padding: 5px 7px;
      background: rgba(255,255,255,.09);
      color: #fff;
      font-size: 10px;
      outline: none;
    }
    .session-select option { color: #172033; background: #fff; }
    .status-cluster { display: flex; align-items: center; gap: 7px; }
    .status-dot {
      width: 9px; height: 9px; border-radius: 999px; background: #94a3b8;
      box-shadow: 0 0 0 4px rgba(148,163,184,.14);
    }
    .status-dot.ready { background: #34d399; box-shadow: 0 0 0 4px rgba(52,211,153,.15); }
    .status-dot.busy { background: #fbbf24; box-shadow: 0 0 0 4px rgba(251,191,36,.15); }
    .status-dot.error { background: #fb7185; box-shadow: 0 0 0 4px rgba(251,113,133,.15); }
    .status-text { color: #dce9f2; font-size: 11px; }
    .icon-button {
      border: 1px solid rgba(255,255,255,.22);
      background: rgba(255,255,255,.08);
      color: #fff;
      border-radius: 8px;
      padding: 6px 9px;
      line-height: 1;
    }
    .icon-button:hover { background: rgba(255,255,255,.16); }
    .icon-button:disabled { opacity: .4; cursor: default; }
    .new-window { white-space: nowrap; font-size: 10px; }
    .preview-shell { position: relative; flex: 1; min-height: 0; padding: 14px; }
    #preview {
      width: 100%; height: 100%; border: 0; border-radius: 12px; background: #fff;
      box-shadow: var(--shadow); display: none;
    }
    .empty-document {
      width: min(560px, 75%);
      position: absolute;
      left: 50%; top: 48%;
      transform: translate(-50%, -50%);
      padding: 42px 40px;
      text-align: center;
      border-radius: 20px;
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
    }
    .empty-document .symbol {
      width: 70px; height: 70px; margin: 0 auto 18px; display: block;
    }
    .empty-document h1 { font-size: 26px; margin: 0 0 8px; letter-spacing: -.02em; }
    .empty-document p { color: var(--muted); margin: 0; line-height: 1.6; }
    .splitter {
      flex: 0 0 7px;
      cursor: col-resize;
      background: var(--line);
      position: relative;
      z-index: 5;
    }
    .splitter::after {
      content: "";
      position: absolute; inset: 0 2px;
      background: var(--teal);
      opacity: 0;
      transition: opacity .15s ease;
    }
    .splitter:hover::after, .splitter.dragging::after { opacity: 1; }
    .chat-pane {
      flex: 1 1 auto;
      min-width: 280px;
      background: var(--panel);
      display: grid;
      grid-template-rows: auto auto minmax(0,1fr) auto auto;
      min-height: 0;
    }
    .chat-header {
      padding: 18px 18px 14px;
      border-bottom: 1px solid var(--line);
      display: flex; align-items: center; gap: 12px;
    }
    .chat-header h2 { margin: 0; font-size: 17px; letter-spacing: -.01em; }
    .chat-header p { margin: 2px 0 0; font-size: 11px; color: var(--muted); }
    .lite-badge {
      margin-left: auto; color: var(--teal); border: 1px solid color-mix(in srgb, var(--teal) 40%, transparent);
      background: color-mix(in srgb, var(--teal) 10%, transparent);
      border-radius: 999px; padding: 4px 8px; font-size: 10px; font-weight: 750;
      letter-spacing: .08em;
    }
    .open-panel { padding: 12px 14px; border-bottom: 1px solid var(--line); background: var(--soft); }
    .open-line { display: flex; gap: 7px; }
    .path-field, .recent-select {
      width: 100%; min-width: 0; border: 1px solid var(--line); border-radius: 9px;
      background: var(--panel); color: var(--ink); outline: none;
    }
    .path-field { padding: 9px 10px; font-size: 12px; }
    .recent-select { margin-top: 7px; padding: 7px 9px; font-size: 11px; color: var(--muted); }
    .path-field:focus, textarea:focus { border-color: var(--teal); box-shadow: 0 0 0 3px rgba(13,148,136,.11); }
    .primary {
      border: 0; border-radius: 9px; background: var(--teal); color: #fff;
      padding: 9px 13px; font-weight: 700;
    }
    .primary:hover { background: #0b8178; }
    .secondary {
      border: 1px solid var(--line); border-radius: 9px; background: var(--panel);
      color: var(--ink); padding: 9px 11px; font-weight: 650;
    }
    .secondary:hover { border-color: var(--teal); color: var(--teal); }
    .transcript { padding: 16px 14px 20px; overflow-y: auto; min-height: 0; }
    .message { display: flex; margin: 0 0 13px; }
    .message.user { justify-content: flex-end; }
    .bubble {
      max-width: 88%; border-radius: 14px; padding: 10px 12px; font-size: 12.5px;
      line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere;
    }
    .assistant .bubble { background: var(--soft); border: 1px solid var(--line); border-top-left-radius: 4px; }
    .user .bubble { background: var(--navy); color: #fff; border-top-right-radius: 4px; }
    .activity {
      margin: 0 14px 9px; border: 1px solid var(--line); border-radius: 10px;
      background: var(--soft); overflow: hidden;
    }
    .activity summary { padding: 8px 10px; cursor: pointer; color: var(--muted); font-size: 11px; }
    .activity pre {
      max-height: 130px; overflow: auto; margin: 0; padding: 9px 10px;
      border-top: 1px solid var(--line); font: 10px/1.5 ui-monospace, "Cascadia Mono", Consolas, monospace;
      white-space: pre-wrap; color: var(--muted);
    }
    .composer { border-top: 1px solid var(--line); padding: 12px 14px 14px; }
    .agent-settings {
      display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, .85fr);
      gap: 8px; margin-bottom: 8px;
    }
    .setting-field { min-width: 0; }
    .setting-field span {
      display: block; margin: 0 0 4px; color: var(--muted); font-size: 9px;
      font-weight: 750; letter-spacing: .07em; text-transform: uppercase;
    }
    .agent-select {
      width: 100%; min-width: 0; border: 1px solid var(--line); border-radius: 9px;
      padding: 7px 9px; background: var(--panel); color: var(--ink); outline: none;
      font-size: 11px;
    }
    .agent-select:focus {
      border-color: var(--teal); box-shadow: 0 0 0 3px rgba(13,148,136,.11);
    }
    .agent-select:disabled { opacity: .58; cursor: default; }
    textarea {
      width: 100%; min-height: 74px; max-height: 180px; resize: vertical; border: 1px solid var(--line);
      border-radius: 11px; padding: 10px 11px; background: var(--panel); color: var(--ink); outline: none;
      font-size: 12.5px; line-height: 1.45;
    }
    .composer-actions { display: flex; align-items: center; gap: 7px; margin-top: 8px; }
    .hint { color: var(--muted); font-size: 10px; margin-right: auto; }
    .stop {
      border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--line));
      color: var(--danger); background: transparent; border-radius: 9px; padding: 8px 11px; font-weight: 700;
    }
    .stop:disabled { opacity: .38; cursor: default; }
    .send { min-width: 74px; }
    .toast {
      position: fixed; left: 18px; bottom: 18px; z-index: 20; max-width: min(520px, calc(100vw - 36px));
      background: var(--navy-2); color: #fff; padding: 11px 14px; border-radius: 10px;
      box-shadow: var(--shadow); font-size: 12px; opacity: 0; transform: translateY(12px);
      pointer-events: none; transition: .18s ease;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    @media (max-width: 820px) {
      :root { --left: 58%; }
      .chat-pane { min-width: 300px; }
      .status-text { display: none; }
      .session-select { width: 120px; }
      .new-window { display: none; }
    }
  </style>
</head>
<body>
  <main class="workspace" id="workspace">
    <section class="document-pane" id="documentPane" aria-label="Live document">
      <header class="document-toolbar">
        <div class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 256 256" focusable="false">
            <defs>
              <linearGradient id="toolbar-mark-gradient" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stop-color="#17324d"/>
                <stop offset="1" stop-color="#0d9488"/>
              </linearGradient>
            </defs>
            <rect x="8" y="8" width="240" height="240" rx="56" fill="url(#toolbar-mark-gradient)"/>
            <circle cx="128" cy="120" r="66" fill="none" stroke="#fff" stroke-width="30"/>
            <circle cx="175" cy="167" r="16" fill="#14b8a6" stroke="#fff" stroke-width="3"/>
          </svg>
        </div>
        <div class="doc-title">
          <small>Live working copy</small>
          <span id="documentName">No document open</span>
          <span class="complex-note" id="complexNote">Complex layout detected — live view approximates floating shapes. Use Word view for exact rendering.</span>
        </div>
        <div class="session-controls">
          <select class="session-select" id="sessionSelect" aria-label="Open Ogent sessions"></select>
          <button class="icon-button new-window" id="newWindowButton" type="button" title="Open an independent Ogent workspace">+ New window</button>
        </div>
        <div class="status-cluster">
          <span class="status-dot" id="statusDot"></span>
          <span class="status-text" id="statusText">Ready to open a document</span>
          <button class="icon-button" id="wordViewButton" type="button" title="Open a Word-accurate PDF view" hidden>Word view</button>
          <button class="icon-button" id="reloadPreview" type="button" title="Reload preview">↻</button>
        </div>
      </header>
      <div class="preview-shell">
        <div class="empty-document" id="emptyDocument">
          <div class="symbol" aria-hidden="true">
            <svg viewBox="0 0 256 256" focusable="false">
              <defs>
                <linearGradient id="empty-mark-gradient" x1="0" y1="0" x2="1" y2="1">
                  <stop offset="0" stop-color="#17324d"/>
                  <stop offset="1" stop-color="#0d9488"/>
                </linearGradient>
              </defs>
              <rect x="8" y="8" width="240" height="240" rx="56" fill="url(#empty-mark-gradient)"/>
              <circle cx="128" cy="120" r="66" fill="none" stroke="#fff" stroke-width="30"/>
              <circle cx="175" cy="167" r="16" fill="#14b8a6" stroke="#fff" stroke-width="3"/>
            </svg>
          </div>
          <h1>Your document, live.</h1>
          <p>Paste a Word, Excel, or PowerPoint path on the right. Ogent creates a protected working copy, opens it here, and keeps every AI edit visible.</p>
        </div>
        <iframe id="preview" title="OfficeCLI live preview"></iframe>
      </div>
    </section>
    <div class="splitter" id="splitter" role="separator" aria-orientation="vertical" aria-label="Resize panes"></div>
    <aside class="chat-pane" aria-label="Ogent chat">
      <header class="chat-header">
        <div>
          <h2>Ogent</h2>
          <p>Plain-language Office editing</p>
        </div>
        <span class="lite-badge">LITE</span>
      </header>
      <section class="open-panel" aria-label="Open document">
        <div class="open-line">
          <input class="path-field" id="pathInput" type="text" placeholder="D:\Reports\document.docx" autocomplete="off">
          <button class="secondary" id="browseButton" type="button">Browse…</button>
          <button class="primary" id="openButton" type="button">Open</button>
        </div>
        <select class="recent-select" id="recentSelect" aria-label="Recent documents">
          <option value="">Recent documents</option>
        </select>
      </section>
      <section class="transcript" id="transcript" aria-live="polite"></section>
      <details class="activity" id="activity">
        <summary id="activitySummary">Agent activity</summary>
        <pre id="activityLog"></pre>
      </details>
      <section class="composer">
        <div class="agent-settings" aria-label="Agent settings">
          <label class="setting-field">
            <span>Model</span>
            <select class="agent-select" id="modelSelect" aria-label="Codex model">
              <option value="gpt-5.6-sol" selected>GPT-5.6 Sol</option>
              <option value="gpt-5.6-terra">GPT-5.6 Terra</option>
            </select>
          </label>
          <label class="setting-field">
            <span>Reasoning</span>
            <select class="agent-select" id="reasoningSelect" aria-label="Reasoning effort">
              <option value="low">Low</option>
              <option value="medium" selected>Medium</option>
              <option value="high">High</option>
              <option value="xhigh">XHigh</option>
              <option value="max">Max</option>
              <option value="ultra">Ultra</option>
            </select>
          </label>
        </div>
        <textarea id="messageInput" placeholder="Tell Ogent what to change…" aria-label="Document request"></textarea>
        <div class="composer-actions">
          <span class="hint">Enter to send · Shift+Enter for a new line</span>
          <button class="stop" id="stopButton" type="button" disabled>Stop</button>
          <button class="primary send" id="sendButton" type="button">Send</button>
        </div>
      </section>
    </aside>
  </main>
  <div class="toast" id="toast" role="status"></div>
  <script nonce="__NONCE__">
    const TOKEN = "__TOKEN__";
    const SESSION_ID = "__SESSION_ID__";
    const CLIENT_ID =
      (globalThis.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
    const AGENT_SETTINGS_KEY = "ogent-agent-settings-v1";
    const elements = {
      path: document.getElementById("pathInput"),
      open: document.getElementById("openButton"),
      browse: document.getElementById("browseButton"),
      recent: document.getElementById("recentSelect"),
      session: document.getElementById("sessionSelect"),
      newWindow: document.getElementById("newWindowButton"),
      transcript: document.getElementById("transcript"),
      input: document.getElementById("messageInput"),
      model: document.getElementById("modelSelect"),
      reasoning: document.getElementById("reasoningSelect"),
      send: document.getElementById("sendButton"),
      stop: document.getElementById("stopButton"),
      preview: document.getElementById("preview"),
      empty: document.getElementById("emptyDocument"),
      documentName: document.getElementById("documentName"),
      statusDot: document.getElementById("statusDot"),
      statusText: document.getElementById("statusText"),
      complexNote: document.getElementById("complexNote"),
      wordView: document.getElementById("wordViewButton"),
      reload: document.getElementById("reloadPreview"),
      activity: document.getElementById("activity"),
      activitySummary: document.getElementById("activitySummary"),
      activityLog: document.getElementById("activityLog"),
      toast: document.getElementById("toast"),
      splitter: document.getElementById("splitter")
    };
    let state = {
      session_id: SESSION_ID,
      active_document: null,
      watch_url: null,
      run_status: "idle",
      recent: [],
      sessions: [],
      transcript: []
    };
    let repairing = false;
    let toastTimer = null;
    let closeSent = false;

    function scopedPath(path) {
      const url = new URL(path, window.location.origin);
      if (!url.searchParams.has("s")) url.searchParams.set("s", SESSION_ID);
      return `${url.pathname}${url.search}`;
    }

    async function api(path, options = {}) {
      const headers = Object.assign({}, options.headers || {});
      if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
      if ((options.method || "GET") !== "GET") {
        headers["X-Ogent-Token"] = TOKEN;
        headers["X-Ogent-Session"] = SESSION_ID;
      }
      const response = await fetch(
        scopedPath(path),
        Object.assign({}, options, { headers })
      );
      let payload = {};
      try { payload = await response.json(); } catch (_) {}
      if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
      return payload;
    }

    function showToast(message) {
      elements.toast.textContent = message;
      elements.toast.classList.add("show");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => elements.toast.classList.remove("show"), 3600);
    }

    function appendMessage(message) {
      const row = document.createElement("div");
      row.className = `message ${message.role === "user" ? "user" : "assistant"}`;
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = message.text;
      row.appendChild(bubble);
      elements.transcript.appendChild(row);
      elements.transcript.scrollTop = elements.transcript.scrollHeight;
    }

    function renderTranscript(messages) {
      elements.transcript.replaceChildren();
      for (const message of messages || []) appendMessage(message);
    }

    function renderRecent(items) {
      const current = elements.recent.value;
      elements.recent.replaceChildren(new Option("Recent documents", ""));
      for (const item of items || []) {
        const name = item.split(/[\\/]/).pop() || item;
        elements.recent.add(new Option(name, item));
      }
      if ([...elements.recent.options].some(option => option.value === current)) {
        elements.recent.value = current;
      }
    }

    function renderSessions(items) {
      elements.session.replaceChildren();
      for (const item of items || []) {
        const status = item.run_status || "idle";
        const label = `${item.document_name || "New workspace"} — ${status}`;
        elements.session.add(new Option(label, item.id));
      }
      if ([...elements.session.options].some(option => option.value === SESSION_ID)) {
        elements.session.value = SESSION_ID;
      }
    }

    function renderDocumentControls() {
      const active = state.active_document || "";
      const isDocx = active.toLowerCase().endsWith(".docx");
      elements.wordView.hidden = !isDocx;
      elements.complexNote.classList.toggle(
        "visible",
        Boolean(isDocx && state.complex_layout)
      );
      if (state.complex_layout_detail) {
        elements.complexNote.title = state.complex_layout_detail;
      }
    }

    function optionExists(select, value) {
      return [...select.options].some(option => option.value === value);
    }

    function loadAgentSettings() {
      try {
        const saved = JSON.parse(localStorage.getItem(AGENT_SETTINGS_KEY) || "{}");
        if (optionExists(elements.model, saved.model)) elements.model.value = saved.model;
        if (optionExists(elements.reasoning, saved.reasoning)) elements.reasoning.value = saved.reasoning;
      } catch (_) {}
    }

    function saveAgentSettings() {
      try {
        localStorage.setItem(
          AGENT_SETTINGS_KEY,
          JSON.stringify({ model: elements.model.value, reasoning: elements.reasoning.value })
        );
      } catch (_) {}
    }

    function setPreview(path, url) {
      if (!path) {
        elements.preview.style.display = "none";
        elements.empty.style.display = "block";
        elements.documentName.textContent = "No document open";
        renderDocumentControls();
        return;
      }
      elements.empty.style.display = "none";
      elements.preview.style.display = "block";
      elements.documentName.textContent = path.split(/[\\/]/).pop() || path;
      const target = url || state.watch_url;
      if (target && elements.preview.src !== target) elements.preview.src = target;
      renderDocumentControls();
    }

    function setRunStatus(status) {
      state.run_status = status;
      const busy = ["starting", "working", "stopping"].includes(status);
      const snapshotBusy = Boolean(state.snapshot_in_progress);
      elements.stop.disabled = !busy;
      elements.send.disabled = busy || snapshotBusy;
      elements.open.disabled = busy || snapshotBusy;
      elements.browse.disabled = busy || snapshotBusy;
      elements.model.disabled = busy;
      elements.reasoning.disabled = busy;
      elements.wordView.disabled = busy || snapshotBusy;
      elements.statusDot.className = `status-dot ${busy ? "busy" : status === "error" ? "error" : state.watch_alive ? "ready" : ""}`;
      elements.statusText.textContent =
        snapshotBusy ? "Rendering Word view…" :
        status === "working" ? "Codex is editing…" :
        status === "starting" ? "Starting Codex…" :
        status === "stopping" ? "Stopping…" :
        status === "error" ? "Action needed" :
        state.active_document ? (state.watch_alive ? "Live preview connected" : "Preview reconnecting") :
        "Ready to open a document";
      elements.activitySummary.textContent = busy ? "Agent activity · working…" : "Agent activity";
      renderDocumentControls();
    }

    function applySnapshot(snapshot) {
      state = Object.assign(state, snapshot);
      renderTranscript(state.transcript || []);
      renderRecent(state.recent || []);
      renderSessions(state.sessions || []);
      setPreview(
        state.active_document,
        state.active_document && state.watch_url
          ? `${state.watch_url}?v=${Date.now()}`
          : null
      );
      setRunStatus(state.run_status || "idle");
    }

    function appendActivity(data) {
      const prefix = data.stream ? `[${data.stream}] ` : "";
      elements.activityLog.textContent += `${prefix}${data.text}\n`;
      const lines = elements.activityLog.textContent.split("\n");
      if (lines.length > 180) elements.activityLog.textContent = lines.slice(-180).join("\n");
      elements.activityLog.scrollTop = elements.activityLog.scrollHeight;
    }

    function handleEvent(event) {
      const payload = JSON.parse(event.data);
      const type = payload.type;
      const data = payload.data || {};
      if (type === "snapshot") applySnapshot(data);
      else if (type === "message") appendMessage(data);
      else if (type === "activity") appendActivity(data);
      else if (type === "recent") { state.recent = data.items || []; renderRecent(state.recent); }
      else if (type === "sessions") {
        state.sessions = data.items || [];
        renderSessions(state.sessions);
      }
      else if (type === "run") setRunStatus(data.status);
      else if (type === "watch") {
        state.watch_alive = data.status === "ready";
        if (data.port) state.watch_url = `http://127.0.0.1:${data.port}/`;
        setRunStatus(state.run_status || "idle");
      } else if (type === "document") {
        state.active_document = data.working;
        state.watch_url = data.watch_url || state.watch_url;
        state.complex_layout = Boolean(data.complex_layout);
        state.complex_layout_detail = data.complex_layout_detail || null;
        state.watch_alive = true;
        setPreview(data.working, data.watch_url);
        setRunStatus(state.run_status || "idle");
      } else if (type === "snapshot_status") {
        state.snapshot_in_progress = data.status === "working";
        setRunStatus(state.run_status || "idle");
      }
    }

    const eventSource = new EventSource(
      `/events?s=${encodeURIComponent(SESSION_ID)}` +
      `&token=${encodeURIComponent(TOKEN)}` +
      `&client=${encodeURIComponent(CLIENT_ID)}`
    );
    eventSource.onmessage = event => {
      try { handleEvent(event); } catch (error) { console.error(error); }
    };
    eventSource.onerror = () => {
      state.watch_alive = false;
      setRunStatus(state.run_status || "idle");
    };

    async function openDocument() {
      const path = elements.path.value.trim();
      if (!path) return showToast("Paste an absolute document path.");
      try {
        elements.open.disabled = true;
        const result = await api("/open", { method: "POST", body: JSON.stringify({ path }) });
        if (result.action === "focus_session" && result.session_id) {
          window.location.assign(`/?s=${encodeURIComponent(result.session_id)}`);
          return;
        }
        if (result.action === "pdf_import") {
          showToast(result.message || "Preparing a protected PDF working copy.");
          return;
        }
        state.active_document = result.active_document;
        state.watch_url = result.watch_url || null;
        state.complex_layout = Boolean(result.complex_layout);
        state.complex_layout_detail = result.complex_layout_detail || null;
        state.watch_alive = true;
        setPreview(result.active_document, `${result.watch_url}?v=${Date.now()}`);
        showToast("Working copy opened. The source remains untouched.");
      } catch (error) {
        showToast(error.message);
      } finally {
        setRunStatus(state.run_status || "idle");
      }
    }

    async function browseDocument() {
      try {
        elements.browse.disabled = true;
        const result = await api("/pick", {
          method: "POST",
          body: "{}"
        });
        if (!result.path) return;
        elements.path.value = result.path;
        await openDocument();
      } catch (error) {
        showToast(error.message);
      } finally {
        setRunStatus(state.run_status || "idle");
      }
    }

    async function openWordView() {
      const popup = window.open("about:blank", "_blank");
      try {
        state.snapshot_in_progress = true;
        setRunStatus(state.run_status || "idle");
        const result = await api("/snapshot", {
          method: "POST",
          body: "{}"
        });
        const snapshotUrl = new URL(result.url || "/snapshot.pdf", window.location.origin);
        snapshotUrl.searchParams.set("s", SESSION_ID);
        snapshotUrl.searchParams.set("token", TOKEN);
        snapshotUrl.searchParams.set("v", Date.now().toString());
        const target = `${snapshotUrl.pathname}${snapshotUrl.search}`;
        if (popup) popup.location.replace(target);
        else window.open(target, "_blank");
      } catch (error) {
        if (popup) popup.close();
        showToast(error.message);
      } finally {
        state.snapshot_in_progress = false;
        setRunStatus(state.run_status || "idle");
      }
    }

    async function sendMessage() {
      const message = elements.input.value.trim();
      if (!message) return;
      try {
        elements.input.value = "";
        const result = await api("/chat", {
          method: "POST",
          body: JSON.stringify({
            message,
            model: elements.model.value,
            reasoning: elements.reasoning.value
          })
        });
        if (result.action === "focus_session" && result.session_id) {
          window.location.assign(`/?s=${encodeURIComponent(result.session_id)}`);
        }
      } catch (error) {
        elements.input.value = message;
        showToast(error.message);
      }
    }

    async function stopRun() {
      try {
        await api("/stop", { method: "POST", body: "{}" });
      } catch (error) {
        showToast(error.message);
      }
    }

    async function repairWatch() {
      if (!state.active_document || repairing) return;
      repairing = true;
      try {
        const result = await api("/watch/restart", { method: "POST", body: "{}" });
        state.watch_alive = true;
        state.watch_url = result.watch_url || state.watch_url;
        if (state.watch_url) {
          elements.preview.src = `${state.watch_url}?v=${Date.now()}`;
        }
      } catch (error) {
        state.watch_alive = false;
        showToast(error.message);
      } finally {
        repairing = false;
        setRunStatus(state.run_status || "idle");
      }
    }

    elements.open.addEventListener("click", openDocument);
    elements.browse.addEventListener("click", browseDocument);
    elements.send.addEventListener("click", sendMessage);
    elements.stop.addEventListener("click", stopRun);
    elements.wordView.addEventListener("click", openWordView);
    elements.newWindow.addEventListener("click", () => window.open("/", "_blank"));
    elements.session.addEventListener("change", () => {
      if (elements.session.value && elements.session.value !== SESSION_ID) {
        window.location.assign(`/?s=${encodeURIComponent(elements.session.value)}`);
      }
    });
    elements.model.addEventListener("change", saveAgentSettings);
    elements.reasoning.addEventListener("change", saveAgentSettings);
    elements.reload.addEventListener("click", repairWatch);
    elements.preview.addEventListener("error", repairWatch);
    elements.recent.addEventListener("change", () => {
      if (elements.recent.value) elements.path.value = elements.recent.value;
    });
    elements.path.addEventListener("keydown", event => {
      if (event.key === "Enter") openDocument();
    });
    elements.input.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });

    let dragging = false;
    elements.splitter.addEventListener("pointerdown", event => {
      dragging = true;
      elements.splitter.classList.add("dragging");
      elements.splitter.setPointerCapture(event.pointerId);
    });
    elements.splitter.addEventListener("pointermove", event => {
      if (!dragging) return;
      const percent = Math.max(45, Math.min(82, event.clientX / window.innerWidth * 100));
      document.documentElement.style.setProperty("--left", `${percent}%`);
    });
    elements.splitter.addEventListener("pointerup", event => {
      dragging = false;
      elements.splitter.classList.remove("dragging");
      elements.splitter.releasePointerCapture(event.pointerId);
    });

    loadAgentSettings();
    api("/health").then(applySnapshot).catch(error => showToast(error.message));

    function announceClose() {
      if (closeSent) return;
      closeSent = true;
      eventSource.close();
      const url =
         `/session/close?s=${encodeURIComponent(SESSION_ID)}` +
        `&token=${encodeURIComponent(TOKEN)}` +
        `&client=${encodeURIComponent(CLIENT_ID)}`;
      navigator.sendBeacon(url, new Blob(["{}"], { type: "application/json" }));
    }
    window.addEventListener("pagehide", announceClose);
    window.addEventListener("beforeunload", announceClose);
  </script>
</body>
</html>
"""


class OgentHandler(BaseHTTPRequestHandler):
    server_version = "OgentLite"

    def log_message(self, format_string: str, *args: Any) -> None:
        return

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send_bytes(status, json_bytes(payload), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise UserFacingError("Invalid Content-Length.") from None
        if length <= 0 or length > MAX_BODY_BYTES:
            raise UserFacingError("Invalid request body size.", 413)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UserFacingError("Invalid JSON request body.") from None
        if not isinstance(value, dict):
            raise UserFacingError("Request body must be a JSON object.")
        return value

    def _authorized(self) -> bool:
        token = self.headers.get("X-Ogent-Token", "")
        return secrets.compare_digest(token, STATE.token)

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def _session_id_from_query(parsed: urllib.parse.ParseResult) -> str:
        query = urllib.parse.parse_qs(parsed.query)
        return str((query.get("s") or [""])[0]).strip()

    def _session_for_post(self) -> SessionState:
        session_id = self.headers.get("X-Ogent-Session", "").strip()
        if session_id == "new":
            return STATE.create_session()
        if not session_id:
            raise UserFacingError("Missing Ogent session.", 400)
        return STATE.get_session(session_id)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            session_id = self._session_id_from_query(parsed)
            if not session_id:
                session = STATE.create_session()
                self._send_redirect(f"/?s={session.session_id}")
                return
            try:
                session = STATE.get_session(session_id)
            except UserFacingError:
                self._send_redirect("/")
                return
            nonce = secrets.token_urlsafe(18)
            html = (
                HTML_TEMPLATE.replace("__TOKEN__", STATE.token)
                .replace("__NONCE__", nonce)
                .replace("__SESSION_ID__", session.session_id)
            )
            self._send_bytes(
                200,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                {
                    "Content-Security-Policy": (
                        "default-src 'self'; "
                        f"script-src 'nonce-{nonce}'; "
                        "style-src 'unsafe-inline'; "
                        "frame-src http://127.0.0.1:* http://localhost:*; "
                        "connect-src 'self'; img-src 'self' data:"
                    )
                },
            )
            return
        if parsed.path == "/health":
            session_id = self._session_id_from_query(parsed)
            if session_id:
                try:
                    session = STATE.get_session(session_id)
                    self._send_json(200, STATE.snapshot_for(session))
                except UserFacingError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
            else:
                self._send_json(200, STATE.global_snapshot())
            return
        if parsed.path == "/events":
            query = urllib.parse.parse_qs(parsed.query)
            token = (query.get("token") or [""])[0]
            if not secrets.compare_digest(token, STATE.token):
                self._send_json(403, {"error": "Forbidden."})
                return
            try:
                session = STATE.get_session(self._session_id_from_query(parsed))
            except UserFacingError as exc:
                self._send_json(exc.status, {"error": str(exc)})
                return
            client_id = str((query.get("client") or [""])[0]).strip()
            if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", client_id):
                self._send_json(400, {"error": "Missing or invalid browser client id."})
                return
            self._serve_events(session, client_id)
            return
        if parsed.path == "/snapshot.pdf":
            query = urllib.parse.parse_qs(parsed.query)
            token = str((query.get("token") or [""])[0])
            if not secrets.compare_digest(token, STATE.token):
                self._send_json(403, {"error": "Forbidden."})
                return
            try:
                session = STATE.get_session(self._session_id_from_query(parsed))
                with session.lock:
                    snapshot_path = session.snapshot_path
                session_root = WORK_ROOT / session.session_id
                if (
                    snapshot_path is None
                    or not snapshot_path.is_file()
                    or not path_is_within(snapshot_path, session_root)
                ):
                    raise UserFacingError("No Word view is ready for this session.", 404)
                self._send_bytes(
                    200,
                    snapshot_path.read_bytes(),
                    "application/pdf",
                    {"Content-Disposition": 'inline; filename="ogent-word-view.pdf"'},
                )
            except UserFacingError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            return
        self._send_json(404, {"error": "Not found."})

    def _serve_events(self, session: SessionState, client_id: str) -> None:
        try:
            last_id = int(self.headers.get("Last-Event-ID", "0"))
        except ValueError:
            last_id = 0
        try:
            session.connect_sse(client_id)
        except UserFacingError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            with session.lock:
                initial_sequence = session.sequence
                snapshot_data = session.public_snapshot()
            snapshot_data.update(STATE.global_snapshot())
            snapshot = {
                "seq": initial_sequence,
                "type": "snapshot",
                "time": now_iso(),
                "data": snapshot_data,
            }
            self._write_event(snapshot)
            cursor = max(last_id, initial_sequence)
            while not STATE.shutdown_requested and not session.closed:
                events = session.current_events_after(cursor)
                if not events:
                    with session.condition:
                        session.condition.wait(timeout=15)
                    events = session.current_events_after(cursor)
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    self._write_event(event)
                    cursor = event["seq"]
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            session.disconnect_sse(client_id)
            STATE.broadcast_sessions()

    def _write_event(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"id: {event['seq']}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/session/close":
            query = urllib.parse.parse_qs(parsed.query)
            token = str((query.get("token") or [""])[0])
            if not secrets.compare_digest(token, STATE.token):
                self._send_json(403, {"error": "Forbidden."})
                return
            try:
                session = STATE.get_session(self._session_id_from_query(parsed))
                client_id = str((query.get("client") or [""])[0]).strip()
                if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", client_id):
                    raise UserFacingError("Missing or invalid browser client id.", 400)
                session.mark_page_closed(client_id)
                self._send_bytes(204, b"", "text/plain; charset=utf-8")
            except UserFacingError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            return
        if not self._authorized():
            self._send_json(403, {"error": "Forbidden."})
            return
        session: SessionState | None = None
        created_for_open = False
        try:
            if parsed.path == "/shutdown":
                self._read_json()
                with STATE.registry_lock:
                    STATE.shutdown_requested = True
                self._send_json(200, {"message": "Ogent Lite is stopping."})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            session = self._session_for_post()
            created_for_open = (
                parsed.path == "/open"
                and self.headers.get("X-Ogent-Session", "").strip() == "new"
            )
            if parsed.path == "/open":
                with session.lock:
                    busy = session.run_status in ACTIVE_RUN_STATUSES
                    snapshot_busy = session.snapshot_in_progress
                if busy:
                    raise UserFacingError(
                        "Ogent is still working. Stop that run or wait for it to finish.",
                        409,
                    )
                if snapshot_busy:
                    raise UserFacingError(
                        "Word view is still being generated. Wait for it to finish.",
                        409,
                    )
                payload = self._read_json()
                result = dispatch_open_path(session, str(payload.get("path", "")))
                if created_for_open and result.get("action") == "focus_session":
                    close_session(session)
                self._send_json(200, result)
                return
            if parsed.path == "/chat":
                payload = self._read_json()
                status, result = handle_chat_message(
                    session,
                    str(payload.get("message", "")),
                    payload.get("model", DEFAULT_MODEL),
                    payload.get("reasoning", DEFAULT_REASONING),
                )
                self._send_json(status, result)
                return
            if parsed.path == "/stop":
                self._read_json()
                stopped = stop_active_run(session)
                self._send_json(200, {"stopped": stopped})
                return
            if parsed.path == "/watch/restart":
                self._read_json()
                ensure_watch(session)
                self._send_json(
                    200,
                    {
                        "watch_alive": True,
                        "watch_port": session.watch_port,
                        "watch_url": (
                            f"http://{HOST}:{session.watch_port}/"
                            if session.watch_port
                            else None
                        ),
                    },
                )
                return
            if parsed.path == "/pick":
                self._read_json()
                selected = pick_document_path()
                self._send_json(200, {"path": selected})
                return
            if parsed.path == "/snapshot":
                self._read_json()
                generate_word_snapshot(session)
                self._send_json(
                    200,
                    {
                        "url": f"/snapshot.pdf?s={session.session_id}",
                        "session_id": session.session_id,
                    },
                )
                return
            self._send_json(404, {"error": "Not found."})
        except UserFacingError as exc:
            if parsed.path == "/open" and session is not None:
                if created_for_open:
                    close_session(session)
                else:
                    with session.lock:
                        session.last_error = str(exc)
                    session.add_message("assistant", str(exc))
            self._send_json(exc.status, {"error": str(exc)})
        except Exception as exc:
            message = f"Internal error: {exc}"
            if session is not None:
                if created_for_open:
                    close_session(session)
                else:
                    with session.lock:
                        session.last_error = str(exc)
                if parsed.path == "/open" and not created_for_open:
                    session.add_message("assistant", message)
            self._send_json(500, {"error": message})


class OgentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    allow_reuse_port = False

    def handle_error(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def find_existing_server() -> tuple[int, dict[str, Any]] | None:
    for port in range(BASE_PORT, BASE_PORT + 20):
        data = http_json(f"http://{HOST}:{port}/health", timeout=0.18)
        if data and data.get("app") == APP_NAME:
            return port, data
    return None


def post_open_to_existing_server(port: int, raw_path: str) -> dict[str, Any]:
    try:
        info = json.loads(SERVER_INFO_PATH.read_text(encoding="utf-8"))
        recorded_port = int(info["port"])
        token = str(info["token"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UserFacingError(
            "Ogent is running, but its local connection record is missing or invalid.",
            500,
        ) from exc
    if recorded_port != port:
        raise UserFacingError(
            "Ogent's local connection record does not match the running server. "
            "Run `ogent stop`, then try again.",
            409,
        )

    request = urllib.request.Request(
        f"http://{HOST}:{port}/open",
        data=json_bytes({"path": raw_path}),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Ogent-Token": token,
            "X-Ogent-Session": "new",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = str(payload.get("error", "")).strip()
        except (UnicodeDecodeError, ValueError, AttributeError):
            message = ""
        raise UserFacingError(
            message or f"Ogent could not open the file (HTTP {exc.code}).",
            exc.code,
        ) from None
    except (OSError, urllib.error.URLError) as exc:
        raise UserFacingError(f"Could not contact the running Ogent server: {exc}", 503) from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise UserFacingError("The running Ogent server returned an invalid response.", 502) from exc
    if not isinstance(payload, dict):
        raise UserFacingError("The running Ogent server returned an invalid response.", 502)
    return payload


def _registry_module() -> Any:
    if os.name != "nt" or winreg is None:
        raise UserFacingError("Windows Explorer integration is available only on Windows.", 501)
    return winreg


def shell_registry_path(extension: str) -> str:
    return (
        rf"Software\Classes\SystemFileAssociations\{extension}"
        r"\shell\OgentLite"
    )


def resolve_shell_interpreter() -> tuple[Path, bool]:
    executable = Path(sys.executable).resolve()
    pythonw = executable.with_name("pythonw.exe")
    if pythonw.is_file():
        return pythonw, False
    return executable, True


def register_shell_integration() -> None:
    registry = _registry_module()
    if not ICON_PATH.is_file():
        raise UserFacingError(f"Ogent icon not found: {ICON_PATH}", 500)
    interpreter, console_fallback = resolve_shell_interpreter()
    command = f'"{interpreter}" "{Path(__file__).resolve()}" --open "%1"'
    for extension in SHELL_EXTENSIONS:
        key_path = shell_registry_path(extension)
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER,
            key_path,
            0,
            registry.KEY_WRITE,
        ) as key:
            registry.SetValueEx(key, None, 0, registry.REG_SZ, "Open in Ogent")
            registry.SetValueEx(key, "Icon", 0, registry.REG_SZ, str(ICON_PATH.resolve()))
            registry.SetValueEx(key, "Position", 0, registry.REG_SZ, "Top")
        command_path = key_path + r"\command"
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER,
            command_path,
            0,
            registry.KEY_WRITE,
        ) as command_key:
            registry.SetValueEx(command_key, None, 0, registry.REG_SZ, command)
        print(rf"Wrote HKCU\{key_path}")
        print(rf"Wrote HKCU\{command_path}")
    if console_fallback:
        print(
            "Note: pythonw.exe was not found beside the active interpreter; "
            "Explorer will use python.exe and may briefly show a console window."
        )


def unregister_shell_integration() -> None:
    registry = _registry_module()
    for extension in SHELL_EXTENSIONS:
        key_path = shell_registry_path(extension)
        removed = False
        for candidate in (key_path + r"\command", key_path):
            try:
                registry.DeleteKey(registry.HKEY_CURRENT_USER, candidate)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise UserFacingError(
                    rf"Could not remove HKCU\{candidate}: {exc}",
                    500,
                ) from exc
            print(rf"Removed HKCU\{candidate}")
            removed = True
        if not removed:
            print(rf"Already absent: HKCU\{key_path}")


def stop_existing_server() -> bool:
    try:
        info = json.loads(SERVER_INFO_PATH.read_text(encoding="utf-8"))
        port = int(info["port"])
        token = str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    body = b"{}"
    request = urllib.request.Request(
        f"http://{HOST}:{port}/shutdown",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Ogent-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def write_server_info(port: int) -> None:
    LOCAL_DATA.mkdir(parents=True, exist_ok=True)
    info = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "pid": os.getpid(),
        "port": port,
        "token": STATE.token,
        "started_at": now_iso(),
    }
    temp = SERVER_INFO_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, SERVER_INFO_PATH)


def choose_port(requested: int) -> int:
    for port in range(requested, requested + 30):
        if port_available(port):
            return port
    raise RuntimeError(f"No free localhost port found from {requested} through {requested + 29}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ogent Lite local document workspace")
    parser.add_argument("--port", type=int, default=BASE_PORT, help="Preferred localhost port")
    parser.add_argument("--no-browser", action="store_true", help="Start without opening a browser")
    parser.add_argument(
        "--idle-exit-minutes",
        type=float,
        default=DEFAULT_IDLE_EXIT_MINUTES,
        metavar="N",
        help="Exit after N minutes with no sessions (0 keeps the backend resident)",
    )
    parser.add_argument(
        "--session-grace-seconds",
        type=float,
        default=float(
            os.environ.get(
                "OGENT_SESSION_GRACE_SECONDS",
                str(DEFAULT_SESSION_GRACE_SECONDS),
            )
        ),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reaper-tick-seconds",
        type=float,
        default=float(
            os.environ.get(
                "OGENT_REAPER_TICK_SECONDS",
                str(DEFAULT_REAPER_TICK_SECONDS),
            )
        ),
        help=argparse.SUPPRESS,
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--open", dest="open_path", metavar="FILE", help="Open a file in Ogent")
    action.add_argument("--stop", action="store_true", help="Stop a running Ogent Lite server")
    action.add_argument(
        "--register-shell",
        action="store_true",
        help="Add per-user Explorer 'Open in Ogent' entries",
    )
    action.add_argument(
        "--unregister-shell",
        action="store_true",
        help="Remove per-user Explorer 'Open in Ogent' entries",
    )
    args = parser.parse_args()
    if args.idle_exit_minutes < 0:
        parser.error("--idle-exit-minutes must be 0 or greater")
    if args.session_grace_seconds < 0:
        parser.error("--session-grace-seconds must be 0 or greater")
    if args.reaper_tick_seconds <= 0:
        parser.error("--reaper-tick-seconds must be greater than 0")

    LOCAL_DATA.mkdir(parents=True, exist_ok=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    if args.register_shell:
        try:
            register_shell_integration()
        except UserFacingError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.unregister_shell:
        try:
            unregister_shell_integration()
        except UserFacingError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.stop:
        if stop_existing_server():
            print("Ogent Lite stopped.")
            return 0
        print("Ogent Lite is not running.")
        return 1

    existing = find_existing_server()
    if existing:
        port, _ = existing
        url = f"http://{HOST}:{port}/"
        if args.open_path:
            try:
                result = post_open_to_existing_server(port, args.open_path)
            except UserFacingError as exc:
                webbrowser.open(url)
                print(str(exc), file=sys.stderr)
                return 1
            session_id = str(result.get("session_id", "")).strip()
            target_url = (
                f"{url}?s={urllib.parse.quote(session_id)}"
                if session_id
                else url
            )
            webbrowser.open(target_url)
            print(f"{result.get('message', 'File sent to Ogent')} {target_url}")
            return 0
        if not args.no_browser:
            webbrowser.open(url)
        print(f"Ogent Lite is already running at {url}")
        return 0

    port = choose_port(args.port)
    STATE.server_port = port
    STATE.idle_exit_minutes = args.idle_exit_minutes
    STATE.session_grace_seconds = args.session_grace_seconds
    STATE.reaper_tick_seconds = args.reaper_tick_seconds
    server = OgentServer((HOST, port), OgentHandler)
    write_server_info(port)
    atexit.register(cleanup)
    initial_session: SessionState | None = None
    if args.open_path:
        initial_session = STATE.create_session()
        try:
            dispatch_open_path(initial_session, args.open_path)
        except UserFacingError as exc:
            with initial_session.lock:
                initial_session.last_error = str(exc)
            initial_session.add_message("assistant", str(exc))
            print(str(exc), file=sys.stderr)

    def request_shutdown(*_: Any) -> None:
        with STATE.registry_lock:
            STATE.shutdown_requested = True
        threading.Thread(target=server.shutdown, daemon=True).start()

    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGINT, request_shutdown)
        signal.signal(signal.SIGTERM, request_shutdown)

    reaper = threading.Thread(
        target=reaper_loop,
        args=(server,),
        name="ogent-session-reaper",
        daemon=True,
    )
    reaper.start()

    base_url = f"http://{HOST}:{port}/"
    url = (
        f"{base_url}?s={initial_session.session_id}"
        if initial_session
        else base_url
    )
    print(f"Ogent Lite {APP_VERSION} listening on {url}")
    print(
        f"Session previews use ports {WATCH_PORT_FIRST} through {WATCH_PORT_LAST}."
    )
    print("Press Ctrl+C to stop.")
    if args.open_path or not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

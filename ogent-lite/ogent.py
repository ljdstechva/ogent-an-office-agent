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
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - Ogent is a Windows app.
    winreg = None  # type: ignore[assignment]


APP_NAME = "Ogent Lite"
APP_VERSION = "0.4.0"
HOST = "127.0.0.1"
BASE_PORT = 8765
WATCH_PORT = 26315
SUPPORTED_OFFICE = {".docx", ".xlsx", ".pptx"}
SHELL_EXTENSIONS = (".docx", ".xlsx", ".pptx")
ACTIVE_RUN_STATUSES = {"starting", "working", "stopping"}
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


def http_json(url: str, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def watch_http_alive() -> bool:
    try:
        request = urllib.request.Request(f"http://{HOST}:{WATCH_PORT}/", method="GET")
        with urllib.request.urlopen(request, timeout=1.25) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            return False


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


class OgentState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.watch_lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.events: collections.deque[dict[str, Any]] = collections.deque(maxlen=2000)
        self.sequence = 0
        self.transcript: list[dict[str, Any]] = []
        self.recent = load_recent()
        self.active_source: Path | None = None
        self.active_doc: Path | None = None
        self.watch_process: subprocess.Popen[str] | None = None
        self.watch_tail: collections.deque[str] = collections.deque(maxlen=40)
        self.run_process: subprocess.Popen[str] | None = None
        self.run_thread: threading.Thread | None = None
        self.run_status = "idle"
        self.run_id: str | None = None
        self.stop_requested = False
        self.codex_thread_id: str | None = None
        self.pending_pdf = False
        self.server_port = BASE_PORT
        self.token = secrets.token_urlsafe(32)
        self.shutdown_requested = False
        self.last_error: str | None = None

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
            self.transcript.append(message)
            self.transcript = self.transcript[-100:]
        self.emit("message", message)

    def add_activity(self, stream: str, text: str) -> None:
        if text:
            self.emit("activity", {"stream": stream, "text": text[-4000:]})

    def set_run_status(self, status: str, **extra: Any) -> None:
        with self.lock:
            self.run_status = status
        self.emit("run", {"status": status, **extra})

    def public_snapshot(self, include_watch_probe: bool = True) -> dict[str, Any]:
        with self.lock:
            active_doc = str(self.active_doc) if self.active_doc else None
            active_source = str(self.active_source) if self.active_source else None
            snapshot = {
                "app": APP_NAME,
                "version": APP_VERSION,
                "server_port": self.server_port,
                "watch_port": WATCH_PORT,
                "active_document": active_doc,
                "source_document": active_source,
                "run_status": self.run_status,
                "run_id": self.run_id,
                "recent": list(self.recent),
                "transcript": list(self.transcript),
                "last_error": self.last_error,
                "codex_context": bool(self.codex_thread_id),
                "sequence": self.sequence,
            }
        snapshot["watch_alive"] = bool(active_doc) and watch_http_alive() if include_watch_probe else False
        return snapshot

    def reset_conversation(
        self,
        source: Path,
        working: Path,
        *,
        preserve_transcript: bool = False,
    ) -> None:
        with self.lock:
            self.active_source = source
            self.active_doc = working
            self.codex_thread_id = None
            self.pending_pdf = False
            self.run_status = "idle"
            self.run_id = None
            self.stop_requested = False
            if not preserve_transcript:
                self.transcript = []
            self.last_error = None
        self.emit("snapshot", self.public_snapshot(include_watch_probe=False))

    def remember(self, source: Path) -> None:
        value = str(source)
        with self.lock:
            self.recent = [item for item in self.recent if item.casefold() != value.casefold()]
            self.recent.insert(0, value)
            self.recent = self.recent[:12]
            recent = list(self.recent)
        save_recent(recent)
        self.emit("recent", {"items": recent})

    def current_events_after(self, sequence: int) -> list[dict[str, Any]]:
        with self.lock:
            return [event for event in self.events if event["seq"] > sequence]


STATE = OgentState()


def stop_watch(*, clear_document: bool = False) -> None:
    with STATE.watch_lock:
        with STATE.lock:
            document = STATE.active_doc
            process = STATE.watch_process
            STATE.watch_process = None
            if clear_document:
                STATE.active_doc = None
                STATE.active_source = None
                STATE.codex_thread_id = None

        if process and process.poll() is None:
            # The watch is an owned process tree; stopping it directly releases
            # the port faster than launching a second OfficeCLI command.
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
        elif document:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                run_quiet(["officecli", "unwatch", str(document)], cwd=document.parent, timeout=12)
        if process:
            STATE.emit("watch", {"status": "stopped", "port": WATCH_PORT})
        if clear_document:
            STATE.emit("snapshot", STATE.public_snapshot(include_watch_probe=False))


def _watch_output_reader(
    process: subprocess.Popen[str],
    ready_queue: queue.Queue[tuple[str, str]],
) -> None:
    assert process.stdout is not None
    for raw in iter(process.stdout.readline, ""):
        line = raw.rstrip()
        if not line:
            continue
        with STATE.lock:
            STATE.watch_tail.append(line)
        STATE.add_activity("watch", line)
        ready_queue.put(("line", line))
        if "http://" in line or "https://" in line or "watching" in line.casefold():
            ready_queue.put(("ready", line))
    code = process.wait()
    ready_queue.put(("exit", str(code)))
    with STATE.lock:
        is_current = STATE.watch_process is process
        if is_current:
            STATE.watch_process = None
    if is_current and not STATE.shutdown_requested:
        STATE.emit("watch", {"status": "dead", "exit_code": code, "port": WATCH_PORT})


def start_watch(document: Path) -> None:
    with STATE.watch_lock:
        stop_watch(clear_document=False)
        if not document.exists():
            raise UserFacingError(f"The working document no longer exists: {document}", 404)
        if not port_available(WATCH_PORT):
            raise UserFacingError(
                f"Port {WATCH_PORT} is already in use. Stop the stale OfficeCLI watch and try again.",
                409,
            )

        ready_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        process = subprocess.Popen(
            ["officecli", "watch", str(document), "--port", str(WATCH_PORT)],
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
        with STATE.lock:
            STATE.watch_process = process
            STATE.watch_tail.clear()
        reader = threading.Thread(
            target=_watch_output_reader,
            args=(process, ready_queue),
            name="ogent-watch-output",
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
                raise UserFacingError(
                    f"OfficeCLI watch exited before it became ready (exit {value}). {last_line}",
                    500,
                )
            if kind == "ready":
                # OfficeCLI emits its URL only after the preview listener has
                # been created; avoid a redundant HTTP round trip here.
                STATE.emit(
                    "watch",
                    {"status": "ready", "port": WATCH_PORT, "document": str(document)},
                )
                return

        if watch_http_alive():
            STATE.emit("watch", {"status": "ready", "port": WATCH_PORT, "document": str(document)})
            return
        terminate_process_tree(process)
        with STATE.lock:
            if STATE.watch_process is process:
                STATE.watch_process = None
        raise UserFacingError(f"OfficeCLI watch did not become ready. {last_line}", 504)


def ensure_watch() -> None:
    with STATE.watch_lock:
        with STATE.lock:
            document = STATE.active_doc
        if not document:
            raise UserFacingError("Open an Office document first.", 409)
        if not document.exists():
            stop_watch(clear_document=True)
            raise UserFacingError(
                "The active working file was moved or deleted. Paste its new path and open it again.",
                404,
            )
        if watch_http_alive():
            return
        STATE.emit("watch", {"status": "restarting", "port": WATCH_PORT})
        start_watch(document)


def make_working_copy(source: Path) -> Path:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(str(source).casefold().encode("utf-8")).hexdigest()[:8]
    filename = f"{safe_name(source.stem)}-ogent-{stamp}-{digest}{source.suffix.lower()}"
    destination = WORK_ROOT / filename
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


def open_document(
    raw_path: str,
    *,
    make_copy: bool = True,
    state_source: Path | None = None,
    preserve_transcript: bool = False,
    remember_source: bool = True,
    announce: bool = True,
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

    if make_copy and not path_is_within(source, WORK_ROOT):
        working = make_working_copy(source)
    else:
        working = source

    try:
        start_watch(working)
    except Exception:
        if working != source:
            with contextlib.suppress(OSError):
                working.unlink()
        raise

    protected_source = state_source.resolve() if state_source else source
    STATE.reset_conversation(
        protected_source,
        working,
        preserve_transcript=preserve_transcript,
    )
    if remember_source:
        STATE.remember(source)
    if announce:
        STATE.add_message(
            "assistant",
            f"Opened a protected working copy: {working.name}. The source file remains untouched.",
        )
    STATE.emit(
        "document",
        {
            "source": str(protected_source),
            "working": str(working),
            "watch_url": f"http://{HOST}:{WATCH_PORT}/",
        },
    )
    return {
        "message": "Working copy opened.",
        "source": str(protected_source),
        "active_document": str(working),
        "watch_url": f"http://{HOST}:{WATCH_PORT}/",
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
        STATE.add_activity(stream_name, line)
    return process.wait(), lines


def _pdf_import_worker(source: Path, request_text: str) -> None:
    work_dir = WORK_ROOT / f"pdf-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    copied_pdf = work_dir / f"{safe_name(source.stem)}-source-copy.pdf"
    working_docx = work_dir / f"{safe_name(source.stem)}-working.docx"
    shutil.copy2(source, copied_pdf)
    process: subprocess.Popen[str] | None = None
    try:
        with STATE.lock:
            if STATE.stop_requested:
                STATE.add_message("assistant", "PDF conversion stopped.")
                STATE.set_run_status("stopped", kind="pdf")
                return
        STATE.set_run_status("working", kind="pdf", label="Converting a protected PDF copy")
        with STATE.lock:
            if STATE.stop_requested:
                STATE.add_message("assistant", "PDF conversion stopped.")
                STATE.set_run_status("stopped", kind="pdf")
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
            STATE.run_process = process
        code, lines = _stream_plain_process(process, stream_name="pdf")
        with STATE.lock:
            stopped = STATE.stop_requested
        if stopped:
            STATE.add_message("assistant", "PDF conversion stopped.")
            STATE.set_run_status("stopped", kind="pdf")
            return
        if code != 0 or not working_docx.exists():
            tail = "\n".join(lines[-8:])
            if "SCANNED_PDF" in tail:
                message = "This PDF is image-only and needs OCR before it can be edited."
            else:
                message = f"PDF conversion failed with exit code {code}. {tail}".strip()
            STATE.last_error = message
            STATE.add_message("assistant", message)
            STATE.set_run_status("error", kind="pdf", exit_code=code)
            return
        open_document(
            str(working_docx),
            make_copy=False,
            state_source=source,
            preserve_transcript=True,
            remember_source=False,
            announce=False,
        )
        STATE.add_message(
            "assistant",
            "The source PDF was preserved, its working DOCX is open on the left, and it is ready for your edit request.",
        )
        STATE.set_run_status("idle", kind="pdf", exit_code=0)
    except Exception as exc:
        STATE.last_error = str(exc)
        STATE.add_message("assistant", f"PDF preparation failed: {exc}")
        STATE.set_run_status("error", kind="pdf")
    finally:
        with STATE.lock:
            if STATE.run_process is process:
                STATE.run_process = None
            STATE.stop_requested = False


def start_pdf_import(source: Path, request_text: str) -> None:
    with STATE.lock:
        if STATE.run_status in ACTIVE_RUN_STATUSES:
            raise UserFacingError("Ogent is still working. Stop that run or wait for it to finish.", 409)
        STATE.run_status = "starting"
        STATE.run_id = uuid.uuid4().hex
        STATE.stop_requested = False
        STATE.pending_pdf = False
    STATE.emit("run", {"status": "starting", "kind": "pdf", "run_id": STATE.run_id})
    thread = threading.Thread(
        target=_pdf_import_worker,
        args=(source, request_text),
        name="ogent-pdf-import",
        daemon=True,
    )
    with STATE.lock:
        STATE.run_thread = thread
    thread.start()


def dispatch_open_path(raw_path: str) -> dict[str, Any]:
    source = normalize_existing_path(raw_path)
    if source.suffix.lower() == ".pdf":
        start_pdf_import(source, f"Open this PDF in Ogent: {source}")
        message = (
            "Preparing a protected PDF working copy. The original PDF will remain untouched."
        )
        STATE.add_message("assistant", message)
        with STATE.lock:
            run_id = STATE.run_id
        return {
            "action": "pdf_import",
            "message": message,
            "source": str(source),
            "run_id": run_id,
        }
    result = open_document(str(source))
    result["action"] = "document_opened"
    return result


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
    prompt: str,
    document: Path,
    session_id: str | None,
    model: str,
    reasoning: str,
) -> tuple[int, str | None, str | None, list[str]]:
    args = build_codex_command(prompt, session_id, model, reasoning)

    with STATE.lock:
        if STATE.stop_requested:
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
        STATE.run_process = process
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
            STATE.add_activity("stderr", line)
            continue
        try:
            event = json.loads(line)
        except ValueError:
            STATE.add_activity("codex", line)
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
            STATE.add_activity("codex", activity)

    code = process.wait()
    return code, thread_id, final_text, list(stderr_tail)


def _agent_worker(
    message: str,
    document: Path,
    source: Path | None,
    model: str,
    reasoning: str,
) -> None:
    started = time.perf_counter()
    try:
        with STATE.lock:
            if STATE.stop_requested:
                STATE.add_message("assistant", "Stopped. No further agent work is running.")
                STATE.set_run_status("stopped", kind="codex")
                return
        ensure_watch()
        with STATE.lock:
            session_id = STATE.codex_thread_id
        STATE.set_run_status("working", kind="codex", run_id=STATE.run_id)
        STATE.add_activity("codex", f"Using {model} with {reasoning} reasoning.")
        code, new_thread_id, final_text, stderr_tail = _run_codex_once(
            agent_prompt(message, document, source),
            document,
            session_id,
            model,
            reasoning,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        with STATE.lock:
            stopped = STATE.stop_requested
            if new_thread_id:
                STATE.codex_thread_id = new_thread_id
        if stopped:
            STATE.add_message("assistant", "Stopped. No further agent work is running.")
            STATE.set_run_status("stopped", kind="codex", elapsed_ms=elapsed_ms)
            return
        if code != 0:
            detail = "\n".join(stderr_tail[-6:]).strip()
            message_text = f"Codex exited with code {code}."
            if detail:
                message_text += f" {detail}"
            STATE.last_error = message_text
            STATE.add_message("assistant", message_text)
            STATE.set_run_status("error", kind="codex", exit_code=code, elapsed_ms=elapsed_ms)
            return
        if not final_text:
            final_text = "The document task completed. Review the live document on the left."
        STATE.add_message("assistant", final_text)
        STATE.set_run_status("idle", kind="codex", exit_code=0, elapsed_ms=elapsed_ms)
        ensure_watch()
        STATE.emit(
            "document",
            {
                "source": str(source) if source else None,
                "working": str(document),
                "watch_url": f"http://{HOST}:{WATCH_PORT}/?refresh={time.time_ns()}",
            },
        )
    except Exception as exc:
        with STATE.lock:
            stopped = STATE.stop_requested
        if stopped:
            STATE.add_message("assistant", "Stopped. No further agent work is running.")
            STATE.set_run_status("stopped", kind="codex")
        else:
            STATE.last_error = str(exc)
            STATE.add_message("assistant", f"The document run failed: {exc}")
            STATE.set_run_status("error", kind="codex")
    finally:
        with STATE.lock:
            STATE.run_process = None
            STATE.stop_requested = False


def start_agent_run(message: str, model: str, reasoning: str) -> str:
    selected_model, selected_reasoning = validate_agent_settings(model, reasoning)
    with STATE.lock:
        if STATE.run_status in {"starting", "working", "stopping"}:
            raise UserFacingError("Ogent is still working. Stop that run or wait for it to finish.", 409)
        document = STATE.active_doc
        source = STATE.active_source
        if not document:
            raise UserFacingError("Open an Office document first.", 409)
        STATE.run_status = "starting"
        STATE.run_id = uuid.uuid4().hex
        STATE.stop_requested = False
        run_id = STATE.run_id
    STATE.add_message("user", message)
    STATE.emit(
        "run",
        {
            "status": "starting",
            "kind": "codex",
            "run_id": run_id,
            "model": selected_model,
            "reasoning": selected_reasoning,
        },
    )
    thread = threading.Thread(
        target=_agent_worker,
        args=(message, document, source, selected_model, selected_reasoning),
        name=f"ogent-codex-{run_id[:8]}",
        daemon=True,
    )
    with STATE.lock:
        STATE.run_thread = thread
    thread.start()
    return run_id


def handle_chat_message(
    message: str,
    model: Any = DEFAULT_MODEL,
    reasoning: Any = DEFAULT_REASONING,
) -> tuple[int, dict[str, Any]]:
    text = message.strip()
    if not text:
        raise UserFacingError("Type a request first.")
    selected_model, selected_reasoning = validate_agent_settings(model, reasoning)
    with STATE.lock:
        has_document = STATE.active_doc is not None
    if has_document:
        run_id = start_agent_run(text, selected_model, selected_reasoning)
        return 202, {
            "message": "Run started.",
            "run_id": run_id,
            "model": selected_model,
            "reasoning": selected_reasoning,
        }

    STATE.add_message("user", text)
    pdf_path = extract_pdf_path(text)
    if pdf_path:
        start_pdf_import(pdf_path, text)
        return 202, {"message": "Preparing a protected PDF working copy.", "run_id": STATE.run_id}
    if "pdf" in text.casefold():
        with STATE.lock:
            STATE.pending_pdf = True
        response = (
            "Paste the absolute PDF path here. I will copy it, convert the copy through the "
            "Word-first PDF pipeline, and open the working DOCX on the left. The original will remain untouched."
        )
    else:
        response = (
            "Open a .docx, .xlsx, or .pptx using the path field above. "
            "For a PDF, ask me to edit it and then paste its absolute path."
        )
    STATE.add_message("assistant", response)
    return 200, {"message": response}


def stop_active_run() -> bool:
    with STATE.lock:
        process = STATE.run_process
        active = STATE.run_status in {"starting", "working", "stopping"}
        if not active:
            return False
        STATE.stop_requested = True
        STATE.run_status = "stopping"
    STATE.emit("run", {"status": "stopping", "run_id": STATE.run_id})
    terminate_process_tree(process)
    return True


def cleanup() -> None:
    with STATE.lock:
        if STATE.shutdown_requested:
            already_requested = True
        else:
            STATE.shutdown_requested = True
            already_requested = False
        process = STATE.run_process
    if not already_requested:
        terminate_process_tree(process)
        stop_watch(clear_document=False)
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
      height: 50px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 18px;
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
    .status-cluster { margin-left: auto; display: flex; align-items: center; gap: 8px; }
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
        </div>
        <div class="status-cluster">
          <span class="status-dot" id="statusDot"></span>
          <span class="status-text" id="statusText">Ready to open a document</span>
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
    const WATCH_URL = "http://127.0.0.1:__WATCH_PORT__/";
    const AGENT_SETTINGS_KEY = "ogent-agent-settings-v1";
    const elements = {
      path: document.getElementById("pathInput"),
      open: document.getElementById("openButton"),
      recent: document.getElementById("recentSelect"),
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
      reload: document.getElementById("reloadPreview"),
      activity: document.getElementById("activity"),
      activitySummary: document.getElementById("activitySummary"),
      activityLog: document.getElementById("activityLog"),
      toast: document.getElementById("toast"),
      splitter: document.getElementById("splitter")
    };
    let state = { active_document: null, run_status: "idle", recent: [], transcript: [] };
    let repairing = false;
    let toastTimer = null;

    async function api(path, options = {}) {
      const headers = Object.assign({}, options.headers || {});
      if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
      if ((options.method || "GET") !== "GET") headers["X-Ogent-Token"] = TOKEN;
      const response = await fetch(path, Object.assign({}, options, { headers }));
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
        return;
      }
      elements.empty.style.display = "none";
      elements.preview.style.display = "block";
      elements.documentName.textContent = path.split(/[\\/]/).pop() || path;
      const target = url || `${WATCH_URL}?v=${Date.now()}`;
      if (elements.preview.src !== target) elements.preview.src = target;
    }

    function setRunStatus(status) {
      state.run_status = status;
      const busy = ["starting", "working", "stopping"].includes(status);
      elements.stop.disabled = !busy;
      elements.send.disabled = busy;
      elements.open.disabled = busy;
      elements.model.disabled = busy;
      elements.reasoning.disabled = busy;
      elements.statusDot.className = `status-dot ${busy ? "busy" : status === "error" ? "error" : state.watch_alive ? "ready" : ""}`;
      elements.statusText.textContent =
        status === "working" ? "Codex is editing…" :
        status === "starting" ? "Starting Codex…" :
        status === "stopping" ? "Stopping…" :
        status === "error" ? "Action needed" :
        state.active_document ? (state.watch_alive ? "Live preview connected" : "Preview reconnecting") :
        "Ready to open a document";
      elements.activitySummary.textContent = busy ? "Agent activity · working…" : "Agent activity";
    }

    function applySnapshot(snapshot) {
      state = Object.assign(state, snapshot);
      renderTranscript(state.transcript || []);
      renderRecent(state.recent || []);
      setPreview(state.active_document, state.active_document ? `${WATCH_URL}?v=${Date.now()}` : null);
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
      else if (type === "run") setRunStatus(data.status);
      else if (type === "watch") {
        state.watch_alive = data.status === "ready";
        setRunStatus(state.run_status || "idle");
      } else if (type === "document") {
        state.active_document = data.working;
        state.watch_alive = true;
        setPreview(data.working, data.watch_url);
        setRunStatus(state.run_status || "idle");
      }
    }

    const eventSource = new EventSource(`/events?token=${encodeURIComponent(TOKEN)}`);
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
        if (result.action === "pdf_import") {
          showToast(result.message || "Preparing a protected PDF working copy.");
          return;
        }
        state.active_document = result.active_document;
        state.watch_alive = true;
        setPreview(result.active_document, `${result.watch_url}?v=${Date.now()}`);
        showToast("Working copy opened. The source remains untouched.");
      } catch (error) {
        showToast(error.message);
      } finally {
        setRunStatus(state.run_status || "idle");
      }
    }

    async function sendMessage() {
      const message = elements.input.value.trim();
      if (!message) return;
      try {
        elements.input.value = "";
        await api("/chat", {
          method: "POST",
          body: JSON.stringify({
            message,
            model: elements.model.value,
            reasoning: elements.reasoning.value
          })
        });
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
        await api("/watch/restart", { method: "POST", body: "{}" });
        state.watch_alive = true;
        elements.preview.src = `${WATCH_URL}?v=${Date.now()}`;
      } catch (error) {
        state.watch_alive = false;
        showToast(error.message);
      } finally {
        repairing = false;
        setRunStatus(state.run_status || "idle");
      }
    }

    async function heartbeat() {
      try {
        const health = await api("/health");
        state = Object.assign(state, health);
        setRunStatus(state.run_status || "idle");
        if (state.active_document && !health.watch_alive) await repairWatch();
      } catch (_) {
        state.watch_alive = false;
        setRunStatus("error");
      }
    }

    elements.open.addEventListener("click", openDocument);
    elements.send.addEventListener("click", sendMessage);
    elements.stop.addEventListener("click", stopRun);
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
    setInterval(heartbeat, 30000);
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

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            nonce = secrets.token_urlsafe(18)
            html = (
                HTML_TEMPLATE.replace("__TOKEN__", STATE.token)
                .replace("__NONCE__", nonce)
                .replace("__WATCH_PORT__", str(WATCH_PORT))
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
                        f"frame-src http://{HOST}:{WATCH_PORT} http://localhost:{WATCH_PORT}; "
                        "connect-src 'self'; img-src 'self' data:"
                    )
                },
            )
            return
        if parsed.path == "/health":
            self._send_json(200, STATE.public_snapshot())
            return
        if parsed.path == "/events":
            query = urllib.parse.parse_qs(parsed.query)
            token = (query.get("token") or [""])[0]
            if not secrets.compare_digest(token, STATE.token):
                self._send_json(403, {"error": "Forbidden."})
                return
            self._serve_events()
            return
        self._send_json(404, {"error": "Not found."})

    def _serve_events(self) -> None:
        try:
            last_id = int(self.headers.get("Last-Event-ID", "0"))
        except ValueError:
            last_id = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            snapshot = {
                "seq": STATE.sequence,
                "type": "snapshot",
                "time": now_iso(),
                "data": STATE.public_snapshot(),
            }
            self._write_event(snapshot)
            cursor = max(last_id, STATE.sequence)
            while not STATE.shutdown_requested:
                events = STATE.current_events_after(cursor)
                if not events:
                    with STATE.condition:
                        STATE.condition.wait(timeout=15)
                    events = STATE.current_events_after(cursor)
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    self._write_event(event)
                    cursor = event["seq"]
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _write_event(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"id: {event['seq']}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorized():
            self._send_json(403, {"error": "Forbidden."})
            return
        try:
            if parsed.path == "/open":
                with STATE.lock:
                    busy = STATE.run_status in ACTIVE_RUN_STATUSES
                if busy:
                    raise UserFacingError(
                        "Ogent is still working. Stop that run or wait for it to finish.",
                        409,
                    )
                payload = self._read_json()
                result = dispatch_open_path(str(payload.get("path", "")))
                self._send_json(200, result)
                return
            if parsed.path == "/chat":
                payload = self._read_json()
                status, result = handle_chat_message(
                    str(payload.get("message", "")),
                    payload.get("model", DEFAULT_MODEL),
                    payload.get("reasoning", DEFAULT_REASONING),
                )
                self._send_json(status, result)
                return
            if parsed.path == "/stop":
                self._read_json()
                stopped = stop_active_run()
                self._send_json(200, {"stopped": stopped})
                return
            if parsed.path == "/watch/restart":
                self._read_json()
                ensure_watch()
                self._send_json(200, {"watch_alive": True, "watch_port": WATCH_PORT})
                return
            if parsed.path == "/shutdown":
                self._read_json()
                self._send_json(200, {"message": "Ogent Lite is stopping."})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._send_json(404, {"error": "Not found."})
        except UserFacingError as exc:
            if parsed.path == "/open":
                STATE.last_error = str(exc)
                STATE.add_message("assistant", str(exc))
            self._send_json(exc.status, {"error": str(exc)})
        except Exception as exc:
            STATE.last_error = str(exc)
            message = f"Internal error: {exc}"
            if parsed.path == "/open":
                STATE.add_message("assistant", message)
            self._send_json(500, {"error": message})


class OgentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


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
        headers={"Content-Type": "application/json", "X-Ogent-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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
            webbrowser.open(url)
            print(f"{result.get('message', 'File sent to Ogent')} {url}")
            return 0
        if not args.no_browser:
            webbrowser.open(url)
        print(f"Ogent Lite is already running at {url}")
        return 0

    port = choose_port(args.port)
    STATE.server_port = port
    server = OgentServer((HOST, port), OgentHandler)
    write_server_info(port)
    atexit.register(cleanup)
    if args.open_path:
        try:
            dispatch_open_path(args.open_path)
        except UserFacingError as exc:
            STATE.last_error = str(exc)
            STATE.add_message("assistant", str(exc))
            print(str(exc), file=sys.stderr)

    def request_shutdown(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGINT, request_shutdown)
        signal.signal(signal.SIGTERM, request_shutdown)

    url = f"http://{HOST}:{port}/"
    print(f"Ogent Lite {APP_VERSION} listening on {url}")
    print(f"Live document preview uses http://{HOST}:{WATCH_PORT}/")
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

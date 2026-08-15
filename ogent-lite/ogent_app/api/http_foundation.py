"""Extracted localhost HTTP adapter for Ogent."""

# ruff: noqa: F821

from __future__ import annotations

from http.server import BaseHTTPRequestHandler


def bind_runtime(values: dict[str, object]) -> None:
    for name, value in values.items():
        if not name.startswith("__"):
            globals()[name] = value


class OgentHandlerFoundation(BaseHTTPRequestHandler):
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
        if length <= 0 or length > MAX_JSON_BODY_BYTES:
            raise UserFacingError(
                "Invalid request body size. Ogent accepts JSON requests up to 1 MB.",
                413,
            )
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UserFacingError("Invalid JSON request body.") from None
        if not isinstance(value, dict):
            raise UserFacingError("Request body must be a JSON object.")
        return value

    def _read_upload(self, session: SessionState) -> tuple[Path, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise UserFacingError("Invalid upload size.") from None
        if length < 0:
            raise UserFacingError("Invalid upload size.")
        if length > MAX_UPLOAD_BYTES:
            raise UserFacingError(
                f"The dropped file exceeds Ogent's {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
                413,
            )
        if FEATURE_FLAGS.strict_disk_forecast:
            try:
                STORAGE_RESOURCES.ensure_capacity(
                    length,
                    purpose="document import",
                )
            except OSError as exc:
                raise UserFacingError(str(exc), 507) from exc

        encoded_name = self.headers.get("X-Ogent-Filename", "").strip()
        if not encoded_name or len(encoded_name) > 2048:
            raise UserFacingError("The dropped file has no valid filename.")
        try:
            original_name = urllib.parse.unquote(
                encoded_name,
                encoding="utf-8",
                errors="strict",
            )
        except UnicodeError:
            raise UserFacingError("The dropped filename is not valid UTF-8.") from None
        filename = safe_upload_filename(original_name)
        if length == 0 and Path(filename).suffix.casefold() == ".pdf":
            raise UserFacingError(
                "An empty PDF has no pages to open. Choose a PDF with content.",
                400,
            )

        import_dir = IMPORT_ROOT / session.session_id / uuid.uuid4().hex
        import_dir.mkdir(parents=True, exist_ok=False)
        target = import_dir / filename
        temporary = import_dir / f".{filename}.uploading"
        remaining = length
        try:
            with temporary.open("xb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise UserFacingError(
                            "The file upload ended unexpectedly.", 400
                        )
                    output.write(chunk)
                    remaining -= len(chunk)
            os.replace(temporary, target)
        except Exception:
            with contextlib.suppress(OSError):
                temporary.unlink()
            with contextlib.suppress(OSError):
                import_dir.rmdir()
            raise
        return target, Path(original_name.replace("\\", "/")).name

    def _discard_request_body(self, length: int) -> None:
        """Drain a bounded rejected upload so Windows can return JSON, not a reset."""
        if length <= 0 or length > MAX_REFERENCE_BYTES:
            self.close_connection = True
            return
        original_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(5)
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            self.close_connection = True
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(original_timeout)

    def _read_reference_upload(  # noqa: C901
        self,
        session: SessionState,
    ) -> ReferenceAttachment:
        """Own the compatibility upload transaction; see ARCHITECTURE.md."""
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type.casefold() != "application/octet-stream":
            raise UserFacingError(
                "Reference uploads require Content-Type: application/octet-stream.",
                415,
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise UserFacingError("Invalid reference upload size.") from None
        if length <= 0:
            raise UserFacingError("The reference file is empty.")
        if length > MAX_REFERENCE_BYTES:
            raise UserFacingError(
                f"The reference exceeds the "
                f"{MAX_REFERENCE_BYTES // (1024 * 1024)} MB per-file limit.",
                413,
            )
        if FEATURE_FLAGS.strict_disk_forecast:
            try:
                STORAGE_RESOURCES.ensure_capacity(
                    length,
                    purpose="reference upload",
                )
            except OSError as exc:
                self._discard_request_body(length)
                raise UserFacingError(str(exc), 507) from exc
        encoded_name = self.headers.get("X-Ogent-Filename", "").strip()
        if not encoded_name or len(encoded_name) > 2048:
            raise UserFacingError("The reference upload has no valid filename.")
        try:
            original_name = urllib.parse.unquote(
                encoded_name,
                encoding="utf-8",
                errors="strict",
            )
            filename = sanitize_reference_filename(original_name)
        except UnicodeError:
            raise UserFacingError(
                "The reference filename is not valid UTF-8."
            ) from None
        except ReferenceError as exc:
            raise _reference_user_error(exc) from exc

        store = session.attachment_store
        if store is None:
            raise UserFacingError("Retained attachment storage is unavailable.", 500)
        reservation_id = uuid.uuid4().hex
        rejection: UserFacingError | None = None
        with session.reference_lock:
            with session.lock:
                if session.closed:
                    rejection = UserFacingError(
                        "This Ogent session has closed.",
                        410,
                    )
            reserved_count = len(session.reference_reservations)
            reserved_bytes = sum(session.reference_reservations.values())
            pending_count = len(session.pending_references)
            pending_bytes = sum(item.byte_size for item in session.pending_references)
            retained_count = len(session.retained_references)
            retained_bytes = sum(
                item.byte_size for item in session.retained_references.values()
            )
            if (
                rejection is None
                and session.reference_operations >= MAX_CONCURRENT_REFERENCE_UPLOADS
            ):
                rejection = UserFacingError(
                    "Ogent is already processing three attachments. "
                    "Wait for one upload to finish and try again.",
                    429,
                )
            elif (
                rejection is None
                and pending_count + reserved_count >= MAX_REFERENCES_PER_SEND
            ):
                rejection = UserFacingError(
                    f"The next message already has {MAX_REFERENCES_PER_SEND} "
                    "attachments or uploads. Remove one before attaching another.",
                    413,
                )
            elif (
                rejection is None
                and pending_bytes + reserved_bytes + length
                > MAX_COMBINED_BYTES_PER_SEND
            ):
                rejection = UserFacingError(
                    f"The next message would exceed the "
                    f"{MAX_COMBINED_BYTES_PER_SEND // (1024 * 1024)} MB combined "
                    "attachment limit. Remove an attachment or choose a smaller file.",
                    413,
                )
            elif (
                rejection is None
                and retained_count + reserved_count >= MAX_SESSION_REFERENCE_COUNT
            ):
                rejection = UserFacingError(
                    f"This workspace already has "
                    f"{MAX_SESSION_REFERENCE_COUNT} retained attachments or "
                    "uploads. Forget one before attaching another.",
                    413,
                )
            elif (
                rejection is None
                and retained_bytes + reserved_bytes + length
                > MAX_SESSION_REFERENCE_BYTES
            ):
                rejection = UserFacingError(
                    f"This workspace would exceed the "
                    f"{MAX_SESSION_REFERENCE_BYTES // (1024 * 1024)} MB retained "
                    "attachment limit. Forget an attachment or choose a smaller file.",
                    413,
                )
            if rejection is None:
                session.reference_reservations[reservation_id] = length
                session.reference_connections[reservation_id] = self.connection
                session.reference_operations += 1
                session.reference_idle.clear()
        if rejection is not None:
            self._discard_request_body(length)
            raise rejection

        try:
            attachment_dir = store.begin_upload(reservation_id)
        except (RetainedAttachmentError, OSError) as exc:
            with session.reference_lock:
                session.reference_connections.pop(reservation_id, None)
                session.reference_reservations.pop(reservation_id, None)
                session.reference_operations = max(
                    0,
                    session.reference_operations - 1,
                )
                if session.reference_operations == 0:
                    session.reference_idle.set()
            raise UserFacingError(str(exc), 500) from exc
        target = attachment_dir / f"source{Path(filename).suffix.casefold()}"
        temporary = attachment_dir / ".uploading"
        cleanup_needed = True
        original_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(30)
            remaining = length
            with temporary.open("xb") as output:
                while remaining:
                    try:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                    except TimeoutError:
                        raise UserFacingError(
                            "The reference upload timed out. Attach the file again.",
                            408,
                        ) from None
                    if not chunk:
                        raise UserFacingError(
                            "The reference upload ended unexpectedly. "
                            "Attach the file again.",
                            400,
                        )
                    output.write(chunk)
                    remaining -= len(chunk)
            os.replace(temporary, target)
            inspection = inspect_reference_upload(
                session,
                reservation_id,
                target,
                filename,
            )
            with session.reference_lock:
                if (
                    session.closed
                    or reservation_id not in session.reference_reservations
                ):
                    raise UserFacingError("This Ogent session has closed.", 410)
                # Consume this upload's reservation before committing it. Keeping
                # both the reservation and the attachment visible, even briefly,
                # double-counts a successful concurrent upload.
                session.reference_reservations.pop(reservation_id)
                attachment = register_reference_upload(
                    session,
                    target,
                    filename,
                    inspection,
                )
            cleanup_needed = False
            return attachment
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(original_timeout)
            cleanup_error: Exception | None = None
            if cleanup_needed:
                try:
                    store.reject_upload(attachment_dir)
                except (RetainedAttachmentError, OSError) as exc:
                    cleanup_error = exc
            with session.reference_lock:
                process = session.reference_processes.pop(
                    reservation_id,
                    None,
                )
                session.reference_connections.pop(reservation_id, None)
                session.reference_reservations.pop(reservation_id, None)
                session.reference_operations = max(
                    0,
                    session.reference_operations - 1,
                )
                if session.reference_operations == 0:
                    session.reference_idle.set()
            if process is not None and process.poll() is None:
                terminate_process_tree(process)
            if cleanup_error is not None and sys.exc_info()[0] is None:
                raise UserFacingError(
                    "Temporary cleanup failed after the rejected upload.",
                    500,
                ) from cleanup_error

    def _authorized(self) -> bool:
        token = self.headers.get("X-Ogent-Token", "")
        return secrets.compare_digest(token, STATE.token)

    def _reject_unauthorized_post(self) -> None:
        """Drain bounded JSON bodies so Windows can deliver the 403 cleanly."""

        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if 0 < length <= MAX_JSON_BODY_BYTES:
            self._discard_request_body(length)
        elif length != 0:
            # Do not consume an attacker-controlled large or malformed body.
            self.close_connection = True
        self._send_json(403, {"error": "Forbidden."})

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

    def _preview_context(
        self,
        parsed: urllib.parse.ParseResult,
        *,
        register: bool = False,
    ) -> tuple[SessionState, str, str, Path, int, str, str]:
        query = urllib.parse.parse_qs(parsed.query)
        session = STATE.get_session(self._session_id_from_query(parsed))
        client_id = str((query.get("client") or [""])[0]).strip()
        document_id = str((query.get("document") or [""])[0]).strip()
        generation = str((query.get("generation") or [""])[0]).strip()
        channel = str((query.get("channel") or [""])[0]).strip()
        try:
            with session.lock:
                if (
                    session.active_doc is None
                    or session.document_id != document_id
                    or session.watch_generation != generation
                    or session.watch_port is None
                ):
                    backend_log(
                        "preview_proxy_rejected",
                        session_id=session.session_id,
                        client_id=client_id,
                        requested_document_id=document_id,
                        active_document_id=session.document_id,
                        requested_generation=generation,
                        active_generation=session.watch_generation,
                        reason="stale_preview_identity",
                    )
                    raise UserFacingError("That preview link is stale.", 409)
                if register and not channel:
                    if session.sse_client_refs.get(client_id, 0) <= 0:
                        backend_log(
                            "preview_proxy_rejected",
                            session_id=session.session_id,
                            client_id=client_id,
                            document_id=document_id,
                            watch_generation=generation,
                            reason="event_stream_not_registered",
                        )
                        raise PreviewSyncError(
                            "The preview client is not attached to this "
                            "document workspace."
                        )
                    channel = session.preview_sync.register_client(
                        client_id=client_id,
                        document_id=document_id,
                        watch_generation=generation,
                    )
                else:
                    session.preview_sync.authorize(
                        client_id=client_id,
                        document_id=document_id,
                        watch_generation=generation,
                        channel=channel,
                    )
                document = session.active_doc
                port = session.watch_port
        except PreviewSyncError as exc:
            raise UserFacingError(str(exc), 403) from exc
        if document is None or port is None:
            raise UserFacingError("That preview link is stale.", 409)
        return (
            session,
            client_id,
            channel,
            document,
            port,
            document_id,
            generation,
        )

    def _serve_preview_root(
        self,
        parsed: urllib.parse.ParseResult,
    ) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        comparison_only = str((query.get("canonical") or [""])[0]).strip() == "1"
        (
            session,
            client_id,
            channel,
            document,
            port,
            document_id,
            generation,
        ) = self._preview_context(
            parsed,
            register=True,
        )
        try:
            request = urllib.request.Request(
                f"http://{HOST}:{port}/",
                method="GET",
                headers={"Accept": "text/html"},
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                if response.status != 200:
                    raise UserFacingError(
                        "The OfficeCLI preview did not return a document.",
                        502,
                    )
                payload = response.read(32 * 1024 * 1024 + 1)
            if len(payload) > 32 * 1024 * 1024:
                raise UserFacingError(
                    "The OfficeCLI preview exceeded the safe relay limit.",
                    413,
                )
            fingerprint = package_sha256(document)
            html = rewrite_preview_html(
                payload.decode("utf-8", errors="replace"),
                session,
                client_id,
                channel,
                fingerprint,
                document_id=document_id,
                watch_generation=generation,
                comparison_only=comparison_only,
            )
        except UserFacingError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise UserFacingError(
                "The OfficeCLI preview could not be relayed.",
                502,
            ) from exc
        self._send_bytes(
            200,
            html.encode("utf-8"),
            "text/html; charset=utf-8",
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'self' data: blob: https:; "
                    "script-src 'self' 'unsafe-inline' https:; "
                    "style-src 'self' 'unsafe-inline' https:; "
                    "img-src 'self' data: blob: https:; "
                    "font-src 'self' data: https:; "
                    f"connect-src 'self' http://{HOST}:{STATE.server_port}; "
                    "object-src 'none'; "
                    f"frame-ancestors http://{HOST}:{STATE.server_port} "
                    f"http://localhost:{STATE.server_port}"
                ),
            },
        )

    def _serve_preview_events(
        self,
        parsed: urllib.parse.ParseResult,
    ) -> None:
        (
            session,
            _,
            _,
            document,
            port,
            document_id,
            generation,
        ) = self._preview_context(parsed)
        request = urllib.request.Request(
            f"http://{HOST}:{port}/events",
            method="GET",
            headers={"Accept": "text/event-stream"},
        )
        try:
            upstream = urllib.request.urlopen(request)
        except (OSError, urllib.error.URLError) as exc:
            raise UserFacingError(
                "The OfficeCLI preview stream is unavailable.",
                502,
            ) from exc
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            data_lines: list[str] = []
            while not STATE.shutdown_requested and not session.closed:
                raw = upstream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if line:
                    self.wfile.write(f"{line}\n".encode("utf-8"))
                    self.wfile.flush()
                    continue
                if not data_lines:
                    self.wfile.write(b"\n")
                    self.wfile.flush()
                    continue
                data = "\n".join(data_lines)
                data_lines.clear()
                try:
                    event = json.loads(data)
                except ValueError:
                    event = None
                if (
                    isinstance(event, dict)
                    and event.get("action") in PREVIEW_MUTATION_ACTIONS
                ):
                    with session.lock:
                        current = (
                            session.active_doc is not None
                            and session.active_doc.resolve() == document.resolve()
                            and session.document_id == document_id
                            and session.watch_generation == generation
                        )
                    if not current:
                        break
                    try:
                        event["_ogent"] = {
                            "event_fingerprint": event_fingerprint(event),
                            "package_sha256": package_sha256(document),
                        }
                    except OSError:
                        event["_ogent"] = {
                            "event_fingerprint": event_fingerprint(event),
                            "package_sha256": "",
                        }
                    data = json.dumps(
                        event,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Intentional transport boundary: the preview client disconnected.
            return
        finally:
            with contextlib.suppress(OSError):
                upstream.close()

    def _serve_preview_controls(
        self,
        parsed: urllib.parse.ParseResult,
    ) -> None:
        (
            session,
            client_id,
            channel,
            _,
            _,
            document_id,
            generation,
        ) = self._preview_context(parsed)
        try:
            cursor = int(self.headers.get("Last-Event-ID", "0"))
        except ValueError:
            cursor = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while not STATE.shutdown_requested and not session.closed:
                controls = session.preview_sync.controls_after(
                    client_id=client_id,
                    document_id=document_id,
                    watch_generation=generation,
                    channel=channel,
                    sequence=cursor,
                    timeout=15,
                )
                if not controls:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for control in controls:
                    cursor = int(control["seq"])
                    data = json.dumps(
                        {key: value for key, value in control.items() if key != "seq"},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    self.wfile.write(f"id: {cursor}\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
        except (
            BrokenPipeError,
            ConnectionResetError,
            OSError,
            PreviewSyncError,
        ):
            return

    def _proxy_preview_api(
        self,
        parsed: urllib.parse.ParseResult,
    ) -> None:
        _, _, _, _, port, _, _ = self._preview_context(parsed)
        length_value = self.headers.get("Content-Length", "0")
        try:
            length = int(length_value)
        except ValueError:
            raise UserFacingError("Invalid preview request length.", 400) from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise UserFacingError("The preview request is too large.", 413)
        body = self.rfile.read(length)
        upstream_path = (
            "/api/selection" if parsed.path.endswith("/selection") else "/api/send"
        )
        request = urllib.request.Request(
            f"http://{HOST}:{port}{upstream_path}",
            data=body,
            method="POST",
            headers={
                "Content-Type": self.headers.get(
                    "Content-Type",
                    "application/json",
                )
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = response.read(MAX_BODY_BYTES + 1)
                status = response.status
                content_type = response.headers.get(
                    "Content-Type",
                    "application/json",
                )
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_BODY_BYTES + 1)
            status = exc.code
            content_type = exc.headers.get(
                "Content-Type",
                "application/json",
            )
        except (OSError, urllib.error.URLError) as exc:
            raise UserFacingError(
                "The OfficeCLI preview request failed.",
                502,
            ) from exc
        if len(payload) > MAX_BODY_BYTES:
            raise UserFacingError(
                "The OfficeCLI preview response is too large.",
                502,
            )
        self._send_bytes(
            status,
            payload,
            content_type,
            {"Cache-Control": "no-store"},
        )

    def _accept_preview_ack(
        self,
        parsed: urllib.parse.ParseResult,
    ) -> None:
        (
            session,
            client_id,
            channel,
            _,
            _,
            document_id,
            generation,
        ) = self._preview_context(parsed)
        length_value = self.headers.get("Content-Length", "0")
        try:
            length = int(length_value)
        except ValueError:
            raise UserFacingError("Invalid preview acknowledgment.", 400) from None
        if length <= 0 or length > 32 * 1024:
            raise UserFacingError("Invalid preview acknowledgment.", 400)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UserFacingError("Invalid preview acknowledgment.", 400) from None
        if not isinstance(payload, dict):
            raise UserFacingError("Invalid preview acknowledgment.", 400)
        try:
            session.preview_sync.acknowledge(
                client_id=client_id,
                document_id=document_id,
                watch_generation=generation,
                channel=channel,
                kind=str(payload.get("kind") or ""),
                version=payload.get("version"),
                package_sha256=str(payload.get("package_sha256") or ""),
                event_fingerprint_value=(
                    str(payload["event_fingerprint"])
                    if payload.get("event_fingerprint") is not None
                    else None
                ),
                dom_fingerprint=(
                    str(payload["dom_fingerprint"])
                    if payload.get("dom_fingerprint") is not None
                    else None
                ),
                canonical_dom_fingerprint=(
                    str(payload["canonical_dom_fingerprint"])
                    if payload.get("canonical_dom_fingerprint") is not None
                    else None
                ),
                control_id=(
                    str(payload["control_id"])
                    if payload.get("control_id") is not None
                    else None
                ),
                viewport_path=(
                    str(payload["viewport_path"])
                    if payload.get("viewport_path") is not None
                    else None
                ),
            )
        except PreviewSyncError as exc:
            raise UserFacingError(str(exc), 400) from exc
        self._send_bytes(
            204,
            b"",
            "text/plain; charset=utf-8",
            {
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )

    def _session_for_post(self) -> tuple[SessionState, bool]:
        session_id = self.headers.get("X-Ogent-Session", "").strip()
        if session_id == "new":
            return STATE.create_session(), True
        if session_id == "shell":
            return STATE.select_shell_session()
        if not session_id:
            raise UserFacingError("Missing Ogent session.", 400)
        return STATE.get_session(session_id), False

    def _require_connected_session_client(
        self,
        session: SessionState,
    ) -> str:
        client_id = self.headers.get("X-Ogent-Client", "").strip()
        if not PREVIEW_CLIENT_ID_PATTERN.fullmatch(client_id):
            raise UserFacingError(
                "A connected browser client is required for this action.",
                403,
            )
        with session.lock:
            connected = session.sse_client_refs.get(client_id, 0) > 0
        if not connected:
            raise UserFacingError(
                "That browser client is not attached to this document workspace.",
                403,
            )
        return client_id

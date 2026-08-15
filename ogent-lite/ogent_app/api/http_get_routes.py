"""Extracted localhost HTTP adapter for Ogent."""

# ruff: noqa: F821

from __future__ import annotations

from ogent_app.application.checkpoint_store import list_checkpoints


def bind_runtime(values: dict[str, object]) -> None:
    for name, value in values.items():
        if not name.startswith("__"):
            globals()[name] = value


class OgentGetRoutesMixin:
    def do_GET(self) -> None:  # noqa: C901
        """Dispatch legacy-compatible GET routes; see ARCHITECTURE.md."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {
            "/preview",
            "/preview/events",
            "/preview/control",
        }:
            try:
                if parsed.path == "/preview":
                    self._serve_preview_root(parsed)
                elif parsed.path == "/preview/events":
                    self._serve_preview_events(parsed)
                else:
                    self._serve_preview_controls(parsed)
            except UserFacingError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            return
        transcript_match = re.fullmatch(
            r"/api/workspaces/([0-9a-f]{8,32})/turns",
            parsed.path,
        )
        if transcript_match:
            if not self._authorized():
                self._send_json(403, {"error": "Forbidden."})
                return
            workspace_id = transcript_match.group(1)
            requested_session = self._session_id_from_query(parsed)
            if requested_session != workspace_id:
                self._send_json(403, {"error": "Forbidden."})
                return
            try:
                session = STATE.get_session(workspace_id)
                if session.durable_conversation is None:
                    raise UserFacingError(
                        "Paged transcript storage is unavailable.",
                        503,
                    )
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int((query.get("limit") or ["50"])[0])
                    after = int((query.get("after") or ["0"])[0])
                except ValueError:
                    raise UserFacingError(
                        "Transcript cursors and limits must be integers."
                    ) from None
                direction = str((query.get("direction") or ["tail"])[0]).casefold()
                if direction == "tail":
                    page = session.durable_conversation.tail_public(limit=limit)
                elif direction == "after":
                    page = session.durable_conversation.page_public(
                        after_sequence=after,
                        limit=limit,
                    )
                else:
                    raise UserFacingError("Transcript direction must be tail or after.")
                self._send_json(
                    200,
                    {
                        **page,
                        "workspace_id": workspace_id,
                        "conversation_generation": (session.conversation_generation),
                    },
                )
            except UserFacingError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            return
        workspace_review_match = re.fullmatch(
            r"/api/workspaces/([0-9a-f]{8,32})/"
            r"(run-coverage|change-review)",
            parsed.path,
        )
        if workspace_review_match:
            self._serve_workspace_review(
                parsed,
                workspace_review_match.group(1),
                workspace_review_match.group(2),
            )
            return
        document_intelligence_match = re.fullmatch(
            r"/api/workspaces/([0-9a-f]{8,32})/"
            r"(document-index|document-nodes|document-search)",
            parsed.path,
        )
        if document_intelligence_match:
            self._serve_document_intelligence(
                parsed,
                document_intelligence_match.group(1),
                document_intelligence_match.group(2),
            )
            return
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
                .replace("__VERSION__", APP_VERSION)
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
        if parsed.path == "/checkpoints":
            try:
                session = STATE.get_session(self._session_id_from_query(parsed))
            except UserFacingError as exc:
                self._send_json(exc.status, {"error": str(exc)})
                return
            with session.lock:
                document = session.active_doc
            if document is None:
                self._send_json(200, {"checkpoints": []})
                return
            try:
                listing = list_checkpoints(document)
            except OSError as exc:
                self._send_json(
                    500,
                    {"error": f"Checkpoints could not be listed: {exc}"},
                )
                return
            self._send_json(200, {"checkpoints": listing})
            return
        if parsed.path in {
            "/agent-capabilities",
            "/api/agent-capabilities",
        }:
            self._send_json(200, AGENT_CATALOG.snapshot())
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
                    snapshot_document_id = session.snapshot_document_id
                    snapshot_document_revision = session.snapshot_document_revision
                    snapshot_package_sha256 = session.snapshot_package_sha256
                    active_document = session.active_doc
                    document_id = session.document_id
                    document_revision = session.document_revision
                snapshot_root = WORK_ROOT / session.session_id / "word-view"
                if (
                    snapshot_path is None
                    or not valid_pdf_file(snapshot_path)
                    or not path_is_within(snapshot_path, snapshot_root)
                    or active_document is None
                    or snapshot_document_id != document_id
                    or snapshot_document_revision != document_revision
                    or not snapshot_package_sha256
                ):
                    raise UserFacingError(
                        "No Word view is ready for this session.", 404
                    )
                if package_sha256(active_document) != snapshot_package_sha256:
                    with session.lock:
                        invalidate_word_snapshot_locked(session)
                    raise UserFacingError(
                        "The document changed after Word View was generated. Retry.",
                        409,
                    )
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

    def _serve_document_intelligence(
        self,
        parsed: Any,
        workspace_id: str,
        resource: str,
    ) -> None:
        if not self._authorized():
            self._send_json(403, {"error": "Forbidden."})
            return
        if self._session_id_from_query(parsed) != workspace_id:
            self._send_json(403, {"error": "Forbidden."})
            return
        if DOCUMENT_REPOSITORY is None:
            self._send_json(
                503,
                {"error": "Document intelligence is unavailable."},
            )
            return
        current = DOCUMENT_REPOSITORY.current_state_for_workspace(workspace_id)
        if current is None:
            self._send_json(404, {"error": "No document index exists."})
            return
        revision, job = current
        progress = (
            DOCUMENT_INTELLIGENCE.progress_payload(revision, job)
            if DOCUMENT_INTELLIGENCE is not None
            else {
                "document_id": revision.document_id,
                "revision_id": revision.revision_id,
                "revision_number": revision.revision_number,
                "status": job.status.value,
                "progress": round(job.progress, 4),
                "indexed_nodes": job.indexed_nodes,
                "total_estimate": job.total_estimate,
                "quick_manifest": revision.quick_manifest,
                "error_code": job.error_code,
            }
        )
        if resource == "document-index":
            payload = dict(progress)
            payload["manifest"] = revision.manifest or None
            if job.status.terminal:
                delta = DOCUMENT_REPOSITORY.delta(revision.revision_id)
                payload["delta"] = {
                    "added_paths": list(delta.added_paths),
                    "changed_paths": list(delta.changed_paths),
                    "reused_paths": list(delta.reused_paths),
                    "removed_paths": list(delta.removed_paths),
                }
            else:
                payload["delta"] = None
            self._send_json(200, payload)
            return
        if job.status.value not in {"complete", "partial"}:
            self._send_json(
                409,
                {
                    "error": "The current document index is not ready.",
                    "document_index": progress,
                },
            )
            return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            limit = int((query.get("limit") or ["100"])[0])
            offset = int((query.get("offset") or ["0"])[0])
        except ValueError:
            self._send_json(
                400,
                {"error": "Document cursors and limits must be integers."},
            )
            return
        if resource == "document-nodes":
            raw_kinds = str((query.get("kinds") or [""])[0])
            try:
                kinds = tuple(
                    NodeKind(value.strip())
                    for value in raw_kinds.split(",")
                    if value.strip()
                )
            except ValueError:
                self._send_json(
                    400,
                    {"error": "The document node kind is invalid."},
                )
                return
            include_text = str(
                (query.get("include_text") or ["false"])[0]
            ).casefold() in {"1", "true", "yes"}
            stored = DOCUMENT_REPOSITORY.nodes(
                revision.revision_id,
                kinds=kinds,
                limit=limit,
                offset=offset,
                include_text=include_text,
            )
            self._send_json(
                200,
                {
                    "revision_id": revision.revision_id,
                    "status": job.status.value,
                    "offset": max(0, offset),
                    "nodes": [
                        self._document_node_public(
                            item,
                            include_text=include_text,
                        )
                        for item in stored
                    ],
                },
            )
            return
        search_query = str((query.get("q") or [""])[0]).strip()
        if not search_query:
            self._send_json(400, {"error": "Search text is required."})
            return
        hits = DOCUMENT_REPOSITORY.search(
            revision.revision_id,
            search_query,
            limit=limit,
            require_complete=job.status.value == "complete",
        )
        self._send_json(
            200,
            {
                "revision_id": revision.revision_id,
                "status": job.status.value,
                "coverage_complete": job.status.value == "complete",
                "hits": [
                    {
                        "node_id": hit.node_id,
                        "stable_path": hit.stable_path,
                        "kind": hit.kind.value,
                        "title": hit.title,
                        "text": hit.text,
                        "rank": hit.rank,
                        "sheet_name": hit.sheet_name,
                        "slide_number": hit.slide_number,
                        "metadata": hit.metadata,
                    }
                    for hit in hits
                ],
            },
        )

    def _serve_workspace_review(
        self,
        parsed: Any,
        workspace_id: str,
        resource: str,
    ) -> None:
        if not self._authorized():
            self._send_json(403, {"error": "Forbidden."})
            return
        if self._session_id_from_query(parsed) != workspace_id:
            self._send_json(403, {"error": "Forbidden."})
            return
        try:
            session = STATE.get_session(workspace_id)
        except UserFacingError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        if resource == "change-review":
            if CHANGE_REVIEW_SERVICE is None:
                self._send_json(
                    503,
                    {"error": "Verified change review is unavailable."},
                )
                return
            with session.lock:
                document = session.active_doc
                document_id = session.document_id or None
            self._send_json(
                200,
                CHANGE_REVIEW_SERVICE.review(
                    workspace_id,
                    document_id=document_id,
                    document=document,
                ),
            )
            return
        if RUN_COVERAGE_REPOSITORY is None or RUN_REPOSITORY is None:
            self._send_json(
                503,
                {"error": "Run coverage is unavailable."},
            )
            return
        query = urllib.parse.parse_qs(parsed.query)
        requested_run_id = str((query.get("run_id") or [""])[0]).strip()
        if requested_run_id and not re.fullmatch(
            r"[0-9a-f]{32}",
            requested_run_id,
        ):
            self._send_json(400, {"error": "Invalid run identifier."})
            return
        if requested_run_id:
            run = RUN_REPOSITORY.get(requested_run_id)
            if run is None or run.workspace_id != workspace_id:
                self._send_json(404, {"error": "Run coverage was not found."})
                return
        else:
            run = RUN_REPOSITORY.latest_for_workspace(workspace_id)
        ledger = RUN_COVERAGE_REPOSITORY.get(run.run_id) if run is not None else None
        if ledger is None:
            self._send_json(
                200,
                {
                    "run_id": run.run_id if run is not None else None,
                    "required": False,
                    "complete": None,
                    "categories": [],
                    "unsupported": [],
                    "visual_interpretation_used": [],
                    "disclosure": (
                        "This run did not require whole-document coverage."
                        if run is not None
                        else "No run has recorded a coverage ledger yet."
                    ),
                },
            )
            return
        public = ledger.public()
        categories = [
            {
                "category": category,
                "required": int(counts.get("total", 0)),
                "reviewed": int(counts.get("reviewed", 0)),
                "complete": (
                    int(counts.get("reviewed", 0)) == int(counts.get("total", 0))
                ),
            }
            for category, counts in public["categories"].items()
        ]
        reviewed_total = sum(int(item["reviewed"]) for item in categories)
        required_total = sum(int(item["required"]) for item in categories)
        self._send_json(
            200,
            {
                "run_id": run.run_id if run is not None else None,
                "revision_id": ledger.revision_id,
                "required": True,
                "complete": ledger.complete,
                "categories": categories,
                "unsupported": list(ledger.unreadable_or_unsupported),
                "visual_interpretation_used": list(ledger.visual_interpretation_used),
                "disclosure": (
                    "Every required structural and visual item was reviewed."
                    if ledger.complete
                    else (
                        f"Reviewed {reviewed_total} of {required_total} "
                        "required structural items. Unreviewed or unsupported "
                        "items remain disclosed."
                    )
                ),
            },
        )

    @staticmethod
    def _document_node_public(
        stored: Any,
        *,
        include_text: bool,
    ) -> dict[str, Any]:
        node = stored.node
        payload = {
            "node_id": stored.node_id,
            "stable_path": node.stable_path,
            "parent_path": node.parent_path,
            "kind": node.kind.value,
            "title": node.title,
            "metadata": node.metadata,
            "sheet_name": node.sheet_name,
            "slide_number": node.slide_number,
            "page_number": node.page_number,
            "ordinal": node.ordinal,
            "content_sha256": node.content_sha256,
            "locator": {
                "native_key": node.native_key,
                "stability": node.locator_stability.value,
                "lineage_key": node.lineage_key,
                "source_paths": list(node.locator.source_paths),
                "namespace": node.locator.namespace.value,
                "resolvable": node.locator.resolvable,
            },
        }
        if include_text:
            payload["text"] = node.text
        return payload

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
            initial_sequence, snapshot_data = capture_sse_snapshot(session)
            snapshot_data.update(STATE.global_snapshot())
            replayed: list[dict[str, Any]] = []
            if last_id > 0 and last_id < initial_sequence:
                replayed = session.current_events_after(last_id)
                for event in replayed:
                    if int(event["seq"]) > initial_sequence:
                        break
                    self._write_event(event)
            snapshot = {
                "seq": initial_sequence,
                "type": "snapshot",
                "time": now_iso(),
                "data": snapshot_data,
            }
            self._write_event(snapshot)
            cursor = initial_sequence
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
            # Intentional transport boundary: the SSE client disconnected.
            return
        finally:
            session.disconnect_sse(client_id)
            STATE.broadcast_sessions()

    def _write_event(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"id: {event['seq']}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

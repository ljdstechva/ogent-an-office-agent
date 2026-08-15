"""Extracted localhost HTTP adapter for Ogent."""

# ruff: noqa: F821

from __future__ import annotations

from ogent_app.application.checkpoint_store import (
    CheckpointError,
    create_checkpoint,
    list_checkpoints,
    restore_checkpoint,
)


def bind_runtime(values: dict[str, object]) -> None:
    for name, value in values.items():
        if not name.startswith("__"):
            globals()[name] = value


def _claim_maintenance_run(
    session,
    *,
    kind,
    verb,
    missing_document_error,
    require_document_id=False,
):
    """Atomically guard and claim the session for a server-owned run."""
    with session.lock:
        document = session.active_doc
        document_id = session.document_id
        if session.run_status in ACTIVE_RUN_STATUSES:
            raise UserFacingError(
                f"Wait for the active run to finish before {verb}.",
                409,
            )
        if session.snapshot_in_progress:
            raise UserFacingError(
                f"Wait for Exact Word View to finish before {verb}.",
                409,
            )
        if document is None or (require_document_id and not document_id):
            raise UserFacingError(missing_document_error, 409)
        run_id = secrets.token_hex(16)
        session.run_status = "working"
        session.last_run_outcome = "working"
        session.last_error = None
        session.run_id = run_id
        session.run_complete.clear()
    session.emit(
        "run",
        {
            "status": "working",
            "outcome": "working",
            "run_id": run_id,
            "kind": kind,
        },
    )
    STATE.broadcast_sessions()
    return run_id, document, document_id


def _resync_preview_after_restore(
    session,
    document,
    *,
    warning_text,
    activity_prefix,
):
    """Advance the document revision and preview after a server-side restore."""
    with session.lock:
        revision = session.document_revision
    warning = None
    try:
        fingerprint = package_sha256(document)
        revision, advanced = advance_document_revision(
            session,
            document,
            fingerprint,
        )
        session.preview_selection.advance_revision(revision)
        emit_preview_selection(session)
        ensure_watch(session)
        if advanced:
            session.emit(
                "document_revision",
                {
                    "revision": revision,
                    "preview_identity": preview_identity_public(session),
                },
            )
    except Exception as sync_error:
        warning = warning_text
        session.add_activity(
            "preview",
            (
                f"{activity_prefix} preview refresh failed "
                f"({type(sync_error).__name__})."
            ),
        )
    return revision, warning


class OgentPostRoutesMixin:
    def do_POST(self) -> None:  # noqa: C901
        """Dispatch legacy-compatible POST routes; see ARCHITECTURE.md."""
        parsed = urllib.parse.urlparse(self.path)
        workspace_action_match = re.fullmatch(
            (
                r"/api/workspaces/([0-9a-f]{8,32})/"
                r"(document-selection|undo)"
            ),
            parsed.path,
        )
        run_resume_match = re.fullmatch(
            (
                r"/api/workspaces/([0-9a-f]{8,32})/"
                r"runs/([0-9a-f]{32})/resume"
            ),
            parsed.path,
        )
        if parsed.path in {
            "/preview/ack",
            "/preview/api/selection",
            "/preview/api/send",
        }:
            try:
                if parsed.path == "/preview/ack":
                    self._accept_preview_ack(parsed)
                else:
                    self._proxy_preview_api(parsed)
            except UserFacingError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            return
        document_open_route = parsed.path in {"/open", "/upload"}
        if parsed.path == "/session/close":
            query = urllib.parse.parse_qs(parsed.query)
            token = str((query.get("token") or [""])[0])
            if not secrets.compare_digest(token, STATE.token):
                self._reject_unauthorized_post()
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
            self._reject_unauthorized_post()
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
            session, created_for_request = self._session_for_post()
            created_for_open = document_open_route and created_for_request
            if run_resume_match is not None:
                workspace_id, prior_run_id = run_resume_match.groups()
                if workspace_id != session.session_id:
                    raise UserFacingError("Forbidden.", 403)
                self._read_json()
                client_id = self.headers.get(
                    "X-Ogent-Client",
                    "",
                ).strip()
                if client_id and not PREVIEW_CLIENT_ID_PATTERN.fullmatch(client_id):
                    raise UserFacingError(
                        "Invalid browser client identity.",
                        400,
                    )
                self._send_json(
                    202,
                    resume_agent_run(
                        session,
                        prior_run_id,
                        client_id=client_id or None,
                    ),
                )
                return
            if document_open_route:
                if parsed.path == "/open":
                    payload = self._read_json()
                    result = dispatch_open_path(
                        session,
                        str(payload.get("path", "")),
                    )
                else:
                    upload_session, upload_created = STATE.allocate_document_session(
                        session
                    )
                    try:
                        uploaded_path, original_name = self._read_upload(upload_session)
                        result = dispatch_open_path(
                            upload_session,
                            str(uploaded_path),
                            origin="browser_upload",
                        )
                        if (
                            upload_session is not session
                            and result.get("action") == "document_opened"
                        ):
                            result["action"] = "focus_session"
                    except Exception:
                        if upload_created:
                            close_session(upload_session)
                        raise
                    result.update(
                        {
                            "uploaded": True,
                            "uploaded_name": original_name,
                            "import_source": str(uploaded_path),
                        }
                    )
                if created_for_open and result.get("action") == "focus_session":
                    close_session(session)
                self._send_json(200, result)
                return
            if workspace_action_match is not None:
                workspace_id, action = workspace_action_match.groups()
                if workspace_id != session.session_id:
                    raise UserFacingError("Forbidden.", 403)
                if action == "document-selection":
                    payload = self._read_json()
                    node_ids = payload.get("node_ids")
                    if (
                        not isinstance(node_ids, list)
                        or not 1 <= len(node_ids) <= 20
                        or any(
                            not isinstance(node_id, str)
                            or not re.fullmatch(r"[0-9a-f]{32}", node_id)
                            for node_id in node_ids
                        )
                    ):
                        raise UserFacingError(
                            "Select between one and twenty valid map nodes."
                        )
                    self._send_json(
                        200,
                        select_indexed_document_nodes(session, node_ids),
                    )
                    return

                payload = self._read_json()
                changeset_id = str(payload.get("changeset_id") or "").strip()
                if not re.fullmatch(r"[0-9a-f]{32}", changeset_id):
                    raise UserFacingError("Invalid verified change identifier.")
                if CHANGE_REVIEW_SERVICE is None or OFFICECLI_TYPED_GATEWAY is None:
                    raise UserFacingError(
                        "Verified change undo is unavailable.",
                        503,
                    )
                undo_run_id, document, document_id = _claim_maintenance_run(
                    session,
                    kind="undo",
                    verb="undoing",
                    missing_document_error=(
                        "Open the edited document before undoing."
                    ),
                    require_document_id=True,
                )

                try:

                    def validate_restored(
                        candidate: Path,
                    ) -> dict[str, Any]:
                        execution = OFFICECLI_TYPED_GATEWAY.validate(candidate)
                        return {
                            "accepted": execution.exit_code == 0,
                            "officecli_validate": (
                                OFFICECLI_TYPED_GATEWAY.safe_result(execution)
                            ),
                            "exit_status": execution.exit_code,
                        }

                    change_review = CHANGE_REVIEW_SERVICE.undo(
                        workspace_id,
                        changeset_id,
                        document_id=document_id,
                        document=document,
                        validate=validate_restored,
                    )
                    revision, sync_warning = _resync_preview_after_restore(
                        session,
                        document,
                        warning_text=(
                            "Undo succeeded, but Live View could not be "
                            "refreshed automatically."
                        ),
                        activity_prefix="Undo",
                    )
                    with session.lock:
                        session.last_error = None
                    _finish_session_run(
                        session,
                        undo_run_id,
                        "edit_completed",
                        kind="undo",
                        changeset_id=changeset_id,
                    )
                    self._send_json(
                        200,
                        {
                            "message": (
                                "The most recent verified document change was undone."
                            ),
                            "change_review": change_review,
                            "document_revision": revision,
                            "warning": sync_warning,
                        },
                    )
                    return
                except ChangeReviewError as exc:
                    with session.lock:
                        session.last_error = str(exc)
                    _finish_session_run(
                        session,
                        undo_run_id,
                        "error",
                        kind="undo",
                        error=str(exc),
                    )
                    raise UserFacingError(str(exc), 409) from exc
                except Exception as exc:
                    with session.lock:
                        session.last_error = "The verified change could not be undone."
                    _finish_session_run(
                        session,
                        undo_run_id,
                        "error",
                        kind="undo",
                        error=type(exc).__name__,
                    )
                    raise UserFacingError(
                        "The verified change could not be undone safely.",
                        500,
                    ) from exc
            if parsed.path == "/preview/status":
                payload = self._read_json()
                client_id = self.headers.get("X-Ogent-Client", "").strip()
                if not PREVIEW_CLIENT_ID_PATTERN.fullmatch(client_id):
                    raise UserFacingError(
                        "Invalid browser client identity.",
                        400,
                    )
                self._send_json(
                    200,
                    record_preview_client_status(
                        session,
                        client_id,
                        payload,
                    ),
                )
                return
            if parsed.path == "/session/focus":
                self._read_json()
                session.touch_browser_activity()
                self._send_bytes(204, b"", "text/plain; charset=utf-8")
                return
            if parsed.path == "/reference/upload":
                attachment = self._read_reference_upload(session)
                self._send_json(
                    201,
                    {
                        "attachment": attachment.public_metadata(),
                        "references": _public_references(session),
                        "retained": _public_retained_references(session),
                    },
                )
                return
            if parsed.path == "/reference/remove":
                payload = self._read_json()
                attachment_id = str(payload.get("attachment_id", "")).strip()
                if not re.fullmatch(r"[0-9a-f]{32}", attachment_id):
                    raise UserFacingError("Invalid reference attachment id.")
                remove_pending_reference(session, attachment_id)
                self._send_json(
                    200,
                    {
                        "message": "Reference removed.",
                        "references": _public_references(session),
                        "retained": _public_retained_references(session),
                    },
                )
                return
            if parsed.path == "/reference/clear":
                self._read_json()
                removed = clear_pending_references(session)
                self._send_json(
                    200,
                    {
                        "message": f"Removed {removed} reference(s).",
                        "references": _public_references(session),
                        "retained": _public_retained_references(session),
                    },
                )
                return
            if parsed.path == "/reference/forget":
                payload = self._read_json()
                attachment_id = str(payload.get("attachment_id", "")).strip()
                if not re.fullmatch(r"[0-9a-f]{32}", attachment_id):
                    raise UserFacingError("Invalid retained attachment id.")
                forget_retained_reference(session, attachment_id)
                self._send_json(
                    200,
                    {
                        "message": "Attachment forgotten.",
                        "references": _public_references(session),
                        "retained": _public_retained_references(session),
                    },
                )
                return
            if parsed.path == "/selection/focus":
                payload = self._read_json()
                self._send_json(
                    200,
                    focus_submitted_selection(session, payload),
                )
                return
            if parsed.path == "/selection/remove":
                payload = self._read_json()
                selection_id = str(payload.get("selection_id", "")).strip()
                if not re.fullmatch(r"[0-9a-f]{32}", selection_id):
                    raise UserFacingError("Invalid preview selection id.")
                remove_preview_selection(session, selection_id)
                self._send_json(
                    200,
                    {"preview_selection": preview_selection_public(session)},
                )
                return
            if parsed.path == "/selection/clear":
                self._read_json()
                clear_preview_selection(session)
                self._send_json(
                    200,
                    {"preview_selection": preview_selection_public(session)},
                )
                return
            if parsed.path == "/selection/multi-mode":
                payload = self._read_json()
                if not isinstance(payload.get("enabled"), bool):
                    raise UserFacingError("Invalid multi-select mode.")
                with session.lock:
                    session.selection_multi_mode = payload["enabled"]
                emit_preview_selection(session)
                self._send_json(
                    200,
                    {"preview_selection": preview_selection_public(session)},
                )
                return
            if parsed.path == "/selection/bridge":
                body = self._read_json()
                payload = body.get("payload")
                if not isinstance(payload, dict):
                    raise UserFacingError("Invalid preview selection payload.")
                accept_postmessage_selection(
                    session,
                    payload,
                    event_origin=str(body.get("event_origin") or ""),
                    source_matches=body.get("source_matches") is True,
                )
                self._send_json(
                    200,
                    {"preview_selection": preview_selection_public(session)},
                )
                return
            if parsed.path == "/settings/recovery/open-folder":
                self._read_json()
                try:
                    BACKUP_STORE.open_folder()
                except BackupError as exc:
                    raise UserFacingError(str(exc), 500) from exc
                self._send_json(200, {"message": "Recovery folder opened."})
                return
            if parsed.path == "/settings/recovery/delete-expired":
                self._read_json()
                try:
                    result = BACKUP_STORE.cleanup_expired(reason="manual")
                    summary = BACKUP_STORE.summary()
                except BackupError as exc:
                    raise UserFacingError(str(exc), 500) from exc
                with STATE.registry_lock:
                    open_sessions = list(STATE.sessions.values())
                for open_session in open_sessions:
                    open_session.emit("recovery", summary)
                self._send_json(
                    200,
                    {
                        "message": (
                            f"Expired cleanup deleted {result['deleted']} backup(s)."
                        ),
                        "cleanup": result,
                        "recovery": summary,
                    },
                )
                return
            if parsed.path == "/settings/memory/clear":
                payload = self._read_json()
                if payload.get("confirm") is not True:
                    raise UserFacingError(
                        "Confirm session-memory clearing before continuing."
                    )
                self._require_connected_session_client(session)
                with session.lock:
                    if session.active_doc is None:
                        raise UserFacingError(
                            "Open a document before starting a new chat.",
                            409,
                        )
                result = reset_document_conversation(
                    session,
                    reason="settings_clear",
                )
                self._send_json(
                    200,
                    {
                        "message": "A new chat was started for this document.",
                        **result,
                    },
                )
                return
            if parsed.path == "/conversation/reset":
                payload = self._read_json()
                if payload.get("confirm") is not True:
                    raise UserFacingError(
                        "Confirm starting a new chat before continuing."
                    )
                self._require_connected_session_client(session)
                with session.lock:
                    if session.active_doc is None:
                        raise UserFacingError(
                            "Open a document before starting a new chat.",
                            409,
                        )
                result = reset_document_conversation(
                    session,
                    reason="new_chat",
                )
                self._send_json(
                    200,
                    {
                        "message": "New chat started for this document.",
                        **result,
                    },
                )
                return
            if parsed.path in {
                "/agent-capabilities/refresh",
                "/api/agent-capabilities/refresh",
            }:
                payload = self._read_json()
                provider = str(payload.get("provider") or "").strip().casefold()
                model = str(payload.get("model") or "").strip()
                if model:
                    if provider != "claude":
                        raise UserFacingError(
                            "Model-specific effort refresh is available only for Claude Code."
                        )
                    try:
                        AGENT_CATALOG.ensure_model_efforts_async(provider, model)
                    except SelectionValidationError as exc:
                        raise UserFacingError(str(exc), 409) from exc
                else:
                    try:
                        started = AGENT_CATALOG.refresh_async(provider or None)
                    except SelectionValidationError as exc:
                        raise UserFacingError(str(exc)) from exc
                    if not started:
                        self._send_json(
                            202,
                            {
                                **AGENT_CATALOG.snapshot(),
                                "message": "Agent capability refresh is already running.",
                            },
                        )
                        return
                self._send_json(
                    202,
                    AGENT_CATALOG.snapshot(),
                )
                return
            if parsed.path == "/chat":
                payload = self._read_json()
                client_id = self.headers.get("X-Ogent-Client", "").strip()
                if client_id and not PREVIEW_CLIENT_ID_PATTERN.fullmatch(client_id):
                    raise UserFacingError("Invalid browser client identity.", 400)
                status, result = handle_chat_message(
                    session,
                    str(payload.get("message", "")),
                    payload.get("provider", DEFAULT_PROVIDER),
                    payload.get("model"),
                    payload.get(
                        "effort",
                        payload.get("reasoning", AUTOMATIC_EFFORT),
                    ),
                    client_id or None,
                    fast=payload.get("fast") is True,
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
                with session.lock:
                    document = session.active_doc
                if document is None:
                    raise UserFacingError("Open an Office document first.", 409)
                start_watch(session, document)
                self._send_json(
                    200,
                    {
                        "watch_alive": True,
                        "watch_port": session.watch_port,
                        "watch_generation": session.watch_generation,
                        "watch_url": stable_watch_url(
                            session.watch_port,
                            session.watch_generation,
                        ),
                        "preview_identity": preview_identity_public(session),
                    },
                )
                return
            if parsed.path == "/checkpoint/save":
                self._read_json()
                with session.lock:
                    document = session.active_doc
                    if document is None:
                        raise UserFacingError("Open an Office document first.", 409)
                    if session.run_status in ACTIVE_RUN_STATUSES:
                        raise UserFacingError(
                            "Wait for the active run to finish before "
                            "saving a checkpoint.",
                            409,
                        )
                try:
                    checkpoint = create_checkpoint(document, source="manual")
                    listing = list_checkpoints(document)
                except (CheckpointError, OSError) as exc:
                    raise UserFacingError(
                        f"The checkpoint could not be saved: {exc}", 500
                    ) from exc
                session.add_activity(
                    "checkpoint",
                    f"Saved checkpoint {checkpoint['name']}.",
                )
                self._send_json(
                    200,
                    {
                        "message": "Checkpoint saved beside the document.",
                        "checkpoint": checkpoint,
                        "checkpoints": listing,
                    },
                )
                return
            if parsed.path == "/checkpoint/restore":
                payload = self._read_json()
                if payload.get("confirm") is not True:
                    raise UserFacingError(
                        "Confirm the checkpoint restore before continuing."
                    )
                checkpoint_name = str(payload.get("name") or "").strip()
                restore_run_id, document, _ = _claim_maintenance_run(
                    session,
                    kind="checkpoint-restore",
                    verb="restoring",
                    missing_document_error="Open an Office document first.",
                )
                try:

                    def validate_checkpoint(candidate) -> dict:
                        if OFFICECLI_TYPED_GATEWAY is None:
                            return {"accepted": True, "officecli_validate": None}
                        execution = OFFICECLI_TYPED_GATEWAY.validate(candidate)
                        return {
                            "accepted": execution.exit_code == 0,
                            "officecli_validate": (
                                OFFICECLI_TYPED_GATEWAY.safe_result(execution)
                            ),
                            "exit_status": execution.exit_code,
                        }

                    result = restore_checkpoint(
                        document,
                        checkpoint_name,
                        validate=validate_checkpoint,
                    )
                    _, sync_warning = _resync_preview_after_restore(
                        session,
                        document,
                        warning_text=(
                            "Restore succeeded, but Live View could not be "
                            "refreshed automatically."
                        ),
                        activity_prefix="Checkpoint",
                    )
                    session.add_activity(
                        "checkpoint",
                        f"Restored checkpoint {checkpoint_name}.",
                    )
                    _finish_session_run(
                        session,
                        restore_run_id,
                        "edit_completed",
                        kind="checkpoint-restore",
                    )
                    self._send_json(
                        200,
                        {
                            "message": (
                                "The checkpoint was restored. The previous "
                                "contents were saved as a pre-restore checkpoint."
                            ),
                            "result": result,
                            "checkpoints": list_checkpoints(document),
                            "warning": sync_warning,
                        },
                    )
                    return
                except (CheckpointError, OSError) as exc:
                    with session.lock:
                        session.last_error = str(exc)
                    _finish_session_run(
                        session,
                        restore_run_id,
                        "error",
                        kind="checkpoint-restore",
                        error=str(exc),
                    )
                    raise UserFacingError(str(exc), 409) from exc
                except Exception as exc:
                    with session.lock:
                        session.last_error = (
                            "The checkpoint could not be restored."
                        )
                    _finish_session_run(
                        session,
                        restore_run_id,
                        "error",
                        kind="checkpoint-restore",
                        error=type(exc).__name__,
                    )
                    raise UserFacingError(
                        "The checkpoint could not be restored safely.",
                        500,
                    ) from exc
            if parsed.path == "/pick":
                self._read_json()
                selected = pick_document_path()
                self._send_json(200, {"path": selected})
                return
            if parsed.path == "/snapshot":
                self._read_json()
                generate_word_snapshot(session)
                with session.lock:
                    cache_key = session.snapshot_cache_key
                    document_id = session.snapshot_document_id
                    document_revision = session.snapshot_document_revision
                    package_fingerprint = session.snapshot_package_sha256
                self._send_json(
                    200,
                    {
                        "url": f"/snapshot.pdf?s={session.session_id}",
                        "session_id": session.session_id,
                        "cache_key": cache_key,
                        "document_id": document_id,
                        "document_revision": document_revision,
                        "package_sha256": package_fingerprint,
                    },
                )
                return
            self._send_json(404, {"error": "Not found."})
        except UserFacingError as exc:
            if document_open_route and session is not None:
                if created_for_open:
                    close_session(session)
                else:
                    with session.lock:
                        session.last_error = str(exc)
            error_payload = {"error": str(exc)}
            if document_open_route and session is not None and not created_for_open:
                error_payload["session_id"] = session.session_id
            self._send_json(exc.status, error_payload)
        except Exception as exc:
            backend_log(
                "http_post_internal_error",
                route=parsed.path[:120],
                error_type=type(exc).__name__,
            )
            message = (
                "Ogent encountered an internal error. No internal path or "
                "traceback was exposed."
            )
            if session is not None:
                if created_for_open:
                    close_session(session)
                else:
                    with session.lock:
                        session.last_error = message
            self._send_json(500, {"error": message})

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../api/client";
import { clientId, config } from "../../config";
import type { PreviewIdentity, WorkspaceSnapshot } from "../../types";
import { OgentMark } from "../icons";

type PreviewMode = "live" | "word";
type PreviewState = "empty" | "loading" | "ready" | "degraded" | "error";

interface PreviewSurfaceProps {
  workspace: WorkspaceSnapshot;
  update: (value: Partial<WorkspaceSnapshot>) => void;
  notify: (message: string) => void;
}

function liveUrl(identity: PreviewIdentity): string {
  const value = new URL("/preview", window.location.origin);
  value.searchParams.set("s", config.sessionId);
  value.searchParams.set("client", clientId);
  value.searchParams.set("document", identity.document_id);
  value.searchParams.set("generation", identity.watch_generation);
  return value.href;
}

function identityKey(
  identity: PreviewIdentity | null | undefined,
  revision: number | undefined,
): string {
  return identity
    ? [
        identity.session_id,
        identity.document_id,
        identity.watch_generation,
        revision ?? 0,
      ].join("|")
    : "";
}

export function PreviewSurface({
  workspace,
  update,
  notify,
}: PreviewSurfaceProps) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const wordIdentityRef = useRef("");
  const [frameAttempt, setFrameAttempt] = useState(0);
  const [mode, setMode] = useState<PreviewMode>("live");
  const [status, setStatus] = useState<PreviewState>(
    workspace.active_document ? "loading" : "empty",
  );
  const [message, setMessage] = useState("");
  const [frameUrl, setFrameUrl] = useState("");
  const currentIdentityKey = identityKey(
    workspace.preview_identity,
    workspace.document_revision,
  );
  const isDocx = workspace.active_document?.toLowerCase().endsWith(".docx") ?? false;

  const reportPreview = useCallback(
    async (
      reportStatus: string,
      reportMessage: string,
      metrics: Record<string, unknown> = {},
    ) => {
      const identity = workspace.preview_identity;
      if (!identity) return;
      try {
        await api("/preview/status", {
          method: "POST",
          body: JSON.stringify({
            status: reportStatus,
            document_id: identity.document_id,
            watch_generation: identity.watch_generation,
            message: reportMessage,
            metrics,
          }),
        });
      } catch (error) {
        notify(
          error instanceof Error
            ? `Preview status was not recorded: ${error.message}`
            : "Preview status was not recorded.",
        );
      }
    },
    [notify, workspace.preview_identity],
  );

  const openLive = useCallback(
    async (restart = false) => {
      if (!workspace.active_document || !workspace.preview_identity) return;
      setMode("live");
      setStatus("loading");
      setMessage(
        workspace.stream_connected
          ? "Waiting for Live View to confirm document content."
          : "Connecting the protected preview stream before opening Live View.",
      );
      try {
        let identity = workspace.preview_identity;
        if (restart) {
          const result = await api<{
            watch_url?: string;
            watch_port?: number;
            watch_generation?: string;
            preview_identity?: PreviewIdentity;
          }>("/watch/restart", { method: "POST", body: "{}" });
          identity = result.preview_identity ?? identity;
          update({
            watch_alive: true,
            watch_url: result.watch_url ?? workspace.watch_url,
            watch_port: result.watch_port ?? workspace.watch_port,
            watch_generation:
              result.watch_generation ?? workspace.watch_generation,
            preview_identity: identity,
          });
        }
        setFrameAttempt((current) => current + 1);
        wordIdentityRef.current = "";
        setFrameUrl(liveUrl(identity));
      } catch (error) {
        const detail =
          error instanceof Error ? error.message : "Live View could not restart.";
        setStatus("error");
        setMessage(detail);
        notify(detail);
      }
    },
    [
      notify,
      update,
      workspace.active_document,
      workspace.preview_identity,
      workspace.stream_connected,
      workspace.watch_generation,
      workspace.watch_port,
      workspace.watch_url,
    ],
  );

  const openWord = useCallback(async () => {
    const identity = workspace.preview_identity;
    if (!isDocx || !identity) {
      setStatus("error");
      setMessage("Exact Word View is available only for an open DOCX document.");
      return;
    }
    const requestedKey = currentIdentityKey;
    setMode("word");
    setStatus("loading");
    setMessage("Rendering an accurate, read-only PDF from Word.");
    update({ snapshot_in_progress: true });
    try {
      const result = await api<{
        url?: string;
        cache_key?: string;
        document_id?: string;
        document_revision?: number;
      }>("/snapshot", { method: "POST", body: "{}" });
      if (
        result.document_id !== identity.document_id ||
        Number(result.document_revision ?? 0) !==
          Number(workspace.document_revision ?? 0)
      ) {
        throw new Error(
          "Exact Word View finished for an older document revision. Retry.",
        );
      }
      const value = new URL(result.url ?? "/snapshot.pdf", window.location.origin);
      value.searchParams.set("s", config.sessionId);
      value.searchParams.set("token", config.token);
      if (result.cache_key) value.searchParams.set("cache", result.cache_key);
      wordIdentityRef.current = requestedKey;
      setFrameUrl(value.href);
    } catch (error) {
      const detail =
        error instanceof Error
          ? error.message
          : "Exact Word View could not be rendered.";
      setStatus("error");
      setMessage(detail);
      notify(detail);
    } finally {
      update({ snapshot_in_progress: false });
    }
  }, [
    currentIdentityKey,
    isDocx,
    notify,
    update,
    workspace.document_revision,
    workspace.preview_identity,
  ]);

  useEffect(() => {
    if (!workspace.active_document) {
      setFrameUrl("");
      setStatus("empty");
      setMode("live");
      return;
    }
    if (!workspace.preview_identity || !workspace.stream_connected) {
      setStatus("loading");
      setMessage("Connecting the protected Live View.");
      return;
    }
    if (mode === "word" && wordIdentityRef.current === currentIdentityKey) {
      return;
    }
    void openLive(false);
  }, [
    currentIdentityKey,
    mode,
    openLive,
    workspace.active_document,
    workspace.preview_identity,
    workspace.stream_connected,
  ]);

  useEffect(() => {
    if (status !== "loading") return;
    const timeout = window.setTimeout(() => {
      setStatus("error");
      setMessage(
        mode === "word"
          ? "Exact Word View did not finish loading before the safety timeout."
          : "Live View did not confirm usable content before the safety timeout.",
      );
    }, mode === "word" ? 20_000 : 12_000);
    return () => window.clearTimeout(timeout);
  }, [frameUrl, mode, status]);

  useEffect(() => {
    const receive = (event: MessageEvent) => {
      const frame = frameRef.current;
      if (!frame?.contentWindow || event.source !== frame.contentWindow || !frameUrl) {
        return;
      }
      let expectedOrigin = "";
      try {
        expectedOrigin = new URL(frameUrl).origin;
      } catch {
        return;
      }
      if (event.origin !== expectedOrigin || !event.data) return;
      const data = event.data as Record<string, unknown>;
      if (
        data.protocol === "ogent-preview-status" &&
        data.version === 1 &&
        mode === "live"
      ) {
        const identity = workspace.preview_identity;
        if (
          !identity ||
          data.document_id !== identity.document_id ||
          data.watch_generation !== identity.watch_generation
        ) {
          return;
        }
        if (data.type === "preview.ready") {
          const metrics =
            data.metrics && typeof data.metrics === "object"
              ? (data.metrics as Record<string, unknown>)
              : {};
          if (data.meaningful === true) {
            const approximate = Boolean(workspace.complex_layout);
            setStatus(approximate ? "degraded" : "ready");
            setMessage(
              approximate
                ? "Live View is approximate for this complex Word layout. Use Exact Word View for authoritative rendering."
                : "",
            );
            void reportPreview(
              "ready",
              "Live View confirmed usable content.",
              metrics,
            );
          } else {
            const detail =
              "Live View opened but did not contain meaningful document content.";
            void reportPreview("meaningless", detail, metrics);
            if (isDocx && workspace.complex_layout) {
              void openWord();
            } else {
              setStatus("error");
              setMessage(detail);
            }
          }
          return;
        }
        if (data.type === "preview.failed") {
          const detail =
            typeof data.message === "string"
              ? data.message
              : "Live View reported an initialization failure.";
          setStatus("error");
          setMessage(detail);
          void reportPreview("failed", detail);
          return;
        }
      }
      if (
        data.protocol === "officecli-preview-selection" &&
        data.version === 1 &&
        data.type === "selection.changed"
      ) {
        void api<{ preview_selection: WorkspaceSnapshot["preview_selection"] }>(
          "/selection/bridge",
          {
            method: "POST",
            body: JSON.stringify({
              event_origin: event.origin,
              source_matches: true,
              payload: data,
            }),
          },
        )
          .then((result) =>
            update({ preview_selection: result.preview_selection }),
          )
          .catch((error) =>
            notify(
              error instanceof Error
                ? error.message
                : "The preview selection could not be recorded.",
            ),
          );
      }
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [
    frameUrl,
    isDocx,
    mode,
    notify,
    openWord,
    reportPreview,
    update,
    workspace.complex_layout,
    workspace.preview_identity,
  ]);

  const label = useMemo(() => {
    if (status === "empty") return "";
    if (mode === "word") {
      return status === "loading" ? "Exact Word View · loading" : "Exact Word View";
    }
    if (status === "degraded") return "Live View · approximate";
    if (status === "error") return "Preview unavailable";
    return status === "loading" ? "Live View · loading" : "Live View";
  }, [mode, status]);

  return (
    <div className="preview-shell">
      {message && status === "degraded" ? (
        <div className="preview-banner" role="status">
          {message}
        </div>
      ) : null}
      <div className="preview-stage" data-preview-state={status}>
        {status === "empty" ? (
          <section className="empty-document">
            <OgentMark className="empty-mark" />
            <h1>Your document, live.</h1>
            <p>
              Local Office files open for direct editing only after a verified
              recovery backup. Browser uploads remain imported copies; PDFs remain
              protected conversions.
            </p>
          </section>
        ) : null}
        {status === "loading" || status === "error" ? (
          <section className="preview-state-card" role="status">
            <span className={`preview-state-indicator ${status}`} aria-hidden="true" />
            <h2>
              {status === "loading"
                ? mode === "word"
                  ? "Opening Exact Word View"
                  : "Opening Live View"
                : mode === "word"
                  ? "Exact Word View unavailable"
                  : "Live View unavailable"}
            </h2>
            <p>{message}</p>
            {status === "error" ? (
              <div className="preview-state-actions">
                <button
                  className="primary-button"
                  type="button"
                  onClick={() => void openLive(true)}
                >
                  Retry Live View
                </button>
                {isDocx ? (
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void openWord()}
                  >
                    Open Exact Word View
                  </button>
                ) : null}
              </div>
            ) : null}
          </section>
        ) : null}
        <iframe
          key={`${mode}|${currentIdentityKey}|${frameAttempt}`}
          ref={frameRef}
          className="document-preview"
          title={label || "OfficeCLI document preview"}
          src={frameUrl || undefined}
          hidden={status === "empty" || status === "error"}
          onLoad={() => {
            if (mode === "word" && frameUrl) {
              setStatus("ready");
              setMessage("");
            }
          }}
          onError={() => {
            setStatus("error");
            setMessage(
              mode === "word"
                ? "The browser could not display the generated Exact Word View."
                : "The browser could not load Live View.",
            );
          }}
        />
      </div>
      <div className="preview-view-switch" aria-label="Preview mode">
        <button
          type="button"
          aria-pressed={mode === "live"}
          onClick={() => void openLive(false)}
          disabled={!workspace.active_document}
        >
          Live View
          <small>{workspace.complex_layout ? "Approximate" : "Interactive"}</small>
        </button>
        <button
          type="button"
          aria-pressed={mode === "word"}
          onClick={() => void openWord()}
          disabled={!isDocx}
          title={
            isDocx
              ? "Render an exact, read-only PDF using Word"
              : "Exact Word View is available for DOCX files"
          }
        >
          Exact Word View
          <small>{isDocx ? "Word-rendered" : "DOCX only"}</small>
        </button>
      </div>
    </div>
  );
}

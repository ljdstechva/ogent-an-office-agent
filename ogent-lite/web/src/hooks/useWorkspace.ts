import { useCallback, useEffect, useReducer, useRef } from "react";

import { announceClose, api, eventStreamUrl } from "../api/client";
import { initialWorkspace } from "../state/initial";
import { workspaceReducer } from "../state/workspaceReducer";
import type {
  TranscriptPage,
  WorkspaceEventEnvelope,
  WorkspaceSnapshot,
} from "../types";

export interface WorkspaceController {
  state: WorkspaceSnapshot;
  refresh: () => Promise<void>;
  dispatchSnapshot: (value: Partial<WorkspaceSnapshot>) => void;
}

export function useWorkspace(
  onError: (message: string) => void,
): WorkspaceController {
  const [state, dispatch] = useReducer(workspaceReducer, initialWorkspace);
  const generationRef = useRef(state.conversation_generation);
  generationRef.current = state.conversation_generation;

  const loadTranscript = useCallback(
    async (snapshot: Partial<WorkspaceSnapshot>) => {
      if (!snapshot.transcript_paged) return;
      const expectedGeneration = Number(
        snapshot.conversation_generation ?? generationRef.current,
      );
      const url =
        snapshot.transcript_page_url ??
        `/api/workspaces/${initialWorkspace.session_id}/turns`;
      const page = await api<TranscriptPage>(
        `${url}?direction=tail&limit=100`,
      );
      if (page.conversation_generation === expectedGeneration) {
        dispatch({
          type: "transcript",
          transcript: page.items ?? [],
          total: page.total ?? 0,
        });
      }
    },
    [],
  );

  const refresh = useCallback(async () => {
    try {
      const snapshot = await api<WorkspaceSnapshot>("/health");
      dispatch({ type: "snapshot", snapshot });
      await loadTranscript(snapshot);
    } catch (error) {
      onError(error instanceof Error ? error.message : "Ogent could not refresh.");
    }
  }, [loadTranscript, onError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const source = new EventSource(eventStreamUrl());
    source.onopen = () => dispatch({ type: "stream", connected: true });
    source.onerror = () => dispatch({ type: "stream", connected: false });
    source.onmessage = (event) => {
      try {
        const envelope = JSON.parse(event.data) as WorkspaceEventEnvelope;
        dispatch({ type: "event", envelope });
        if (
          envelope.type === "snapshot" ||
          envelope.type === "turn.appended" ||
          envelope.type === "run.accepted" ||
          envelope.type === "conversation_reset"
        ) {
          const snapshot = envelope.data as Partial<WorkspaceSnapshot>;
          void loadTranscript(snapshot);
        }
      } catch {
        onError("Ogent received an invalid live update.");
      }
    };
    return () => source.close();
  }, [loadTranscript, onError]);

  useEffect(() => {
    const focus = () => {
      void api("/session/focus", { method: "POST", body: "{}" }).catch(
        () => undefined,
      );
    };
    const visibility = () => {
      if (document.visibilityState === "visible") focus();
    };
    window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.removeEventListener("focus", focus);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, []);

  useEffect(() => {
    let sent = false;
    const close = () => {
      if (sent) return;
      sent = true;
      announceClose();
    };
    window.addEventListener("pagehide", close);
    window.addEventListener("beforeunload", close);
    return () => {
      window.removeEventListener("pagehide", close);
      window.removeEventListener("beforeunload", close);
    };
  }, []);

  return {
    state,
    refresh,
    dispatchSnapshot: (value) =>
      dispatch({ type: "snapshot", snapshot: value }),
  };
}

import { useCallback, useEffect, useMemo, useState } from "react";

import { ChatPane } from "./components/chat/ChatPane";
import { DocumentPane } from "./components/document/DocumentPane";
import { Splitter } from "./components/Splitter";
import { StatusAnnouncer } from "./components/StatusAnnouncer";
import { useOgentActions } from "./hooks/useOgentActions";
import { useWorkspace } from "./hooks/useWorkspace";

export function App() {
  const [toast, setToast] = useState("");
  const notify = useCallback((message: string) => {
    setToast(message);
  }, []);
  const { state, refresh, dispatchSnapshot } = useWorkspace(notify);
  const actions = useOgentActions({
    state,
    update: dispatchSnapshot,
    notify,
  });
  const announcement = useMemo(() => {
    const runningStep = state.run_steps?.find(
      (step) => step.state === "running",
    );
    if (runningStep) return `Ogent is ${runningStep.description}`;
    if (state.run_status === "stopping") return "Stopping the active Ogent run.";
    if (state.last_run_outcome === "edit_completed") {
      return "The document edit completed and passed verification.";
    }
    if (state.last_run_outcome === "analysis_completed") {
      return "The document analysis completed.";
    }
    if (state.last_run_outcome === "error") {
      return state.last_error ?? "The Ogent run needs attention.";
    }
    return state.stream_connected
      ? "Ogent live updates connected."
      : "Ogent live updates reconnecting.";
  }, [
    state.last_error,
    state.last_run_outcome,
    state.run_status,
    state.run_steps,
    state.stream_connected,
  ]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 4_200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  return (
    <>
      <main className="workspace">
        <a className="skip-link" href="#composer">
          Skip to document request
        </a>
        <DocumentPane
          workspace={state}
          actions={actions}
          update={dispatchSnapshot}
          notify={notify}
        />
        <Splitter />
        <ChatPane
          workspace={state}
          actions={actions}
          update={dispatchSnapshot}
          notify={notify}
        />
      </main>
      <StatusAnnouncer message={announcement} />
      <div className={`toast${toast ? " show" : ""}`} role="status">
        {toast}
      </div>
      {!state.stream_connected ? (
        <button
          className="connection-banner"
          type="button"
          onClick={() => void refresh()}
        >
          Live updates are reconnecting. Click to refresh now.
        </button>
      ) : null}
    </>
  );
}

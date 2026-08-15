import { useRef, useState } from "react";

import type { AgentSelection } from "../../hooks/useAgentSelection";
import type { OgentActions } from "../../hooks/useOgentActions";
import type { WorkspaceSnapshot } from "../../types";
import { defaultInlineTurnCharacters } from "../../lib/largeTextAsset";
import { AttachmentIcon } from "../icons";
import { AgentSettings } from "./AgentSettings";
import { LargeWorkStatus } from "./LargeWorkStatus";
import { ReferenceTray } from "./ReferenceTray";
import { SelectionTray } from "./SelectionTray";

interface ComposerProps {
  workspace: WorkspaceSnapshot;
  actions: OgentActions;
  agent: AgentSelection;
  notify: (message: string) => void;
}

export function Composer({
  workspace,
  actions,
  agent,
  notify,
}: ComposerProps) {
  const [message, setMessage] = useState("");
  const [dragging, setDragging] = useState(false);
  const inlineLimit =
    workspace.quotas?.max_inline_turn_characters ??
    defaultInlineTurnCharacters;
  const referenceFileRef = useRef<HTMLInputElement>(null);
  const busy = !["idle", "error"].includes(workspace.run_status);
  const hasInput =
    Boolean(message.trim()) ||
    workspace.references.some((item) => item.status !== "Failed") ||
    workspace.preview_selection.targets.length > 0;
  const sendDisabled =
    busy ||
    actions.sending ||
    actions.uploadingReferences ||
    !agent.ready ||
    !hasInput;

  const submit = async () => {
    if (sendDisabled) return;
    const accepted = await actions.send(
      message,
      agent.providerId,
      agent.modelId,
      agent.effort,
      agent.fast,
    );
    if (accepted) setMessage("");
  };

  return (
    <section
      id="composer"
      tabIndex={-1}
      className={`composer${dragging ? " reference-drag" : ""}`}
      aria-label="Chat composer, attachments, and preview selection"
      onDragEnter={(event) => {
        if (event.dataTransfer.types.includes("Files")) {
          event.preventDefault();
          setDragging(true);
        }
      }}
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) {
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
        }
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setDragging(false);
        }
      }}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        void actions.uploadReferences([...event.dataTransfer.files]);
      }}
    >
      {dragging ? (
        <div className="reference-drop-label">Drop to attach for the next message</div>
      ) : null}
      <ReferenceTray references={workspace.references} actions={actions} />
      <SelectionTray
        selection={workspace.preview_selection}
        actions={actions}
      />
      <LargeWorkStatus
        workspace={workspace}
        retrying={actions.resuming}
        onRetry={(resumable) => {
          if (resumable && workspace.run_id) {
            void actions.resume(workspace.run_id);
            return;
          }
          const lastUser = [...workspace.transcript]
            .reverse()
            .find((item) => item.role === "user");
          if (lastUser) {
            setMessage(lastUser.text);
            notify("The failed request is ready to resend after you review it.");
          }
        }}
      />
      <AgentSettings
        selection={agent}
        capabilities={workspace.agent_capabilities}
      />
      <div className="composer-input-row">
        <button
          className="attach-button"
          type="button"
          title="Attach retained read-only references"
          aria-label="Attach files for this message"
          disabled={actions.uploadingReferences}
          onClick={() => referenceFileRef.current?.click()}
        >
          <AttachmentIcon />
        </button>
        <label>
          <span className="sr-only">Document request</span>
          <textarea
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
            placeholder="Tell Ogent what to change or ask about references…"
            aria-describedby="composer-hint"
          />
        </label>
      </div>
      <input
        ref={referenceFileRef}
        className="file-input"
        type="file"
        multiple
        aria-label="Choose reference attachments"
        accept=".docx,.xlsx,.pptx,.pdf,.txt,.md,.csv,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff"
        onChange={(event) => {
          void actions.uploadReferences([...(event.target.files ?? [])]);
          event.target.value = "";
        }}
      />
      <div className="composer-actions">
        <span id="composer-hint">
          Enter to send · Shift+Enter for a new line
          {message.length > inlineLimit * 0.8
            ? message.length > inlineLimit
              ? ` · ${message.length.toLocaleString()} characters · sends as an indexed text asset`
              : ` · ${message.length.toLocaleString()}/${inlineLimit.toLocaleString()} characters`
            : ""}
        </span>
        <button
          className="stop-button"
          type="button"
          disabled={!busy || actions.stopping}
          onClick={() => void actions.stop()}
        >
          {actions.stopping ? "Stopping…" : "Stop"}
        </button>
        <button
          className="primary-button send-button"
          type="button"
          disabled={sendDisabled}
          onClick={() => void submit()}
        >
          {actions.sending ? "Sending…" : "Send"}
        </button>
      </div>
    </section>
  );
}

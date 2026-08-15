import { useState } from "react";

import { config } from "../../config";
import { useAgentSelection } from "../../hooks/useAgentSelection";
import type { OgentActions } from "../../hooks/useOgentActions";
import type { WorkspaceSnapshot } from "../../types";
import { SettingsIcon } from "../icons";
import { ActivityPanel } from "./ActivityPanel";
import { Composer } from "./Composer";
import { NewChatDialog } from "./NewChatDialog";
import { OpenPanel } from "./OpenPanel";
import { PlanTimeline } from "./PlanTimeline";
import { SettingsDialog } from "./SettingsDialog";
import { Transcript } from "./Transcript";

interface ChatPaneProps {
  workspace: WorkspaceSnapshot;
  actions: OgentActions;
  update: (value: Partial<WorkspaceSnapshot>) => void;
  notify: (message: string) => void;
}

export function ChatPane({
  workspace,
  actions,
  update,
  notify,
}: ChatPaneProps) {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [newChatOpen, setNewChatOpen] = useState(false);
  const agent = useAgentSelection(
    workspace.agent_capabilities,
    (value) => update({ agent_capabilities: value }),
    notify,
  );

  return (
    <aside className="chat-pane" aria-label="Ogent chat">
      <header className="chat-header">
        <div>
          <h1>Ogent</h1>
          <p>Plain-language Office editing</p>
        </div>
        <button
          className="new-chat-button"
          type="button"
          disabled={!workspace.active_document}
          onClick={() => setNewChatOpen(true)}
        >
          + New chat
        </button>
        <span className="lite-badge">LITE {config.version}</span>
        <button
          className="settings-button"
          type="button"
          aria-label="Settings and recovery"
          title="Settings and recovery"
          onClick={() => setSettingsOpen(true)}
        >
          <SettingsIcon />
        </button>
      </header>
      <OpenPanel workspace={workspace} actions={actions} />
      <Transcript
        messages={workspace.transcript}
        stream={workspace.assistant_stream}
        activeDocument={workspace.active_document}
        update={update}
        notify={notify}
      />
      <PlanTimeline
        plan={workspace.run_plan}
        steps={workspace.run_steps ?? []}
      />
      <ActivityPanel
        activity={workspace.activity}
        outcome={workspace.last_run_outcome}
      />
      <Composer
        workspace={workspace}
        actions={actions}
        agent={agent}
        notify={notify}
      />
      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        workspace={workspace}
        actions={actions}
        update={update}
        notify={notify}
      />
      <NewChatDialog
        open={newChatOpen}
        onClose={() => setNewChatOpen(false)}
        actions={actions}
      />
    </aside>
  );
}

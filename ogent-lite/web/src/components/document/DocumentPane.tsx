import { useEffect, useRef, useState } from "react";

import { useDocumentIntelligence } from "../../hooks/useDocumentIntelligence";
import type { OgentActions } from "../../hooks/useOgentActions";
import type { WorkspaceSnapshot } from "../../types";
import {
  IntelligenceRail,
  type IntelligenceTab,
} from "../intelligence/IntelligenceRail";
import { CheckpointsDialog } from "./CheckpointsDialog";
import { DocumentToolbar } from "./DocumentToolbar";
import { PreviewSurface } from "./PreviewSurface";

interface DocumentPaneProps {
  workspace: WorkspaceSnapshot;
  actions: OgentActions;
  update: (value: Partial<WorkspaceSnapshot>) => void;
  notify: (message: string) => void;
}

export function DocumentPane({
  workspace,
  actions,
  update,
  notify,
}: DocumentPaneProps) {
  const [railOpen, setRailOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<IntelligenceTab>("map");
  const [checkpointsOpen, setCheckpointsOpen] = useState(false);
  const paneRef = useRef<HTMLElement>(null);
  const intelligence = useDocumentIntelligence(
    workspace.session_id,
    workspace.active_document,
    workspace.document_index,
    workspace.run_id,
    notify,
  );

  useEffect(() => {
    if (
      workspace.run_status === "idle" &&
      workspace.run_id &&
      ["edit_completed", "analysis_completed", "no_change"].includes(
        workspace.last_run_outcome,
      )
    ) {
      void intelligence.refreshReview();
    }
  }, [
    intelligence.refreshReview,
    workspace.last_run_outcome,
    workspace.run_id,
    workspace.run_status,
  ]);

  return (
    <section
      className={`document-pane${railOpen ? " rail-visible" : ""}`}
      aria-label="Live document"
      ref={paneRef}
    >
      <DocumentToolbar
        workspace={workspace}
        railOpen={railOpen}
        activeTab={activeTab}
        onTool={(tab) => {
          if (railOpen && activeTab === tab) {
            setRailOpen(false);
          } else {
            setActiveTab(tab);
            setRailOpen(true);
          }
        }}
        onReload={() => {
          const button = paneRef.current?.querySelector<HTMLButtonElement>(
            ".preview-view-switch button:first-child",
          );
          button?.click();
        }}
        onMultiSelect={() =>
          void actions.setMultiSelect(
            !Boolean(workspace.preview_selection.multi_select_mode),
          )
        }
        onCheckpoints={() => setCheckpointsOpen(true)}
      />
      <CheckpointsDialog
        open={checkpointsOpen}
        onClose={() => setCheckpointsOpen(false)}
        busy={!["idle", "error"].includes(workspace.run_status)}
        notify={notify}
      />
      <div className="document-workarea">
        <IntelligenceRail
          open={railOpen}
          tab={activeTab}
          onTab={setActiveTab}
          intelligence={intelligence}
          runSteps={workspace.run_steps ?? []}
          workspace={workspace}
        />
        <PreviewSurface
          workspace={workspace}
          update={update}
          notify={notify}
        />
      </div>
    </section>
  );
}

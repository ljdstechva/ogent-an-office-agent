import { fileName, modeLabel } from "../../lib/format";
import type { WorkspaceSnapshot } from "../../types";
import {
  ContextIcon,
  CoverageIcon,
  MapIcon,
  OgentMark,
  RefreshIcon,
  ReviewIcon,
} from "../icons";
import type { IntelligenceTab } from "../intelligence/IntelligenceRail";

interface DocumentToolbarProps {
  workspace: WorkspaceSnapshot;
  railOpen: boolean;
  activeTab: IntelligenceTab;
  onTool: (tab: IntelligenceTab) => void;
  onReload: () => void;
  onMultiSelect: () => void;
  onCheckpoints: () => void;
}

const tools = [
  { id: "map" as const, label: "Document map", icon: MapIcon },
  { id: "context" as const, label: "Context inspector", icon: ContextIcon },
  { id: "coverage" as const, label: "Coverage", icon: CoverageIcon },
  { id: "review" as const, label: "Change review", icon: ReviewIcon },
];

export function DocumentToolbar({
  workspace,
  railOpen,
  activeTab,
  onTool,
  onReload,
  onMultiSelect,
  onCheckpoints,
}: DocumentToolbarProps) {
  const busy =
    !["idle", "error"].includes(workspace.run_status) ||
    workspace.snapshot_in_progress;
  const failed =
    workspace.run_status === "error" ||
    workspace.last_run_outcome === "error" ||
    workspace.document_index?.status === "failed";
  const statusText = failed
    ? workspace.last_error ?? "Attention needed"
    : busy
      ? workspace.snapshot_in_progress
        ? "Rendering Exact Word View"
        : "Agent work in progress"
      : workspace.active_document
        ? workspace.watch_alive
          ? "Document ready"
          : "Live View reconnecting"
        : "Ready to open a document";

  return (
    <header className="document-toolbar">
      <OgentMark className="brand-mark" />
      <div className="doc-title">
        <small>{modeLabel(workspace.document_mode)}</small>
        <span>{fileName(workspace.active_document) || "No document open"}</span>
      </div>
      <nav className="document-tools" aria-label="Document intelligence tools">
        {tools.map((item) => {
          const Icon = item.icon;
          const selected = railOpen && activeTab === item.id;
          return (
            <button
              key={item.id}
              className="toolbar-icon-button"
              type="button"
              aria-label={item.label}
              aria-pressed={selected}
              disabled={!workspace.active_document}
              onClick={() => onTool(item.id)}
            >
              <Icon />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>
      <div className="session-controls">
        <label>
          <span className="sr-only">Open Ogent session</span>
          <select
            value={workspace.session_id}
            onChange={(event) => {
              if (event.target.value !== workspace.session_id) {
                window.location.assign(
                  `/?s=${encodeURIComponent(event.target.value)}`,
                );
              }
            }}
          >
            {workspace.sessions.length ? (
              workspace.sessions.map((session) => (
                <option key={session.id} value={session.id}>
                  {session.document_name || "New workspace"} ·{" "}
                  {session.run_status || "idle"}
                </option>
              ))
            ) : (
              <option value={workspace.session_id}>Current workspace</option>
            )}
          </select>
        </label>
        <button
          className="toolbar-text-button new-window"
          type="button"
          onClick={() => window.open("/", "_blank", "noopener")}
        >
          + New window
        </button>
      </div>
      <div className="status-cluster" role="status">
        <span className="status-chip">
          <span
            className={`status-dot${failed ? " error" : busy ? " busy" : workspace.active_document ? " ready" : ""}`}
            aria-hidden="true"
          />
          <span className="status-text">{statusText}</span>
        </span>
        {workspace.active_document ? (
          <>
            <button
              className="toolbar-text-button"
              type="button"
              aria-pressed={Boolean(
                workspace.preview_selection.multi_select_mode,
              )}
              onClick={onMultiSelect}
            >
              Multi-select
            </button>
            <button
              className="toolbar-text-button"
              type="button"
              onClick={onCheckpoints}
            >
              Checkpoints
            </button>
            <button
              className="toolbar-icon-button compact"
              type="button"
              aria-label="Reload protected Live View"
              onClick={onReload}
            >
              <RefreshIcon />
            </button>
          </>
        ) : null}
      </div>
    </header>
  );
}

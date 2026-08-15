import type { DocumentIntelligenceController } from "../../hooks/useDocumentIntelligence";
import type { RunStep, WorkspaceSnapshot } from "../../types";
import {
  ContextIcon,
  CoverageIcon,
  MapIcon,
  ReviewIcon,
} from "../icons";
import { ChangeReviewPanel } from "./ChangeReviewPanel";
import { ContextInspector } from "./ContextInspector";
import { CoveragePanel } from "./CoveragePanel";
import { DocumentMap } from "./DocumentMap";

export type IntelligenceTab = "map" | "context" | "coverage" | "review";

interface IntelligenceRailProps {
  open: boolean;
  tab: IntelligenceTab;
  onTab: (value: IntelligenceTab) => void;
  intelligence: DocumentIntelligenceController;
  runSteps: RunStep[];
  workspace: WorkspaceSnapshot;
}

const tabs: Array<{
  id: IntelligenceTab;
  label: string;
  icon: typeof MapIcon;
}> = [
  { id: "map", label: "Document map", icon: MapIcon },
  { id: "context", label: "Context inspector", icon: ContextIcon },
  { id: "coverage", label: "Coverage", icon: CoverageIcon },
  { id: "review", label: "Change review", icon: ReviewIcon },
];

export function IntelligenceRail({
  open,
  tab,
  onTab,
  intelligence,
  runSteps,
  workspace,
}: IntelligenceRailProps) {
  return (
    <aside
      className={`intelligence-rail${open ? " open" : ""}`}
      aria-label="Document intelligence"
      aria-hidden={!open}
    >
      <div className="intelligence-tabs" role="tablist" aria-label="Document tools">
        {tabs.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              id={`intelligence-tab-${item.id}`}
              type="button"
              role="tab"
              aria-selected={tab === item.id}
              aria-controls={`intelligence-panel-${item.id}`}
              tabIndex={tab === item.id ? 0 : -1}
              onClick={() => onTab(item.id)}
            >
              <Icon />
              <span>{item.label}</span>
            </button>
          );
        })}
      </div>
      <div
        className="intelligence-content"
        id={`intelligence-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`intelligence-tab-${tab}`}
      >
        <header>
          <div>
            <h2>{tabs.find((item) => item.id === tab)?.label}</h2>
            <p>
              {intelligence.index
                ? `${intelligence.index.indexed_nodes} indexed nodes · ${intelligence.index.status.replaceAll("_", " ")}`
                : "Waiting for a document index"}
            </p>
          </div>
        </header>
        {intelligence.error ? (
          <div className="inline-error" role="status">
            {intelligence.error}
          </div>
        ) : null}
        {tab === "map" ? (
          <DocumentMap
            nodes={intelligence.nodes}
            selectedNodeId={intelligence.selectedNodeId}
            loading={intelligence.loading}
            loadingMore={intelligence.loadingMore}
            hasMore={intelligence.hasMore}
            onSelect={intelligence.selectNode}
            onLoadMore={intelligence.loadMore}
          />
        ) : null}
        {tab === "context" ? (
          <ContextInspector
            selectedNode={intelligence.selectedNode}
            steps={runSteps}
            searchResults={intelligence.searchResults}
            searching={intelligence.searching}
            onSearch={intelligence.search}
            onSelect={intelligence.selectNode}
            workspace={workspace}
          />
        ) : null}
        {tab === "coverage" ? (
          <CoveragePanel
            coverage={intelligence.coverage}
            index={intelligence.index}
          />
        ) : null}
        {tab === "review" ? (
          <ChangeReviewPanel
            review={intelligence.changeReview}
            onUndo={intelligence.undo}
          />
        ) : null}
      </div>
    </aside>
  );
}

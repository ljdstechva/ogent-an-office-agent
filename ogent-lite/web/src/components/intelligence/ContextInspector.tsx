import { useDeferredValue, useEffect, useState } from "react";

import { titleCase } from "../../lib/format";
import type {
  DocumentNode,
  RunStep,
  WorkspaceSnapshot,
} from "../../types";

interface ContextInspectorProps {
  selectedNode: DocumentNode | null;
  steps: RunStep[];
  searchResults: DocumentNode[];
  searching: boolean;
  onSearch: (query: string) => Promise<void>;
  onSelect: (nodeId: string) => void;
  workspace: WorkspaceSnapshot;
}

function documentName(path: string | null): string {
  return path?.split(/[\\/]/).filter(Boolean).at(-1) ?? "No document";
}

function skillName(path: string | null): string {
  const extension = path?.split(".").at(-1)?.toLowerCase();
  return (
    {
      docx: "Word",
      xlsx: "Excel",
      pptx: "PowerPoint",
      pdf: "PDF review",
    }[extension ?? ""] ?? "Not selected"
  );
}

export function ContextInspector({
  selectedNode,
  steps,
  searchResults,
  searching,
  onSearch,
  onSelect,
  workspace,
}: ContextInspectorProps) {
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const inspect = steps.find((step) => step.id === "inspect");
  const includedPaths = Array.isArray(inspect?.checkpoint?.included_paths)
    ? (inspect.checkpoint?.included_paths as string[])
    : [];
  const selections = workspace.preview_selection.targets ?? [];
  const attachments = workspace.references ?? [];
  const index = workspace.document_index;
  const nextScope = selections.length
    ? "Selected targets"
    : "Request-classified at Send";

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      void onSearch(deferredQuery);
    }, 180);
    return () => window.clearTimeout(timeout);
  }, [deferredQuery, onSearch]);

  return (
    <div className="context-inspector">
      <section
        className="inspector-section next-request-context"
        aria-labelledby="next-request-title"
      >
        <div className="section-heading">
          <h3 id="next-request-title">Next request context</h3>
          <span>Before Send</span>
        </div>
        <dl className="node-details">
          <dt>Document</dt>
          <dd>{documentName(workspace.active_document)}</dd>
          <dt>Revision</dt>
          <dd>{workspace.document_revision ?? 0}</dd>
          <dt>Scope</dt>
          <dd>{nextScope}</dd>
          <dt>Selected targets</dt>
          <dd>{selections.length}</dd>
          <dt>Pending attachments</dt>
          <dd>{attachments.length}</dd>
          <dt>Index</dt>
          <dd>
            {index
              ? `${index.status.replaceAll("_", " ")} · ${index.indexed_nodes} nodes`
              : "Not available"}
          </dd>
          <dt>Office skill</dt>
          <dd>{skillName(workspace.active_document)}</dd>
        </dl>
        {selections.length ? (
          <ul className="path-list" aria-label="Selected request targets">
            {selections.slice(0, 20).map((target) => (
              <li key={target.selection_id}>
                {target.label || target.path}
                {target.stale ? " (stale)" : ""}
              </li>
            ))}
          </ul>
        ) : null}
        {attachments.length ? (
          <p>
            Attached:{" "}
            {attachments
              .slice(0, 5)
              .map((attachment) => attachment.filename)
              .join(", ")}
            {attachments.length > 5
              ? ` and ${attachments.length - 5} more`
              : ""}
          </p>
        ) : null}
      </section>
      <label className="search-field">
        <span className="sr-only">Search indexed document content</span>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search indexed content"
        />
        <span aria-hidden="true">{searching ? "…" : "⌕"}</span>
      </label>
      {query.trim() ? (
        <div className="search-results" aria-label="Document search results">
          {searchResults.length ? (
            searchResults.map((node) => (
              <button
                type="button"
                key={node.node_id}
                onClick={() => onSelect(node.node_id)}
              >
                <strong>{node.title || titleCase(node.kind)}</strong>
                <span>{node.text || node.stable_path}</span>
              </button>
            ))
          ) : (
            <p>{searching ? "Searching…" : "No indexed matches."}</p>
          )}
        </div>
      ) : null}
      <section className="inspector-section" aria-labelledby="agent-read-title">
        <div className="section-heading">
          <h3 id="agent-read-title">Agent read set</h3>
          <span>{includedPaths.length}</span>
        </div>
        {inspect ? (
          <>
            <p>
              {inspect.state === "completed"
                ? "These paths were projected into the bounded provider context."
                : "The read set is still being assembled."}
            </p>
            <ul className="path-list">
              {includedPaths.slice(0, 40).map((path) => (
                <li key={path}>{path}</li>
              ))}
            </ul>
            {includedPaths.length > 40 ? (
              <p>{includedPaths.length - 40} additional paths are recorded.</p>
            ) : null}
          </>
        ) : (
          <p>No run has recorded a bounded read set yet.</p>
        )}
      </section>
      <section className="inspector-section" aria-labelledby="selected-node-title">
        <div className="section-heading">
          <h3 id="selected-node-title">Selected node</h3>
          {selectedNode ? <span>{titleCase(selectedNode.kind)}</span> : null}
        </div>
        {selectedNode ? (
          <dl className="node-details">
            <dt>Title</dt>
            <dd>{selectedNode.title || "Untitled node"}</dd>
            <dt>Stable locator</dt>
            <dd>{selectedNode.stable_path}</dd>
            <dt>Resolution</dt>
            <dd>
              {selectedNode.locator?.resolvable === false
                ? "Informational only"
                : "Resolvable"}
            </dd>
            {selectedNode.text ? (
              <>
                <dt>Indexed text</dt>
                <dd className="node-excerpt">{selectedNode.text}</dd>
              </>
            ) : null}
          </dl>
        ) : (
          <p>Select a map node to inspect its stable locator and indexed text.</p>
        )}
      </section>
    </div>
  );
}

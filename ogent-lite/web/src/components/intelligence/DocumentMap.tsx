import { useVirtualizer } from "@tanstack/react-virtual";
import { useMemo, useRef } from "react";

import type { DocumentNode } from "../../types";
import { ChevronIcon } from "../icons";

interface DocumentMapProps {
  nodes: DocumentNode[];
  selectedNodeId: string | null;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  onSelect: (nodeId: string) => void;
  onLoadMore: () => Promise<void>;
}

function nodeDepth(node: DocumentNode): number {
  const path = node.stable_path;
  if (!path) return 0;
  return Math.max(0, path.split("/").filter(Boolean).length - 1);
}

function nodeLabel(node: DocumentNode): string {
  return (
    node.title?.trim() ||
    node.sheet_name ||
    (node.slide_number ? `Slide ${node.slide_number}` : "") ||
    `${node.kind.replaceAll("_", " ")} ${Number(node.ordinal ?? 0) + 1}`
  );
}

export function DocumentMap({
  nodes,
  selectedNodeId,
  loading,
  loadingMore,
  hasMore,
  onSelect,
  onLoadMore,
}: DocumentMapProps) {
  const parentRef = useRef<HTMLDivElement>(null);
  const ordered = useMemo(
    () =>
      [...nodes].sort((left, right) =>
        left.stable_path.localeCompare(right.stable_path, undefined, {
          numeric: true,
        }),
      ),
    [nodes],
  );
  const virtualizer = useVirtualizer({
    useFlushSync: false,
    count: ordered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 42,
    overscan: 12,
  });

  if (loading && !ordered.length) {
    return <div className="panel-state">Building the document map…</div>;
  }
  if (!ordered.length) {
    return (
      <div className="panel-state">
        The map will appear when the current document index is ready.
      </div>
    );
  }
  return (
    <>
      <div
        className="virtual-node-list"
        ref={parentRef}
        role="tree"
        aria-label="Indexed document structure"
      >
        <div
          className="virtual-node-canvas"
          style={{ height: `${virtualizer.getTotalSize()}px` }}
        >
          {virtualizer.getVirtualItems().map((virtualItem) => {
            const node = ordered[virtualItem.index];
            const selected = node.node_id === selectedNodeId;
            return (
              <button
                key={node.node_id}
                ref={virtualizer.measureElement}
                data-index={virtualItem.index}
                className={`document-node${selected ? " selected" : ""}`}
                type="button"
                role="treeitem"
                aria-selected={selected}
                style={{
                  transform: `translateY(${virtualItem.start}px)`,
                  paddingInlineStart: `${12 + Math.min(5, nodeDepth(node)) * 12}px`,
                }}
                onClick={() => onSelect(node.node_id)}
                title={node.stable_path}
              >
                <ChevronIcon className="node-chevron" />
                <span>
                  <strong>{nodeLabel(node)}</strong>
                  <small>{node.kind.replaceAll("_", " ")}</small>
                </span>
              </button>
            );
          })}
        </div>
      </div>
      {hasMore ? (
        <button
          className="panel-load-more"
          type="button"
          disabled={loadingMore}
          onClick={() => void onLoadMore()}
        >
          {loadingMore ? "Loading more…" : "Load more nodes"}
        </button>
      ) : null}
    </>
  );
}

import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import type {
  ChangeReview,
  CoverageReview,
  DocumentIndex,
  DocumentNode,
  DocumentNodesPage,
} from "../types";

interface IntelligenceState {
  index: DocumentIndex | null;
  nodes: DocumentNode[];
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  selectedNodeId: string | null;
  selectedNode: DocumentNode | null;
  searchResults: DocumentNode[];
  searching: boolean;
  coverage: CoverageReview | null;
  changeReview: ChangeReview | null;
}

export interface DocumentIntelligenceController extends IntelligenceState {
  hasMore: boolean;
  selectNode: (nodeId: string) => void;
  loadMore: () => Promise<void>;
  search: (query: string) => Promise<void>;
  refreshReview: () => Promise<void>;
  undo: (changesetId: string) => Promise<void>;
}

const pageSize = 250;

function asMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "Document intelligence is unavailable.";
}

export function useDocumentIntelligence(
  workspaceId: string,
  activeDocument: string | null,
  documentIndex: DocumentIndex | null | undefined,
  runId: string | null | undefined,
  onError: (message: string) => void,
): DocumentIntelligenceController {
  const [state, setState] = useState<IntelligenceState>({
    index: documentIndex ?? null,
    nodes: [],
    loading: false,
    loadingMore: false,
    error: null,
    selectedNodeId: null,
    selectedNode: null,
    searchResults: [],
    searching: false,
    coverage: null,
    changeReview: null,
  });

  const loadNodes = useCallback(
    async (offset: number, append: boolean) => {
      const result = await api<DocumentNodesPage>(
        `/api/workspaces/${workspaceId}/document-nodes` +
          `?offset=${offset}&limit=${pageSize}&include_text=true`,
      );
      setState((current) => ({
        ...current,
        nodes: append ? [...current.nodes, ...result.nodes] : result.nodes,
        selectedNodeId:
          current.selectedNodeId ?? result.nodes[0]?.node_id ?? null,
        selectedNode:
          current.selectedNode ??
          result.nodes[0] ??
          null,
        error: null,
      }));
    },
    [workspaceId],
  );

  const refreshReview = useCallback(async () => {
    if (!activeDocument) {
      setState((current) => ({
        ...current,
        coverage: null,
        changeReview: null,
      }));
      return;
    }
    const [coverageResult, changeResult] = await Promise.allSettled([
      api<CoverageReview>(
        `/api/workspaces/${workspaceId}/run-coverage${
          runId ? `?run_id=${encodeURIComponent(runId)}` : ""
        }`,
      ),
      api<ChangeReview>(
        `/api/workspaces/${workspaceId}/change-review`,
      ),
    ]);
    setState((current) => ({
      ...current,
      coverage:
        coverageResult.status === "fulfilled"
          ? coverageResult.value
          : null,
      changeReview:
        changeResult.status === "fulfilled" ? changeResult.value : null,
    }));
  }, [activeDocument, runId, workspaceId]);

  useEffect(() => {
    setState((current) => ({
      ...current,
      index: documentIndex ?? null,
    }));
  }, [documentIndex]);

  useEffect(() => {
    let cancelled = false;
    if (
      !activeDocument ||
      !documentIndex ||
      !["complete", "partial"].includes(documentIndex.status)
    ) {
      setState((current) => ({
        ...current,
        nodes: [],
        selectedNodeId: null,
        selectedNode: null,
        loading: false,
        error:
          documentIndex?.status === "failed"
            ? "The document index failed. Live View remains available."
            : null,
      }));
      void refreshReview();
      return () => {
        cancelled = true;
      };
    }
    setState((current) => ({ ...current, loading: true, error: null }));
    void loadNodes(0, false)
      .catch((error) => {
        if (!cancelled) {
          setState((current) => ({
            ...current,
            error: asMessage(error),
          }));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setState((current) => ({ ...current, loading: false }));
        }
      });
    void refreshReview();
    return () => {
      cancelled = true;
    };
  }, [
    activeDocument,
    documentIndex?.revision_id,
    documentIndex?.status,
    loadNodes,
    refreshReview,
  ]);

  const selectNode = useCallback(
    (nodeId: string) => {
      const selected =
        [...state.nodes, ...state.searchResults].find(
          (node) => node.node_id === nodeId,
        ) ?? null;
      setState((current) => ({
        ...current,
        selectedNodeId: nodeId,
        selectedNode: selected,
      }));
      if (!selected || selected.locator?.resolvable === false) {
        return;
      }
      void api<{ message?: string }>(
        `/api/workspaces/${workspaceId}/document-selection`,
        {
          method: "POST",
          body: JSON.stringify({ node_ids: [nodeId] }),
        },
      )
        .then((result) => {
          if (result.message) onError(result.message);
        })
        .catch((error) => onError(asMessage(error)));
    },
    [onError, state.nodes, state.searchResults, workspaceId],
  );

  const loadMore = useCallback(async () => {
    if (state.loadingMore) return;
    setState((current) => ({ ...current, loadingMore: true }));
    try {
      await loadNodes(state.nodes.length, true);
    } catch (error) {
      onError(asMessage(error));
    } finally {
      setState((current) => ({ ...current, loadingMore: false }));
    }
  }, [loadNodes, onError, state.loadingMore, state.nodes.length]);

  const search = useCallback(
    async (query: string) => {
      const clean = query.trim();
      if (!clean) {
        setState((current) => ({ ...current, searchResults: [] }));
        return;
      }
      setState((current) => ({ ...current, searching: true }));
      try {
        const result = await api<{ hits: DocumentNode[] }>(
          `/api/workspaces/${workspaceId}/document-search` +
            `?q=${encodeURIComponent(clean)}&limit=100`,
        );
        setState((current) => ({
          ...current,
          searchResults: result.hits ?? [],
        }));
      } catch (error) {
        onError(asMessage(error));
      } finally {
        setState((current) => ({ ...current, searching: false }));
      }
    },
    [onError, workspaceId],
  );

  const undo = useCallback(
    async (changesetId: string) => {
      try {
        const result = await api<{
          message?: string;
          change_review?: ChangeReview;
        }>(`/api/workspaces/${workspaceId}/undo`, {
          method: "POST",
          body: JSON.stringify({ changeset_id: changesetId }),
        });
        if (result.change_review) {
          setState((current) => ({
            ...current,
            changeReview: result.change_review ?? null,
          }));
        }
        onError(result.message ?? "The completed run was undone.");
      } catch (error) {
        onError(asMessage(error));
        throw error;
      }
    },
    [onError, workspaceId],
  );

  return {
    ...state,
    hasMore:
      Boolean(state.index?.indexed_nodes) &&
      state.nodes.length < Number(state.index?.indexed_nodes ?? 0),
    selectNode,
    loadMore,
    search,
    refreshReview,
    undo,
  };
}

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { WorkspaceSnapshot } from "../../types";
import { ContextInspector } from "./ContextInspector";

const workspace: WorkspaceSnapshot = {
  session_id: "aaaaaaaa",
  active_document: String.raw`C:\Reports\SMR Q2.docx`,
  document_revision: 7,
  watch_url: null,
  run_status: "idle",
  last_run_outcome: "neutral",
  transcript: [],
  conversation_generation: 1,
  references: [
    { id: "reference", filename: "Lab results.pdf", size: 1200 },
  ],
  retained_attachments: [],
  preview_selection: {
    targets: [
      {
        selection_id: "selection",
        path: "/body/tbl[2]",
        label: "Effluent results",
      },
    ],
  },
  document_index: {
    status: "complete",
    progress: 1,
    indexed_nodes: 92,
    total_estimate: 92,
  },
  recent: [],
  sessions: [],
  agent_capabilities: { providers: [] },
  recovery: {},
  session_memory: {},
  activity: [],
  stream_connected: true,
};

describe("ContextInspector", () => {
  it("shows the exact context that will be available before Send", () => {
    render(
      <ContextInspector
        selectedNode={null}
        steps={[]}
        searchResults={[]}
        searching={false}
        onSearch={vi.fn(async () => undefined)}
        onSelect={vi.fn()}
        workspace={workspace}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Next request context" }),
    ).toBeInTheDocument();
    expect(screen.getByText("SMR Q2.docx")).toBeInTheDocument();
    expect(screen.getAllByText("Selected targets")).toHaveLength(2);
    expect(screen.getByText("Effluent results")).toBeInTheDocument();
    expect(screen.getByText(/Lab results\.pdf/)).toBeInTheDocument();
    expect(screen.getByText("Word")).toBeInTheDocument();
  });
});

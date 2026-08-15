import { describe, expect, it } from "vitest";

import { initialWorkspace } from "./initial";
import { workspaceReducer } from "./workspaceReducer";

describe("workspaceReducer", () => {
  it("ignores scoped events from an older conversation generation", () => {
    const state = {
      ...initialWorkspace,
      conversation_generation: 3,
      transcript: [],
    };

    const next = workspaceReducer(state, {
      type: "event",
      envelope: {
        type: "message",
        generation: 2,
        data: { role: "assistant", text: "stale" },
      },
    });

    expect(next).toBe(state);
    expect(next.transcript).toEqual([]);
  });

  it("assembles assistant deltas once and advances run steps", () => {
    const streaming = workspaceReducer(initialWorkspace, {
      type: "event",
      envelope: {
        type: "assistant.delta",
        generation: 1,
        data: {
          run_id: "a".repeat(32),
          delta: "Verified ",
          character_count: 9,
          delta_index: 1,
        },
      },
    });
    const duplicate = workspaceReducer(streaming, {
      type: "event",
      envelope: {
        type: "assistant.delta",
        generation: 1,
        data: {
          run_id: "a".repeat(32),
          delta: "Verified ",
          character_count: 9,
          delta_index: 1,
        },
      },
    });
    const stepped = workspaceReducer(duplicate, {
      type: "event",
      envelope: {
        type: "run.step.completed",
        generation: 1,
        data: {
          step: {
            id: "inspect",
            sequence: 1,
            description: "Inspect the bounded read set",
            target_node_ids: [],
            mutates: false,
            proof: "Indexed context",
            dependencies: [],
            estimated_work_units: 1,
            state: "completed",
          },
        },
      },
    });

    expect(duplicate.assistant_stream?.text).toBe("Verified ");
    expect(stepped.run_steps?.[0]).toMatchObject({
      id: "inspect",
      state: "completed",
    });
  });

  it("clears transient work on a conversation reset", () => {
    const state = {
      ...initialWorkspace,
      conversation_generation: 1,
      transcript: [{ role: "user" as const, text: "old" }],
      references: [
        { id: "ref", filename: "source.pdf", size: 100 },
      ],
      run_steps: [],
      last_run_outcome: "edit_completed",
    };

    const next = workspaceReducer(state, {
      type: "event",
      envelope: {
        type: "conversation_reset",
        generation: 2,
        data: { preview_selection: { targets: [] } },
      },
    });

    expect(next.conversation_generation).toBe(2);
    expect(next.transcript).toEqual([]);
    expect(next.references).toEqual([]);
    expect(next.last_run_outcome).toBe("neutral");
  });
});

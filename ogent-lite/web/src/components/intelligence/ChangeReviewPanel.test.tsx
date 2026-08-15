import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChangeReviewPanel } from "./ChangeReviewPanel";

describe("ChangeReviewPanel", () => {
  it("discloses verification, excerpts, metadata, and undo eligibility", () => {
    render(
      <ChangeReviewPanel
        review={{
          changeset_id: "c".repeat(32),
          affected_paths: ["/body/p[1]"],
          assertions: { officecli_validate: true },
          excerpts: [
            {
              path: "/body/p[1]",
              before: "Old permit number",
              after: "Updated permit number",
            },
          ],
          formula_style_changes: [
            { path: "/body/p[1]", fields: ["style_name"] },
          ],
          can_undo: true,
          undone: false,
        }}
        onUndo={vi.fn(async () => undefined)}
      />,
    );

    expect(screen.getByText("Verified change")).toBeInTheDocument();
    expect(screen.getByText("officecli validate")).toBeInTheDocument();
    expect(screen.getByText("Passed")).toBeInTheDocument();
    expect(screen.getByText("Before and after")).toBeInTheDocument();
    expect(screen.getByText(/style_name/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Undo this run/ }),
    ).toBeEnabled();
  });
});

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

const sessionId = "aaaaaaaa";
const documentId = "d".repeat(32);
const runId = "b".repeat(32);
const changesetId = "c".repeat(32);

function planStep(
  id: string,
  sequence: number,
  description: string,
  state = "completed",
  checkpoint?: Record<string, unknown>,
) {
  return {
    id,
    sequence,
    description,
    target_node_ids: [],
    mutates: id === "edit",
    tool: id === "edit" ? "officecli" : null,
    proof: id === "edit" ? "Targeted readback" : "Indexed read set",
    dependencies: sequence === 1 ? [] : ["inspect"],
    estimated_work_units: 1,
    state,
    ...(checkpoint ? { checkpoint } : {}),
  };
}

function snapshot(state: "ready" | "empty" | "error" | "resumable") {
  const empty = state === "empty";
  const resumable = state === "resumable";
  const failed = state === "error" || resumable;
  return {
    app: "Ogent Lite",
    version: "test",
    session_id: sessionId,
    active_document: empty ? null : String.raw`C:\Reports\SMR Q2.docx`,
    source_document: empty ? null : String.raw`C:\Reports\SMR Q2.docx`,
    document_mode: empty ? "none" : "local_direct",
    document_id: empty ? null : documentId,
    document_revision: empty ? 0 : 7,
    watch_url: empty ? null : "http://127.0.0.1:26320",
    watch_alive: !empty,
    watch_port: empty ? null : 26320,
    watch_generation: empty ? null : "watch-generation",
    preview_identity: empty
      ? null
      : {
          session_id: sessionId,
          document_id: documentId,
          watch_port: 26320,
          watch_generation: "watch-generation",
        },
    run_status: failed ? "error" : "idle",
    last_run_outcome: failed ? "error" : "edit_completed",
    run_id: empty ? null : runId,
    last_error: resumable
      ? "The whole-document review was interrupted after a durable checkpoint."
      : failed
        ? "The last run failed verification."
        : null,
    run_plan: empty
      ? null
      : {
          schema_version: 1,
          goal: "Review and update the selected compliance section.",
          mode: "edit",
          scope: "selected",
          complexity: "structured",
          steps: [],
          target_node_ids: [],
          expected_mutations: ["/body/p[1]"],
          verification_assertions: ["officecli_validate"],
          coverage_requirement: {},
          estimated_work_units: 3,
        },
    run_steps: empty
      ? []
      : [
          planStep("inspect", 1, "Inspect indexed context"),
          planStep(
            "edit",
            2,
            resumable ? "Review structural partitions" : "Apply verified document edits",
            resumable ? "failed" : "completed",
            resumable
              ? {
                  partition_manifest_blob_id: "f".repeat(64),
                  completed_partitions: 2,
                  next_partition: 3,
                  partition_count: 3,
                }
              : undefined,
          ),
          planStep("verify", 3, "Validate and confirm the result"),
        ],
    transcript: [],
    transcript_paged: !empty,
    transcript_total: empty ? 0 : 320,
    transcript_page_url: `/api/workspaces/${sessionId}/turns`,
    conversation_generation: 1,
    references: empty
      ? []
      : [
          {
            id: "reference",
            filename: "Laboratory results.pdf",
            size: 1200,
            status: "Ready",
          },
        ],
    retained_attachments: [],
    preview_selection: {
      targets: empty
        ? []
        : [
            {
              selection_id: "selection",
              path: "/body/tbl[2]",
              label: "Effluent results",
            },
          ],
      multi_select_mode: false,
    },
    document_index: empty
      ? null
      : {
          revision_id: "i".repeat(32),
          status: failed ? "failed" : "complete",
          progress: failed ? 0.42 : 1,
          indexed_nodes: 600,
          total_estimate: 600,
        },
    recent: [],
    sessions: [
      {
        id: sessionId,
        document_name: empty ? "New workspace" : "SMR Q2.docx",
        run_status: failed ? "error" : "idle",
      },
    ],
    agent_capabilities: {
      refreshing: false,
      providers: [
        {
          id: "codex",
          label: "Codex",
          live: true,
          status: "ready",
          models: [
            {
              id: "gpt-5",
              displayName: "GPT-5",
              efforts: ["medium", "high"],
              effortsVerified: true,
            },
          ],
        },
      ],
    },
    recovery: {},
    session_memory: {},
    activity: [],
    complex_layout: false,
    stream_connected: false,
    features: {
      large_text_assets: true,
      strict_disk_forecast: true,
    },
    quotas: {
      max_inline_turn_characters: 200_000,
      max_reference_file_bytes: 50 * 1024 * 1024,
    },
  };
}

function nodes() {
  return Array.from({ length: 250 }, (_, index) => ({
    node_id: index.toString(16).padStart(32, "0"),
    stable_path: `/body/p[${index + 1}]`,
    parent_path: "/document",
    kind: index % 9 === 0 ? "heading" : "paragraph",
    title: index % 9 === 0 ? `Section ${index / 9 + 1}` : `Paragraph ${index + 1}`,
    text: `Indexed content for item ${index + 1}.`,
    ordinal: index,
    locator: {
      namespace: "officecli",
      stability: "stable",
      resolvable: true,
      source_paths: [`/body/p[${index + 1}]`],
    },
  }));
}

async function fulfillJson(route: Route, value: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(value),
  });
}

async function installBackend(
  page: Page,
  state: "ready" | "empty" | "error" | "resumable" = "ready",
  observer?: {
    uploads: Array<{ filename: string; text: string }>;
    chatMessages: string[];
    resumeRuns?: string[];
  },
) {
  await page.addInitScript(() => {
    class MockEventSource extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSED = 2;
      readonly url: string;
      readonly withCredentials = false;
      readyState = 0;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;

      constructor(url: string | URL) {
        super();
        this.url = String(url);
        queueMicrotask(() => {
          this.readyState = 1;
          this.onopen?.(new Event("open"));
        });
      }

      close() {
        this.readyState = 2;
      }
    }
    Object.defineProperty(window, "EventSource", {
      configurable: true,
      value: MockEventSource,
    });
  });

  const workspace = snapshot(state);
  const documentNodes = nodes();
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/health") {
      await fulfillJson(route, workspace);
      return;
    }
    if (path.endsWith("/turns")) {
      await fulfillJson(route, {
        items: Array.from({ length: 100 }, (_, index) => ({
          turn_id: index.toString(16).padStart(32, "0"),
          sequence: index + 1,
          role: index % 2 ? "assistant" : "user",
          text:
            index % 2
              ? `Verified response ${index + 1}.`
              : `Document request ${index + 1}.`,
        })),
        total: 320,
        conversation_generation: 1,
      });
      return;
    }
    if (path.endsWith("/document-nodes")) {
      await fulfillJson(route, {
        revision_id: "i".repeat(32),
        status: "complete",
        offset: 0,
        nodes: documentNodes,
      });
      return;
    }
    if (path.endsWith("/document-search")) {
      await fulfillJson(route, { hits: documentNodes.slice(0, 4) });
      return;
    }
    if (path.endsWith("/document-selection")) {
      await fulfillJson(route, {
        message: "Focused the selected map node and scoped the next request.",
        preview_selection: workspace.preview_selection,
      });
      return;
    }
    if (path.endsWith("/run-coverage")) {
      await fulfillJson(route, {
        run_id: runId,
        required: true,
        complete: false,
        categories: [
          {
            category: "headings",
            required: 20,
            reviewed: 20,
            complete: true,
          },
          {
            category: "figures",
            required: 2,
            reviewed: 2,
            complete: true,
          },
        ],
        unsupported: [],
        visual_interpretation_used: [],
        disclosure:
          "Structural review is complete; two figures still require visual interpretation.",
      });
      return;
    }
    if (path.endsWith("/change-review")) {
      await fulfillJson(route, {
        changeset_id: changesetId,
        run_id: runId,
        created_at: "2026-07-29T10:00:00+08:00",
        outcome: "edit_completed",
        affected_paths: ["/body/p[1]"],
        assertions: {
          officecli_validate: true,
          targeted_readback: true,
          preview_confirmed: true,
        },
        excerpts: [
          {
            path: "/body/p[1]",
            before: "Old permit number",
            after: "Updated permit number",
          },
        ],
        formula_style_changes: [],
        can_undo: true,
        undone: false,
      });
      return;
    }
    if (
      path.endsWith(`/runs/${runId}/resume`) &&
      request.method() === "POST"
    ) {
      observer?.resumeRuns?.push(runId);
      await fulfillJson(
        route,
        {
          run_id: "e".repeat(32),
          resumed_from_run_id: runId,
          completed_partitions: 2,
          partition_count: 3,
        },
        202,
      );
      return;
    }
    if (path === "/reference/upload" && request.method() === "POST") {
      const filename = decodeURIComponent(
        request.headers()["x-ogent-filename"] ?? "",
      );
      const text = request.postDataBuffer()?.toString("utf8") ?? "";
      observer?.uploads.push({ filename, text });
      const attachment = {
        id: "large-text-reference",
        filename,
        size: Buffer.byteLength(text, "utf8"),
        status: "Ready",
      };
      await fulfillJson(
        route,
        {
          attachment,
          references: [attachment],
          retained: [attachment],
        },
        201,
      );
      return;
    }
    if (path === "/chat" && request.method() === "POST") {
      const payload = request.postDataJSON() as { message?: string };
      observer?.chatMessages.push(payload.message ?? "");
      await fulfillJson(route, { message: "Run started.", run_id: runId }, 202);
      return;
    }
    if (path === "/preview" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "text/html",
        body: `<!doctype html><html><body><main><h1>SMR Q2</h1><p>Live document content</p></main><script>
          parent.postMessage({
            protocol: "ogent-preview-status",
            version: 1,
            type: "preview.ready",
            document_id: "${documentId}",
            watch_generation: "watch-generation",
            meaningful: true,
            metrics: { text_characters: 42 }
          }, location.origin);
        </script></body></html>`,
      });
      return;
    }
    if (
      request.method() === "POST" &&
      [
        "/preview/status",
        "/session/focus",
        "/selection/multi-mode",
      ].includes(path)
    ) {
      await fulfillJson(route, {});
      return;
    }
    await route.continue();
  });
}

async function expectNoViewportOverflow(page: Page) {
  const overflow = await page.evaluate(() => {
    const viewport = {
      width: window.innerWidth,
      height: window.innerHeight,
    };
    const controls = [
      ...document.querySelectorAll<HTMLElement>(
        "button:not([hidden]), input:not([hidden]), textarea:not([hidden]), select:not([hidden])",
      ),
    ];
    return {
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: viewport.width,
      clippedControls: controls
        .filter((element) => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          const intersects =
            rect.right > 0 &&
            rect.bottom > 0 &&
            rect.left < viewport.width &&
            rect.top < viewport.height;
          return (
            intersects &&
            style.display !== "none" &&
            style.visibility !== "hidden" &&
            (rect.left < -1 ||
              rect.right > viewport.width + 1 ||
              rect.top < -1 ||
              rect.bottom > viewport.height + 1)
          );
        })
        .map((element) => ({
          label:
            element.getAttribute("aria-label") ||
            element.textContent?.trim().slice(0, 80) ||
            element.tagName,
          rect: element.getBoundingClientRect().toJSON(),
        })),
    };
  });
  expect(overflow.documentWidth).toBeLessThanOrEqual(
    overflow.viewportWidth + 1,
  );
  expect(overflow.clippedControls).toEqual([]);
}

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
});

test("ready workspace exposes durable document controls without accessibility violations", async ({
  page,
}, testInfo) => {
  await installBackend(page, "ready");
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await page.goto("/");

  await expect(page.getByText("SMR Q2.docx").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Ogent" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Exact Word View" }),
  ).toBeEnabled();

  await page.getByRole("button", { name: "Document map" }).click();
  await expect(
    page.getByRole("tree", { name: "Indexed document structure" }),
  ).toBeVisible();
  await page.getByRole("treeitem").first().click();
  await expect(
    page.getByText(/Focused the selected map node/),
  ).toBeVisible();

  await page.getByRole("button", { name: "Context inspector" }).click();
  await expect(
    page.getByRole("heading", { name: "Next request context" }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("region", { name: "Live document" })
      .getByText("Effluent results"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Coverage" }).click();
  await expect(page.getByText(/Structural review is complete/)).toBeVisible();

  await page.getByRole("button", { name: "Change review" }).click();
  await expect(page.getByText("Verified change")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Undo this run/ }),
  ).toBeEnabled();

  await expectNoViewportOverflow(page);
  const accessibility = await new AxeBuilder({ page })
    .exclude("iframe")
    .analyze();
  expect(
    accessibility.violations.map((violation) => ({
      id: violation.id,
      targets: violation.nodes.map((node) => node.target),
    })),
  ).toEqual([]);
  expect(consoleErrors).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("ready-workspace.png"),
    fullPage: true,
  });
});

test("empty and error states remain actionable", async ({ page }) => {
  await installBackend(page, "empty");
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Your document, live." }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Document map" }),
  ).toBeDisabled();

  await page.unrouteAll({ behavior: "wait" });
  await installBackend(page, "error");
  await page.reload();
  await expect(
    page
      .getByRole("region", { name: "Live document" })
      .getByText("The last run failed verification."),
  ).toBeVisible();
  await expectNoViewportOverflow(page);
});

test("checkpointed whole-document review resumes without resending", async ({
  page,
}) => {
  const observer = {
    uploads: [] as Array<{ filename: string; text: string }>,
    chatMessages: [] as string[],
    resumeRuns: [] as string[],
  };
  await installBackend(page, "resumable", observer);
  await page.goto("/");

  const resume = page.getByRole("button", {
    name: "Resume interrupted review",
  });
  await expect(resume).toBeEnabled();
  await resume.click();
  await expect.poll(() => observer.resumeRuns).toEqual([runId]);
  await expect(
    page.getByText("Resuming after 2/3 completed partitions."),
  ).toBeVisible();
  expect(observer.chatMessages).toEqual([]);
});

test("large pasted text streams to a lossless indexed asset before Send", async ({
  page,
}) => {
  const observer = {
    uploads: [] as Array<{ filename: string; text: string }>,
    chatMessages: [] as string[],
  };
  await installBackend(page, "ready", observer);
  await page.goto("/");
  const pasted = `Review this complete evidence set.\n${"é".repeat(200_000)}`;
  await page.getByLabel("Document request").fill(pasted);
  await expect(page.getByText(/sends as an indexed text asset/)).toBeVisible();
  await page.getByRole("button", { name: "Send" }).click();
  await expect
    .poll(() => observer.chatMessages.length)
    .toBe(1);

  expect(observer.uploads).toHaveLength(1);
  expect(observer.uploads[0].text).toBe(pasted);
  expect(observer.uploads[0].filename).toMatch(/^pasted-text-\d+-[0-9a-f]{8}\.txt$/);
  expect(observer.chatMessages[0].length).toBeLessThan(200_000);
  expect(observer.chatMessages[0]).toContain(observer.uploads[0].filename);
});

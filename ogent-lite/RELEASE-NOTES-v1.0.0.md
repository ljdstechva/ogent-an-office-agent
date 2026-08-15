# Ogent Lite v1.0.0 release notes

Date: 2026-07-29

Ogent 1.0 changes the product from a prompt-oriented OfficeCLI bridge into a
local-first, revision-aware document-intelligence workspace while preserving
the existing Windows launcher, localhost route contract, backup guarantees,
and live Office preview.

## Durable workspaces

- Complete turns are stored losslessly in SQLite WAL and content-addressed
  blobs; display excerpts are no longer canonical content.
- Transcripts are paged and durable run events replay by monotonic SSE ID.
- Workspace actors serialize state changes and reject stale run, revision, and
  conversation-generation results.
- Interrupted whole-document read-only reviews can resume completed
  partitions against the same document revision.

## Deterministic Office safety

- Every active Office-document Send resolves the `word`, `excel`, or `pptx`
  skill and performs a real OfficeCLI preflight before provider execution.
- Typed operations are restricted to the exact active document and authorized
  stable paths; the raw command tool remains a restricted compatibility escape
  hatch.
- Mutation completion requires audited mutation evidence, targeted readback,
  OfficeCLI validation, package-hash proof, and preview status.
- A requested edit with no mutation reports `No change was made.`
- Failed post-edit verification restores the run rollback snapshot.
- Completed verified edits expose changed paths, before/after excerpts,
  validation evidence, preview status, and one-run Undo.

## Document intelligence

- DOCX, XLSX, PPTX, and searchable PDF files receive revision-bound structural
  manifests, indexed nodes, stable locators, FTS chunks, and structural graph
  edges.
- Small revisions reuse unchanged content blobs and inherited search
  materialization; only changed text nodes create new FTS chunks. A generated
  normal-edit benchmark enforces p95 reindexing below one second.
- Broad reviews use model-budget-aware structural partitions rather than one
  oversized prompt. Successful partitions checkpoint independently and feed a
  bounded hierarchical synthesis.
- Coverage reports distinguish reviewed, pending, unreadable, unsupported, and
  visual-analysis requirements. Ogent does not claim complete review while a
  required category is incomplete.
- Relevant chart evidence pairs a lazy rendered region with semantic series,
  categories, values, formulas, axes, and source ranges. Text-only models leave
  visual coverage explicitly incomplete.
- Reference files begin extraction and indexing at upload time in a bounded
  worker pool.

## Frontend

- The embedded legacy page is replaced by a React/TypeScript/Vite client while
  retaining the Ogent brand and two-pane workspace.
- The interface adds a virtualized document map and transcript, context
  inspector, plan timeline, streamed provisional response, coverage panel,
  change review, Undo, and interrupted-review Resume.
- Empty, loading, indexing, degraded-preview, recovery, failure, desktop, and
  mobile states have automated browser coverage.
- Dialog focus, keyboard operation, visible focus, reduced motion, live status
  announcements, viewport overflow, console errors, and automated WCAG checks
  are exercised in Playwright.

## Scale, recovery, and security

- Large pasted text uploads as a lossless indexed text asset before the turn is
  submitted.
- Quotas, disk forecasting, rotating content-safe logs, and stale partial-file
  cleanup are configurable.
- Fault injection covers provider, OfficeCLI, database-lock, disk-full,
  Word-lock, and preview-mismatch boundaries.
- Generated stress fixtures cover 300-page/slide documents and a
  100-sheet/250,000-cell workbook without storing large binary fixtures in Git.
- Security tests cover traversal, symlinks, malformed OOXML, prompt injection,
  ZIP bombs, active-document identity, and typed-operation scope isolation.
- Migrations have forward and rollback coverage through the current schema.

## Architecture and compatibility

`ogent.py` is now a short compatibility launcher. Domain, application, ports,
infrastructure, API, and frontend code live in separate modules. Production
source files remain below the first-stage 1,000-line extraction gate. New
application services remain below 400 lines except the documented single-writer
turn saga.

The current localhost HTTP adapter and runtime fragments remain as an explicit
incremental-migration boundary so existing shell integration and endpoints do
not break. Pyright excludes only those late-bound adapters; the statically
composed core passes with zero diagnostics. Reviewed complexity exceptions and
their removal conditions are listed in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Known limits

- Ogent does not provide literal unlimited model context. It uses durable
  storage, indexing, bounded retrieval, partitions, checkpoints, and honest
  coverage instead.
- Image-only PDFs still need OCR. PDF editing still operates on a converted
  DOCX copy.
- Live View is semantic and can approximate complex Word floating layouts;
  Exact Word View remains the authoritative rendering check.
- Provider input limits that are not reported reliably use a conservative
  configurable budget.
- Warm provider transport remains feature-gated; fresh isolated processes are
  the safe fallback.
- Application and compatibility branches remain below the final 90% branch
  target, and overall branch coverage remains below the final 80% target. The
  debt is kept visible in coverage reports rather than omitted; CI enforces
  the current 68% combined baseline to prevent regression.

## Verification

The release workflow runs the full Python regression suite, Ruff lint and
format checks, the documented complexity gate, Pyright on the typed core,
TypeScript checking, frontend unit tests, a production build, and desktop and
mobile Playwright tests. Live-provider tests remain opt-in because CI has no
provider login and must not consume inference.

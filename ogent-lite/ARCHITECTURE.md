# Ogent 1.0 architecture

Ogent is a local-first modular monolith. The browser, localhost API, workspace
actors, document-intelligence services, OfficeCLI gateway, provider adapters,
SQLite database, and content-addressed blobs ship as one Windows application.
No component is independently deployed.

## Runtime boundaries

The dependency direction is:

```text
web UI -> localhost API -> workspace actor -> application services
                                      |-> domain model
                                      |-> ports -> infrastructure adapters
```

- `ogent_app/domain` contains typed state, planning, scope, capability,
  document-index, and verification records. It does not import HTTP,
  subprocess, or filesystem adapters.
- `ogent_app/application` owns use cases: actor commands, capability bootstrap,
  planning, retrieval, partitioned execution, streaming, verification,
  rollback, change review, visual evidence, and reference indexing.
- `ogent_app/ports` contains provider and process-supervisor contracts.
- `ogent_app/infrastructure` owns SQLite WAL repositories,
  content-addressed blobs, OOXML/PDF indexing, OfficeCLI argument-array
  execution, resource forecasting, and owned subprocesses.
- `ogent_app/api` adapts the existing localhost route contract.
- `ogent_app/compat` is an incremental-migration boundary. The short
  `ogent.py` launcher composes the extracted modules through this adapter so
  existing shell integration and endpoints remain operational.

The compatibility adapter is not a second source of truth. Canonical
conversation, run, event, coverage, document-revision, and changeset state is
stored in SQLite or content-addressed blobs.

## Workspace concurrency

One `WorkspaceActor` serializes state-changing commands for a workspace.
Provider, indexing, rendering, and attachment-preparation work runs outside the
actor and returns revision-, run-, and generation-bound results. Stale results
are rejected.

Office mutations remain single-agent and serialized per active document.
Read-only preparation may use bounded worker pools. Shutdown cancels and waits
for owned provider, document-index, and reference-index work before deleting
its storage.

## Durable data and recovery

- SQLite runs in WAL mode and owns migrations, workspaces, turns, runs, steps,
  replayable events, receipts, document revisions, indexes, coverage, and
  changesets.
- Complete turn text and large generated artifacts use SHA-256-addressed blobs.
  Display excerpts are never canonical content.
- Direct local Office edits require a physical, hash-verified recovery backup.
- Each mutating run also owns a rollback snapshot. Failed server verification
  restores that snapshot.
- Browser imports and PDF-derived DOCX files remain copy-based.

## Document execution contract

An active-document turn advances through:

```text
accepted -> capability_bootstrap -> document_refresh -> scope_resolved
         -> plan_ready -> executing -> verifying -> preview_sync
         -> completed | failed | cancelled
```

The backend—not provider prose—loads the format skill, executes an OfficeCLI
preflight, records receipts, supplies revision-aware context, audits typed
operations, and verifies the result.

An edit cannot complete without mutation evidence, targeted readback,
validation, and revision proof. A requested edit with no accepted mutation
finishes as `no_change`. A changed package that fails verification is rolled
back.

## Large-document execution

Opening a document records its package hash and quick manifest, then starts
background structural indexing. DOCX, XLSX, PPTX, and searchable PDF indexers
produce stable locators, content hashes, structural edges, FTS chunks, and
coverage categories.

Each revision compares stable lineage and content hashes with its immutable
parent. Unchanged text nodes inherit the prior content-addressed blob and FTS
materialization through their origin chain; only changed text nodes create new
search chunks. The package is still scanned to detect the delta, but unchanged
regions are not rewritten. A generated normal-edit DOCX test enforces both this
invariant and a p95 reindex budget below one second.

Provider context is a bounded projection, never the canonical document. A
whole-document request is partitioned by structural nodes and model budget.
Each successful partition is checkpointed in a content-addressed blob.
Interrupted read-only reviews can resume against the same revision and reuse
completed partitions. A bounded hierarchical reduction synthesizes the final
answer.

Visual evidence is revision-bound and lazy. Relevant charts pair a rendered
region with semantic titles, axes, categories, series, values, formulas, and
source ranges. If the provider lacks image support, rendering fails, or the
semantic payload cannot fit, visual coverage remains explicitly incomplete.

## Frontend

The React/TypeScript/Vite client preserves the two-pane Ogent workspace and
adds virtualized transcripts and document maps, context inspection, visible
plans, coverage, streamed provisional answers, change review, undo, and
interrupted-review resume.

The production bundle under `web/dist` is served locally; no external browser
assets or telemetry are required.

## Static-analysis boundary

`pyrightconfig.json` type-checks the domain, application, ports,
infrastructure, settings, and statically composed API modules. It excludes only
the late-bound compatibility fragments and the four HTTP adapter modules whose
names are injected by `bind_runtime`.

This is a deliberate transitional boundary, not a blanket diagnostic
suppression. The strict core currently passes Pyright with zero diagnostics.
The compatibility routes remain covered by route-contract, integration,
security, and browser tests. Replacing their dynamic composition with a typed
API composition root is the condition for removing those exclusions.

New application services are kept below 400 source lines by separating context
budgets, structural selection, partition artifacts, provider normalization,
workspace commands, actor lifecycle, and recovery. `run_turn` remains the one
documented application exception because it is the sole terminal saga writer.
An architecture test enforces the application-size exception and a hard
1,000-line extraction gate across all production Python sources.

## Reviewed complexity exceptions

The default production threshold is cyclomatic complexity 15. The following
functions carry narrow `C901` exceptions:

| Function | Reason | Containment and removal condition |
|---|---|---|
| `OgentHandlerFoundation._read_reference_upload` | One compatibility transaction must reserve quotas, stream bytes, register ownership, and unwind every partial failure. | Upload validation/storage helpers are independently tested. Remove the exception when the compatibility HTTP adapter is replaced. |
| `OgentGetRoutesMixin.do_GET` | Legacy route dispatch must preserve the existing endpoint contract during migration. | Route handlers delegate to extracted repositories/services and have route-contract tests. Remove with the typed API composition root. |
| `OgentPostRoutesMixin.do_POST` | Same compatibility-dispatch constraint for state-changing routes. | Authentication, actor dispatch, containment, and error mapping remain server-owned and tested. Remove with the typed API composition root. |
| `run_turn` | This is the single durable saga boundary coordinating state transitions, cleanup, rollback, and terminal recording. Splitting its exception/finally ownership would create competing terminal writers. | Provider dispatch, partition execution, reference preparation, streaming, and verification are already extracted. Keep one terminal writer; remove the exception only when a typed saga/state-machine runner can preserve that invariant. |
| `DocxIndexer._quick_body_inventory` | OOXML body traversal branches by paragraphs, tables, sections, stories, and visual types. | It is read-only, bounded by package validation, and covered with generated mixed-content fixtures. |
| `DocxIndexer._index_body` | Ordered Word semantics require branching across headings, captions, tables, bookmarks, cross-references, sections, and unsupported objects. | Branches emit typed nodes/edges and are checked by inventory, locator, delta, and malformed-package tests. |

No other production function may exceed the threshold without adding a
specific entry here and a local `C901` annotation.

## Intentional exception-suppression boundaries

Broad exceptions are never silently discarded. Cleanup and failure-projection
paths either re-raise the primary failure or record a secret-free diagnostic
containing only stable identifiers and the secondary exception type. An
architecture test rejects both `contextlib.suppress(Exception)` and
`except Exception: pass` in production sources.

Narrow suppression remains only at idempotent operating-system boundaries:
missing temporary files after atomic replacement, already-exited owned
processes, client-disconnected SSE sockets, and best-effort deletion while a
primary exception is already being re-raised. These sites catch only the
expected OS/transport exception classes; they never suppress document,
provider, database, or verification failures.

## Verification

Run from `ogent-lite`:

```powershell
python -m pytest -q
python -m ruff check ogent_app tests
python -m ruff format --check ogent_app tests
python -m ruff check ogent_app --select C901 --config "lint.mccabe.max-complexity=15"
npm --prefix web run typecheck:python
npm --prefix web run typecheck
npm --prefix web test
npm --prefix web run build
npm --prefix web run test:e2e
```

Coverage reports include all compatibility code so migration debt remains
visible. Coverage must not be raised by excluding reachable production
behavior; add tests or remove obsolete compatibility code.

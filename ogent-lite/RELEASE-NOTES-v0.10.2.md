# Ogent Lite v0.10.2 release notes

Release date: 2026-07-28

Ogent 0.10.2 makes the document workspace the conversation boundary, adds a
safe New Chat action, and replaces optimistic Word-preview success with an
exact revision/render acknowledgement.

## Highlights

- Every DOCX, XLSX, PPTX, browser import, and PDF working document owns an
  independent transcript, provider-neutral memory, attachments, selections,
  provider continuation IDs, run state, and OfficeCLI watch.
- Opening document B from A creates or focuses B's workspace. B starts clean;
  reopening A during the same backend lifetime restores only A.
- Long-running work in B no longer lets inactive A age past the reaper grace
  window. While any Ogent tab is connected, all launch-scoped workspaces remain
  available; the two-minute grace restarts after the last tab disconnects.
- Canonical Windows path handling deduplicates case, slash, dot, relative, and
  absolute aliases while keeping different same-named files separate.
- A visible **+ New chat** button opens an accessible application modal with
  Cancel initially focused, focus trapping, Escape cancellation, responsive
  controls, and focus restoration.
- Confirming New Chat clears only the active document's chat state and advances
  its conversation generation. It preserves the document, edits, recovery
  backup, direct-edit state, watch, preview position, recent paths, settings,
  and every other document workspace.
- Word run completion reports **Preview updated** only after Ogent correlates
  the validated package fingerprint, exact OfficeCLI watch event/version, the
  initiating browser client, and that viewer's post-render acknowledgement.
  Every mutation and in-place recovery acknowledgement also requires semantic
  DOM equality with a fully rendered canonical view of that exact watch generation.

## Preview recovery states

Normal mutations update in place and keep the iframe URL and semantic viewport.
If acknowledgement is missing while the watch is healthy, Ogent requests one
supported in-place full resynchronization. If the watch is dead or that
recovery fails, Ogent captures the visible semantic path, restarts the watch
once, navigates the iframe once, and restores the path with
`officecli watch goto`.

The UI may show:

- **Waiting for preview** while the exact revision is being correlated.
- **Recovering preview** during the bounded in-place or watch-restart path.
- **Preview updated** only after viewer confirmation.
- **Preview could not update. Your document was saved; retry preview or open
  Word view.** when automatic recovery cannot be confirmed.

Formatting-only changes, paragraph replacement, added/removed structures,
tables, and consecutive edits use the same handshake. A validated DOCX by
itself is not treated as proof that the browser rendered it.

## New Chat safety

The reset endpoint is bound to the authenticated session and the exact
connected browser client. It accepts no filesystem cleanup target. Only
validated Ogent-owned attachment directories can be deleted. The backend
returns `409 Conflict` while an agent, upload, PDF conversion, Word view, or
another reset is active.

Reset removes transcript, provider-neutral memory, retained and pending
attachments, submitted/current selection context, historical focus marks,
provider continuation IDs, and old run/timing summaries. Late messages and
events with the previous conversation generation are rejected, and all clients
attached to the same document workspace receive the reset broadcast.

## Compatibility and installation

- Windows 11
- Python 3
- OfficeCLI 1.0.143 or later
- A signed-in Codex CLI, Claude Code CLI, or both

OfficeCLI 1.0.143 remains the minimum because it supplies semantic viewport
anchors, public `watch goto`, and exact viewer-mark cleanup. Until the compatible
viewer change is available upstream, use the checksum-verified
[OfficeCLI 1.0.143 Ogent viewer preview prerelease](https://github.com/ljdstechva/OfficeCLI/releases/tag/v1.0.143-ogent-preview)
described in the [repository install guide](../README.md).

Update and launch:

```powershell
git pull --ff-only origin main
py -3 -m pip install -r '.\ogent-lite\requirements.txt'
Set-Location '.\ogent-lite'
py -3 .\ogent.py --register-shell
.\ogent.cmd
```

Stop the backend and only the provider/OfficeCLI processes it owns:

```powershell
.\ogent.cmd stop
```

Verify provider readiness in the UI or from the local health response:

```powershell
$health = Invoke-RestMethod 'http://127.0.0.1:8765/health'
$health.version
$health.agent_capabilities.providers |
  Select-Object id, status, installed, authenticated, cliVersion
```

The port can be 8766 or higher if 8765 was already occupied.

## Verification

The deterministic gate covers document switching and aliases, concurrent
opens, browser-import/PDF isolation, provider switching, transactional reset,
attachment cleanup boundaries, late-event rejection, exact-client
authorization, formatting/structural preview events, consecutive edits,
in-place recovery, canonical-DOM mismatch rejection, stale-generation channel
rejection, and one-restart semantic restoration. The final local run passed 167
tests and 58 subtests under Pytest and the same 167 tests under Unittest.

Real synthetic-document acceptance demonstrated:

- A/B chat, memory, attachment, and selection isolation and restoration.
- Escape, Cancel, focus cycling, and confirmation for New Chat.
- Unchanged document hash, recovery-backup hash, watch port, and watch
  generation across reset.
- Multiple real `gpt-5.6-sol` Word edits rendered automatically with a stable
  iframe and viewport. The final post-review edit reported **Preview updated**
  only after an HTTP 204 acknowledgment carried equal live and fully rendered
  canonical DOM fingerprints; scroll drift was zero.
- A stopped run followed by a successful preview-confirmed edit.
- Live formatting-only and added-table-row rendering.
- Submitted selection navigation to the changed paragraph with a centered gold
  viewer-only mark.
- Desktop 1440x900 and mobile 390x844 layout checks with no horizontal
  overflow or clipped modal controls and a clean browser console.

## Honest limits

- Chat and memory are launch-scoped; stopping Ogent intentionally removes them.
- Multiple browser tabs attached to one document workspace share its chat and
  reset broadcasts. OfficeCLI historical-focus navigation is watch-scoped, so
  those viewers move together.
- Complex Word columns, floating objects, text boxes, and embedded fonts may be
  approximate in the HTML live preview. Use **Word view** for a
  Microsoft Word-rendered verification surface.
- Automatic recovery is bounded. If it cannot be confirmed, Ogent reports the
  degraded state instead of claiming the preview updated.

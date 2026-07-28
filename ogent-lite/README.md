<p align="center">
  <img src="assets/ogent-logo.svg" alt="Ogent" width="360">
</p>

# Ogent Lite

Ogent Lite 0.10.2 is a featherweight, local document workspace: OfficeCLI keeps a
live Word, Excel, or PowerPoint preview on the left, while either Codex or
Claude Code handles plain-language editing requests in the chat pane on the
right.

It runs entirely on `127.0.0.1`, uses the selected CLI's existing sign-in, and
never asks for an OpenAI or Anthropic API key. A DOCX, XLSX, or PPTX opened by
local path is edited directly only after a physical recovery copy is created
and SHA-256 verified. Browser uploads are preserved byte-for-byte under
`%LOCALAPPDATA%\OgentLite\imports\` and edited there. PDFs are copied and
converted to working DOCX files under `%LOCALAPPDATA%\OgentLite\work\`.
Composer attachments follow a retained, read-only session lifecycle and never
become active documents.

For the AI-agent installation sentence and complete human setup, see the
[repository README](../README.md). Release details and the temporary verified
OfficeCLI fork dependency are in
[RELEASE-NOTES-v0.10.2.md](RELEASE-NOTES-v0.10.2.md).

## Start and stop

From the repository root:

```powershell
Set-Location '.\ogent-lite'
.\ogent.cmd

# Stop Ogent and its owned provider/OfficeCLI processes
.\ogent.cmd stop
```

The default URL is `http://127.0.0.1:8765/`; Ogent chooses the next available
loopback port when needed. The health response exposes no document contents and
can verify the release and installed provider CLIs:

```powershell
$health = Invoke-RestMethod 'http://127.0.0.1:8765/health'
$health.version
$health.agent_capabilities.providers |
  Select-Object id, status, installed, authenticated, cliVersion
```

Each usable provider reports `status` as `ready`. After signing in or upgrading
a CLI, use the compact Refresh button and check again.

- Drag a `.docx`, `.xlsx`, `.pptx`, or `.pdf` anywhere into the running Ogent
  window. The drop area can also be clicked to choose a file.
- Double-click `ogent.cmd` in this folder.
- Or double-click the optional **Ogent** desktop shortcut after creating it
  with the instructions in the repository README. You can drag one supported
  file onto that shortcut to launch and open it immediately.
- Or right-click a supported Office file and select **Open in Ogent** after
  registering the Explorer integration below.

If Ogent is already running, another launch creates a fresh browser workspace
inside the existing server instead of starting a second backend process.

## Right-click integration

Register the per-user Windows Explorer command from this folder:

```powershell
py -3 .\ogent.py --register-shell
```

You can then right-click any `.docx`, `.xlsx`, or `.pptx` and choose
**Show more options → Open in Ogent**. Pressing **Shift+F10** opens the same
classic menu. Windows 11 does not allow an unpackaged desktop script to appear
in the compact modern menu; that requires MSIX packaging and is intentionally
outside Ogent Lite's current scope.

The command starts Ogent when necessary or reuses the most recently focused
connected workspace. Its already-open tab updates through SSE, and Ogent also
opens the selected workspace in a predictable extra tab; that extra tab can be
closed. If the selected workspace is running an agent, Ogent leaves its document
unchanged and shows the busy message in that workspace. If that exact source is
already open elsewhere, Ogent focuses the existing session instead of starting
a duplicate OfficeCLI watch. Other independent sessions remain untouched, and
the original document remains protected by its recovery backup.

Remove the integration cleanly at any time:

```powershell
py -3 .\ogent.py --unregister-shell
```

Registration is limited to your Windows account and does not need administrator
rights. The registration asks Windows to place the verb in the **Top** cluster
of the classic menu. Windows controls ordering inside that cluster, so Ogent
cannot guarantee an exact position between two specific apps. If Explorer keeps
an older icon, run `ie4uinit.exe -show` or restart Explorer to refresh its cache.

## Daily recipe

1. Start Ogent and drag a `.docx`, `.xlsx`, `.pptx`, or `.pdf` anywhere into
   the window. You can still paste an absolute path and click **Open**.
2. Choose the agent, model, and effort reported by its installed CLI. Use the
   compact refresh button after signing in or upgrading a CLI.
3. Describe the change and review it live on the left.
4. Open another document in the same browser or use **+ New window** for
   another client. Each document owns its own chat, memory, attachments,
   selections, run state, and preview; the session dropdown switches among
   them without merging state.
5. For a complex DOCX, use **Word view** when exact floating-shape placement
   matters. It opens a Microsoft Word-rendered PDF in a new browser tab.
6. To analyze supporting material, click the paperclip or drop files on the
   composer. A composer drop retains a read-only attachment for this workspace;
   a drop elsewhere opens a browser-imported editable copy.
7. Click a preview element to create a focused target chip. Supported targets
   are Word paragraphs/table cells, PowerPoint shapes, and Excel cells/ranges.
   Multi-select retains up to 20 targets. A document revision makes old targets
   visibly stale and blocks Send.
8. After Send, each frozen selection tag under that user message is clickable.
   It centers the exact current target and adds a temporary gold viewer-only
   highlight without changing the current composer selection or Office file.
9. Use **+ New chat** when you want a clean conversation for only the active
   document. Cancel or Escape is safe; confirmation preserves the document,
   recovery backup, watch, preview position, settings, and other document
   workspaces.

The per-turn icon distinguishes **working**, **completed**, **error**, and
**stopped**. Agent Activity shows provider/model/effort, preparation and tool
phases, OfficeCLI call counts, and elapsed time. The gear opens recovery,
retention, and session-memory settings.

## Stable preview and submitted selection links

Normal completion, provider error, and Stop never reload the preview merely
because the document revision or run status changed. Ogent keys iframe
navigation to the session, logical document, watch port, and watch generation.
For Word mutations, a client-scoped relay correlates the validated package
SHA-256, exact OfficeCLI watch event/version, initiating browser client, and
that viewer's post-render acknowledgement before the UI says **Preview
updated**. Every mutation and in-place recovery acknowledgement is accepted
only when the viewer's semantic DOM equals a fully rendered canonical view of the same
document and watch generation.

If a healthy watch misses a normal acknowledgement, Ogent requests one
supported in-place full resynchronization and compares semantic content without
navigating the iframe. If the watch has died or recovery fails, Ogent captures
the first visible semantic path, restarts that watch exactly once, performs one
required iframe navigation, and asks `officecli watch goto` to restore the
location. If confirmation still fails, it reports:
`Preview could not update. Your document was saved; retry preview or open Word
view.` It never calls a validated DOCX alone proof that the browser updated.

Submitted selection cards are accessible buttons. Mouse, touch, Enter, and
Space replay each tag independently. Ogent accepts only that submitted
message's sequence and selection ID from the browser, resolves the canonical
snapshot from its own session memory, revalidates the current OfficeCLI target,
and invokes OfficeCLI with argument arrays rather than a shell. The target
finishes in the central portion of the preview with a temporary gold highlight;
current composer selection remains teal.

Historical navigation does not reload the iframe, mutate the Office package,
change composer chips or draft text, start an agent, submit a chat turn, or
alter provider-neutral memory. When an exact target no longer resolves, Ogent
allows only a unique conservative relocation. A missing or ambiguous target
fails closed with a prompt to select it again.

Each Ogent document session has its own watch, so historical focus cannot move
an unrelated document session. OfficeCLI's current navigation event is
watch-scoped rather than browser-client-scoped: if two tabs display the same
Ogent session/watch, both viewers center the clicked target. No supported
client-scoped focus channel is available yet.

Excel ranges of at most 100 cells receive cell-by-cell gold viewer marks. For a
larger range, Ogent centers and highlights only the primary top-left cell so
historical navigation cannot spawn an unbounded number of OfficeCLI commands.

For PDFs, drag the PDF into Ogent or start with “Edit my PDF,” then paste its
absolute path. Ogent copies the PDF, converts the copy to a working DOCX through
the Word-first pipeline, and opens that DOCX for editing. Complex PDF reflow may
still need layout cleanup; image-only PDFs require OCR.

## Retained read-only attachments

The chat composer accepts multiple DOCX, XLSX, PPTX, PDF, TXT, Markdown, CSV,
PNG, JPEG, WebP, BMP, and TIFF attachments. Limits are 20 files per Send,
50 MB per file, 100 MB combined per Send, 100 retained files and 500 MB per
workspace, three concurrent uploads, and 25 pages per PDF. Ogent validates file
content and signatures; it rejects empty, malformed, mismatched, executable,
archive, legacy Office, and over-limit inputs with an actionable Failed state.

Attachments may be sent with typed instructions or by pressing **Send** with an
empty message. The latter uses: `Read and analyze the attached reference files.
Summarize the important findings.` Submitted cards stay in the chat and remain
available in this workspace. New attachments belong to the next Send. A later
request can name one or more retained filenames; only those named files and the
new batch are materialized for the provider, subject to per-Send limits.

OfficeCLI performs only read operations on Office attachments. Searchable PDF
text is extracted with page headings. Scanned or low-text PDF pages, supported
images, and visually requested Office content are normalized into bounded
temporary images and supplied only to the selected provider for that turn.
Ogent does not use an external OCR service.

Canonical retained copies live in launch-scoped session memory. Every provider
turn gets isolated materialized copies, extraction, rendered pages, and
manifests under `%LOCALAPPDATA%\OgentLite\temporary-references\`. Ogent deletes
that run directory after success, provider error, Stop, or preparation failure.
**Forget** removes one canonical attachment. **+ New chat** (and Settings'
**Start a new chat**) clears all retained files, transcript turns, memory,
pending attachments, and selection context in only that document workspace.
Session reaping, shutdown, and startup clear launch-scoped memory after owned
processes release the files.

This is ordinary best-effort local deletion, not secure forensic erasure on
NTFS, SSDs, backups, antivirus caches, or synchronized storage. Attachment
contents are sent to the selected AI provider only for requested turns. The
provider's own data-handling and retention policy still applies.

## Direct edits and recovery

Opening DOCX/XLSX/PPTX through an absolute local path, Explorer, or the desktop
shortcut edits that exact file. Before the first mutation, Ogent creates a
byte-for-byte physical copy under `%LOCALAPPDATA%\OgentLite\backups\`, verifies
its size and SHA-256 digest, and records a manifest. It never uses a hardlink.
Browser uploads are edited under `imports`; PDF-derived DOCX files are edited
under `work`; neither mode overwrites the user's browser source or PDF.

A backup expires exactly 30 x 24 hours after creation and is removed by the
first startup, scheduled, or manual cleanup at or after that instant. The gear
shows backup count and size, opens the recovery folder, runs expired cleanup,
and starts a new chat for the current document. To restore manually: stop
Ogent, copy the chosen backup over the original, reopen it, and validate it with
OfficeCLI. Deletion is best-effort, not forensic erasure.

## Sessions and automatic cleanup

One Ogent session is one document workspace, not merely one browser tab. The
workspace owns the stable document identity, transcript, provider-neutral
memory, retained/pending attachments, submitted/current selections, provider
continuation IDs, conversation generation, run state, recovery metadata, and
OfficeCLI watch port from 26320-26380.

An empty browser session may bind to its first document. Opening a different
document creates or focuses that document's workspace and navigates directly
to it, so the previous transcript is never rendered under the new filename.
Reopening a document during the same backend lifetime restores only its own
launch-scoped state. Windows case, separator, dot, relative/absolute, and
supported path aliases deduplicate; distinct files with the same basename do
not. Browser imports and PDF working documents follow the same isolation.

Different document workspaces may edit concurrently; one workspace allows one
agent run at a time. Each turn starts a fresh provider process, while Ogent
supplies only that document's conversation delta, attachment metadata,
identity, and submitted selection. Tabs that navigate to the same deduplicated
session share that workspace and receive the same reset/update broadcasts.

The visible **+ New chat** button opens an accessible application modal with
Cancel initially focused, trapped keyboard focus, Escape cancellation, and
focus restoration. Confirmation atomically removes only the active workspace's
conversation state and increments its generation. Late provider/SSE/browser
events carrying the old generation are ignored. The document, edits, backup,
direct-edit mode, watch, preview position, recent path, settings, and all other
workspaces remain intact. The backend independently rejects reset while a run,
upload, PDF conversion, Word view, or another reset is active.

Closing the final connected tab starts a 120-second grace window; closing one of
several tabs attached to the same session does not. Refreshing or reopening the
same session URL reconnects it. An active run is never reaped; after the run
finishes the session receives a fresh grace window so its result can be
collected.

When all sessions have been reaped, the backend exits after 10 minutes. Override
that delay when launching Python directly:

```powershell
# Keep the backend resident
py -3 .\ogent.py --idle-exit-minutes 0

# Exit 30 minutes after the final session is gone
py -3 .\ogent.py --idle-exit-minutes 30
```

`ogent.cmd stop` shuts down every session immediately. If Word view is active,
Ogent gives the Word converter a bounded clean-exit window and tracks its exact
automation process for forced cleanup if that window expires.

## Local data

| Item | Location |
|---|---|
| Recent paths | `%LOCALAPPDATA%\OgentLite\recent.json` |
| Browser drag/drop imports | `%LOCALAPPDATA%\OgentLite\imports\` |
| PDF working copies | `%LOCALAPPDATA%\OgentLite\work\` |
| Direct-edit recovery backups | `%LOCALAPPDATA%\OgentLite\backups\` |
| Launch-scoped session memory and canonical attachments | `%LOCALAPPDATA%\OgentLite\session-memory\` |
| Per-run attachment copies | `%LOCALAPPDATA%\OgentLite\temporary-references\` |
| Agent capability cache | `%LOCALAPPDATA%\OgentLite\agent-capabilities-v1.json` |
| Running-server record | `%LOCALAPPDATA%\OgentLite\server.json` |

Recent paths and working documents stay local and are excluded from Git.
Composer attachments are not written to recents or the active-document index.

## Requirements

- Windows 11 with Python 3 (`py -3 --version`)
- OfficeCLI 1.0.143 or later (`officecli --version`)
- At least one supported agent CLI, installed and signed in:
  - Codex: `codex --version` and `codex login status`
  - Claude Code: `claude --version` and `claude auth status --json`
- Pinned Python packages:

  ```powershell
  py -3 -m pip install -r '.\requirements.txt'
  ```

  The tested pins are pypdfium2 5.12.1 for PDF inspection/rendering and
  Pillow 12.1.1 for image validation/normalization.

OfficeCLI 1.0.143 supplies the semantic viewport-preservation and public
`watch goto`/exact-mark cleanup commands required by this release. Ogent checks
the installed version before starting a watch and reports an actionable error
instead of silently running with an incompatible viewer.

Until the compatible change is available upstream, use the checksum-verified
[OfficeCLI 1.0.143 Ogent viewer preview fork prerelease](https://github.com/ljdstechva/OfficeCLI/releases/tag/v1.0.143-ogent-preview)
documented in the [repository install steps](../README.md#option-2--human-install-on-windows).
The clean patch is proposed in [upstream PR #268](https://github.com/iOfficeAI/OfficeCLI/pull/268).

## Agents, models, and effort

Ogent has no built-in model or effort catalog. On startup and when **Refresh**
is clicked, it asks the installed CLI directly:

- Codex models and each model's effort choices come from Codex App Server
  `model/list`, with `codex debug models` as a dynamic fallback.
- Claude model aliases come from the local, zero-inference `/model` result.
  Startup effort choices come from `claude --help`; support is then verified
  lazily for the selected model with zero-token `/model` probes. If a future CLI
  cannot perform that local model-specific check, Ogent may show only the
  globally CLI-valid choices with an explicit **model-specific support
  unverified** status.

Choices can differ by account, organization policy, provider, and CLI version.
**Automatic — CLI default** omits the effort override. A cached catalog is
shown only to keep the interface understandable while refreshing; `stale`
information is never accepted for a new run. Use **Refresh** after signing in,
changing accounts, changing policy, or upgrading a CLI.

The public repository's **Ogent CI** workflow runs the deterministic test,
compile, Ruff, and whitespace checks on Windows for every pull request to
`main` and every push to `main`. The live-provider scripts remain separate
because CI never requires a Codex or Claude login and never consumes inference.

Every run is fresh at the provider layer:

- Codex uses `--ephemeral --ignore-user-config --ignore-rules`, workspace-write
  sandboxing, no interactive approvals, and one explicit MCP gateway restricted
  to the active document. Backups and other documents are rejected.
- Claude uses `--setting-sources "" --strict-mcp-config
  --no-session-persistence`, no permission bypass, and only the explicit
  document gateway and/or read-only materialized attachment access required for
  that turn.

Ogent's own provider-neutral session memory carries the relevant prior turns,
active document identity, retained-attachment metadata, and submitted
selection. Switching provider or model therefore does not resume or expose a
provider-owned session.

## Brand assets

The Ogent identity is built from the **Quiet Signal** mark: a navy
`#17324d` to teal `#0d9488` field, a white continuity ring, and a live-document
dot in `#14b8a6`.

| Asset | Purpose |
|---|---|
| `assets/ogent-mark.svg` | Font-independent master mark |
| `assets/ogent-logo.svg` | Mark and Ogent wordmark for documentation |
| `assets/png/ogent-*.png` | Seven rendered icon sizes from 16–256 px |
| `assets/ogent.ico` | Multi-size Windows app, shortcut, and context-menu icon |
| `assets/render-icon.html` | Dependency-free Edge rendering surface |
| `assets/make_ico.py` | Standard-library ICO assembler |

## Troubleshooting

| Symptom | What to do |
|---|---|
| Preferred port 8765 is busy | Ogent automatically tries 8766 and higher. Launch again and use the browser page it opens. |
| Preview says reconnecting | Click the reload icon. Ogent also restarts the OfficeCLI watch before the next chat run. |
| Preview says waiting or recovering | Keep the tab open while Ogent correlates the saved package with the exact watch event and viewer acknowledgement. It first attempts an in-place resynchronization. |
| Preview says it could not update | The document was saved but the browser could not confirm it. Retry the preview or use **Word view**; do not treat the stale view as authoritative. |
| Ogent says OfficeCLI 1.0.143 is required | Install a compatible upstream 1.0.143-or-later release or the checksum-verified temporary fork prerelease linked above, verify `officecli --version`, then restart Ogent. |
| A submitted selection says it moved or was removed | Select the current document content again. Ogent rejects missing, cross-document, and ambiguous historical targets instead of guessing. |
| Codex is not logged in | Run `codex login`, then click the agent refresh button. |
| Claude Code is not logged in | Run `claude auth login`, then click the agent refresh button. |
| Models are unavailable | Confirm the selected CLI's version and auth-status commands work, then refresh. Choices are account- and CLI-specific. |
| Cached or stale model status appears | Ogent is refreshing the matching executable/version. Wait for a successful live result; stale choices cannot start a run. |
| No preview port is available | Ogent allocates one port per session from 26320-26380. Close unused sessions or stale manual OfficeCLI watches, then retry. |
| A closed tab still appears briefly | The 120-second grace absorbs refreshes and accidental closes. Reopen its `/?s=<id>` URL to reconnect or let it reap automatically. |
| Complex DOCX preview looks incomplete | The live HTML view approximates some floating shapes. Click **Word view** and verify before concluding content is missing. |
| PDF opens with broken spacing | PDF Reflow preserved editable content but not exact layout; clean up the working DOCX or use the original source document. |
| PDF reports that OCR is needed | The PDF is image-only. Run OCR first, then import the searchable PDF. |
| An attachment says Failed | Check the extension and file content, then confirm the 50 MB/file, 20-file/100-MB Send, 100-file/500-MB workspace, three-concurrent-upload, and 25-page PDF limits. Rejected uploads are removed. |
| Reference PDF/image preparation is unavailable | Run `py -3 -m pip install -r .\requirements.txt`, restart Ogent, and attach the file again. |
| A visual Office reference cannot render | Text extraction can still work. Microsoft Office or LibreOffice is required for the optional temporary visual export; attach a PDF export if exact visual analysis is essential. |
| Files remain after an abnormal crash | Restart Ogent once. Startup clears abandoned run copies and launch-scoped session memory before accepting sessions. |
| A run is taking too long | Click **Stop**; Ogent terminates the active provider child process tree. |

## Privacy and limits

- Localhost only; no telemetry or external web assets.
- Local-path Office files are edited directly only after a verified physical
  recovery backup. Browser imports and PDFs remain copy-based.
- Composer attachments never become active documents, watches, recents, dedupe
  entries, or final outputs. Their browser metadata contains no temporary path.
- Attachment contents and the provider-neutral conversation context needed for
  a turn are sent to the selected provider in a fresh run. Local deletion does
  not control copies retained under that provider's own data-handling policy.
- Browser drag/drop accepts one DOCX, XLSX, PPTX, or PDF at a time, up to
  128 MB, and retains a local import copy until the user removes it.
- Composer attachments accept up to 20 supported files per Send, 50 MB each,
  100 MB combined, 100 files/500 MB retained per workspace, three concurrent
  uploads, and 25 pages per PDF.
- One active document and one agent run per document workspace; provider
  processes are fresh per turn and documents remain isolated. Chat/memory lasts
  only for the current backend lifetime.
- Focused selection supports Excel cells/ranges, Word paragraphs/table cells,
  and PowerPoint shapes. Unsupported or stale paths fail closed.
- Submitted historical focus is viewer-only. It uses one Ogent-owned gold mark,
  preserves unrelated/current selection marks, and never writes highlight
  formatting into the Office package.
- Recovery backups expire at the first cleanup at or after exactly 30 days.
- PDF editing happens in a converted DOCX, never in the PDF itself.
- Word view currently supports DOCX only and requires Microsoft Word.

# Ogent Lite Verification Report

Date: 2026-07-27
Status: v0.9.0 automated and live provider acceptance passed; earlier release
matrices are retained below as historical evidence.

## Current runtime and architecture

- Runtime: system Python 3.14.3
- Application: `ogent.py` with embedded HTML/CSS/JavaScript plus separate
  provider and capability-catalog adapters
- Dependencies: Python standard library, plus the existing pinned reference
  inspection packages
- Server bind: `127.0.0.1` only
- Preferred app port: 8765, with automatic upward fallback
- OfficeCLI watch ports: one per session, allocated from 26320-26380
- Agent backends observed live: Codex CLI 0.145.0 and Claude Code 2.1.220
- Model and effort source: the installed, authenticated provider CLI; Ogent has
  no static provider model or effort catalog
- OfficeCLI: 1.0.142

The v0.9.0 acceptance evidence appears in the final section. Statements in the
v0.1.0 through v0.8.0 sections describe those historical releases and are not
the current catalog or session architecture.

## Live test matrix

| Test | Result | Time | Evidence |
|---|---|---:|---|
| T1 — Word title dark blue and bold | PASS | 59.132 s | Live pane updated; readback showed `#1F4E79`, `bold=true`; validation passed; source hash unchanged. |
| T2 — follow-up revert to black, not bold | PASS | 27.499 s | Same Codex context retained; readback showed `#000000`, `bold=false`; validation passed. This run also followed a forced watch-process kill. |
| T3 — Excel TOTAL with SUM | PASS | 59.888 s | Existing styled total row was correctly reused rather than duplicated; `D7=SUM(D2:D6)`, cached value 362400; zero formula-error matches; validation passed. |
| T4 — PowerPoint closing slide | PASS | 111.141 s | Quality rerun produced a native dark network-themed slide with 13 elements, speaker notes, and slide number 06; zero issues; validation passed. |
| T5 — PDF guided flow | PASS | 12.390 s after path | No-document chat asked for the absolute path; a protected PDF copy was converted through the Word-first pipeline; the four-message guidance history remained; searchable DOCX opened and validated. |
| T6 — idle footprint | PASS | measured | See memory comparison below. |
| T7 — user-driven real edit | PASS | user-driven | User requested spacing between real CV job descriptions and year ranges, reviewed the live result, and reported that it was good overall. |

The first T4 attempt completed in 56.665 seconds but used a plain default slide.
It was rejected during visual QA. Ogent's agent brief was strengthened to match
the existing document's visual system, and the design-matched rerun above passed.

## Layout and browser verification

- Initial document pane: 68.0% of a 1440 px viewport
- Chat pane: 31.5%; splitter: 7 px
- Drag test moved the document pane to 59.7%, proving the splitter is active
- Final home page: zero browser-console errors
- Model and reasoning selectors fit at 1280 × 720 with no horizontal or vertical
  page overflow
- Selector choices persist across reload; unsupported server values return HTTP
  400 without adding a transcript message or starting a run
- Word, Excel, PowerPoint, and converted DOCX previews all loaded through the
  owned OfficeCLI watch
- PowerPoint closing slide was scrolled into view and visually inspected live

## Launch experience

- Desktop shortcut: `%USERPROFILE%\Desktop\Ogent.lnk`
- Batch launcher: `ogent-lite\ogent.cmd`
- PowerShell commands: `ogent` and `ogent stop`
- Shortcut cold start: 3.946 seconds from Python launcher creation to bound,
  healthy server
- PowerShell start dispatch: 200 ms
- Second-instance test: reused the existing server PID; authoritative server
  count remained one
- Preferred-port-busy test: bound to `127.0.0.1:8766` when 8765 was occupied

## Resilience and process control

- Manually killing the OfficeCLI watch caused the next chat run to create a new
  watch and complete successfully.
- A restart race between UI repair and the pre-run health check was found,
  serialized with a dedicated watch lock, and retested.
- Retest produced no HTTP 500; only expected transient connection-refused/reset
  browser messages occurred while the intentionally killed port was offline.
- Stop button terminated the active Codex run in 351 ms and left zero Codex-exec
  descendants.
- A second request during an active run was rejected with HTTP 409 and a clear
  “still working” message.
- Moving a working copy now clears the browser document state immediately and
  gives one actionable error.
- `ogent stop` left no server-owned process and released the legacy
  single-session watch port.

## Memory comparison

Measured with a document open and no Codex run active:

| Runtime | Processes counted | Working set | Private memory |
|---|---:|---:|---:|
| Ogent server + OfficeCLI watch + console host | 3 | 100.3 MB | 45.0 MB |
| Ogent including Python launcher | 4 | 115.1 MB | 49.5 MB |
| AionUi comparison | 7 | 588.3 MB | 1,022.9 MB |

Ogent used about 80% less working-set memory than the running AionUi instance in
this measurement.

## Preservation and safety checks

- Every Office test used a protected copy under
  `%LOCALAPPDATA%\OgentLite\work\`.
- Tracked Word, Excel, and PowerPoint fixture hashes remained unchanged.
- The private PDF's pre/post hash matched; neither it nor its working DOCX is in
  the repository.
- Recent paths live only in `%LOCALAPPDATA%\OgentLite\recent.json`.
- Runtime state, working documents, compiled Python, logs, and browser artifacts
  are ignored by Git.
- Mutating HTTP endpoints reject requests without the per-instance local token
  (verified HTTP 403).
- No telemetry, CDN, external font, framework, or new API-key dependency exists.

## Known limitations

- One Codex run at a time per session; different sessions can run concurrently.
- Excel watch does not support click-to-select paths.
- PDF editing occurs in a converted working DOCX, never directly in the PDF.
- PDF Reflow can preserve editable text while changing complex layout; the
  working DOCX may need cleanup.
- Image-only PDFs need OCR before import.
- PowerPoint edits that faithfully reproduce a custom visual system can take
  longer than simple text or formatting changes.

## Daily recipe

1. Run `ogent`, paste the absolute Office-document path, and click **Open**.
2. Describe the change in chat and review it live on the left; run `ogent stop` when finished.

## v0.4.0 - shell integration + brand

![Approved Ogent mark](assets/png/ogent-256.png)

### Approved identity

- Badge: 240 x 240 at `(8, 8)`, corner radius 56
- Gradient: navy `#17324d` to teal `#0d9488`
- White ring: center `(128, 120)`, radius 66, stroke 30
- Live dot: center `(175, 167)`, radius 16, fill `#14b8a6`, white stroke 3
- Master assets: `ogent-mark.svg` and `ogent-logo.svg`
- Runtime assets: 16, 24, 32, 48, 64, 128, and 256 px PNGs plus a
  seven-frame `ogent.ico`
- Applied to the browser favicon, live-document toolbar, empty-document state,
  Windows shell verb, Desktop shortcut, and README

The mark was rendered with Microsoft Edge headless and assembled into the ICO by
the standard-library-only `make_ico.py`. Every PNG reported its intended pixel
size, the ICO directory contained all seven PNG frames, and
`System.Drawing.Icon` loaded the final file successfully.

### Shell test matrix

| Test | Result | Live evidence |
|---|---|---|
| S1 - Word right-click | PASS | Windows 11 classic menu displayed **Open in Ogent** with the approved icon. It opened `S1 Word.docx` as a protected working copy through `pythonw.exe`; no console window appeared. |
| S2 - Excel and PowerPoint right-click | PASS | Both entries appeared with the icon and switched the live preview to protected `.xlsx` and `.pptx` working copies. |
| S3 - warm switch | PASS | Existing-server Word, Excel, and PowerPoint switches completed in 2.63-2.89 seconds. The already-open tab updated through SSE; the documented extra tab also opened. |
| S4 - active Codex run | PASS | The exact registered `pythonw --open` command was invoked while GPT-5.6 Sol was working. `/open` returned HTTP 409, the source stayed on `S1 Word.docx`, and the transcript showed `Ogent is still working. Stop that run or wait for it to finish.` |
| S5 - spaces and accents | PASS | Explorer opened `résumé test file.docx` with both accents preserved in the source path, proving quoted `%1` and Unicode handling. |
| S6 - PDF negative control | PASS | The PDF classic menu contained no Ogent entry. Direct `--open` PDF routing was separately exercised: the source PDF remained unchanged and its converted working DOCX opened live. |
| S7 - reversible uninstall | PASS | `--unregister-shell` removed all six verb/command keys and a refresh showed all three base keys absent. `--register-shell` then restored exact label, icon, and command values and was left enabled. |
| S8 - regressions | PASS | Paste-path open, GPT-5.6 Sol chat edit, live preview refresh, Stop, model/reasoning selectors, favicon, and toolbar mark all passed. The edit changed Heading 1 to `1. Project Context — OGENT VERIFIED` and OfficeCLI validation returned no errors. The Browse picker was not part of the v0.2.0 baseline, so that sub-check was not applicable. |

### Additional live checks

- Cold Explorer launch started Ogent v0.4.0 on port 8765, opened the requested
  Word file, and started a healthy OfficeCLI watch without a console flash.
- PDF `--open` returned `action=pdf_import`; an immediate Office open was
  rejected with HTTP 409; the conversion then completed with a searchable,
  editable working DOCX.
- The controlled browser inspection found one visible toolbar brand mark with
  inline SVG, an inline SVG favicon, a connected live preview, GPT-5.6 Sol plus
  all six reasoning choices, and zero browser console messages.
- Stop changed the active run to `stopped`, restored Send, disabled Stop, and
  added `Stopped. No further agent work is running.` to the transcript.
- SHA-256 comparisons confirmed that the Word, Unicode Word, Excel,
  PowerPoint, and PDF source fixtures were byte-for-byte unchanged after all
  live tests.
- The Desktop shortcut targets `ogent.cmd`, uses the Ogent working directory,
  and references `assets\ogent.ico,0`.

### Issues found and resolved

- A first watch-switch implementation performed a redundant readiness probe and
  missed the three-second warm-switch goal. The watch lifecycle was tightened
  to trust OfficeCLI's ready marker and terminate owned watch processes
  directly; all three formats then passed under three seconds.
- Windows 11 exposes classic registered verbs under **Show more options**, not
  in the compact menu without MSIX packaging. The implementation and README
  state this accurately.
- The PDF and Codex busy paths were both exercised so a shell launch cannot
  replace the document during an active operation.

Final state: Ogent v0.4.0 is running, the per-user shell integration is enabled
for `.docx`, `.xlsx`, and `.pptx`, and no public push was performed.

## v0.5.0 - multi-session, lifecycle, and Word view

Verified on 2026-07-24 with Python 3.14.3, Codex CLI 0.144.1,
OfficeCLI 1.0.141, Microsoft Word, and installed Microsoft Edge. This section
supersedes the v0.4.0 single-session architecture notes above while preserving
the earlier test history.

### Architecture delivered

- One localhost server owns a registry of independent workspace sessions. A
  fresh browser workspace creates a session; reconnecting or deduplicating can
  attach more than one tab to that same session.
- Each session owns one protected working document, transcript, Codex context,
  run state, event stream, and OfficeCLI watch port from 26320-26380.
- Different sessions can edit different files concurrently. One session still
  allows only one Codex run at a time.
- Same-source opens are atomically deduplicated to the existing session.
- Stable browser-client IDs make SSE disconnects and close beacons idempotent,
  including two tabs viewing one deduplicated session.
- An idle disconnected session reaps after 120 seconds. A session that is still
  opening, generating Word view, or running Codex is protected and receives a
  fresh grace window when the operation finishes.
- The backend exits 10 minutes after the final session is gone by default.
  `--idle-exit-minutes 0` keeps it resident.
- DOCX sessions have an on-demand **Word view** that calls
  `tools\docx2pdf.ps1 -Engine Word -Force` and streams the resulting PDF. A
  session-owned PID sidecar identifies the exact Word automation process for
  safe forced cleanup without touching pre-existing Word windows.
- Explorer registration writes `Position=Top` for DOCX, XLSX, and PPTX.

### M1-M12 live matrix

| Test | Result | Observed time | Evidence |
|---|---|---:|---|
| M1 - three sessions | PASS | 4.5 s concurrent open | Alpha, Beta, and Gamma used ports 26321, 26320, and 26322. Edge/Playwright captures showed the correct unique text in each iframe, all three items in every switcher, and no console errors. |
| M2 - concurrent edits | PASS | 60.64 s to both terminal events | Two `gpt-5.6-sol` medium-reasoning runs finished independently. `OGENT_ALPHA_PARALLEL_20260724` appeared only in Alpha and `OGENT_BETA_PARALLEL_20260724` only in Beta; both working DOCX files validated and both source hashes were unchanged. |
| M3 - same-file dedupe | PASS | 0.6 s warm dispatch | A second Alpha open returned `focus_session` with the original session id and port. Transient CLI-created sessions are now removed immediately; health remained at one Alpha session. |
| M4 - close one tab | PASS | 3.91 s with a 3 s test grace | The closed test session disappeared only after its OfficeCLI process stopped; a socket probe then returned connection refused. The other session and preview stayed live. |
| M5 - close during run | PASS | 69.0 s test | The orphaned session survived beyond grace while Codex worked, completed successfully, reopened with its finished transcript, then reaped only after that reopened client closed. |
| M6 - close all / self-exit | PASS | 62.68 s with `--idle-exit-minutes 1` | Backend exited cleanly after the one-minute empty window. Port 8765 and all watch ports were closed, stderr was empty, and zero Ogent-owned OfficeCLI/Codex processes remained. |
| M7 - cold right-click | PASS | 4.3 s | `--open` started v0.5.0, created the first session before serving, opened Alpha as a protected copy, and started a healthy watch. |
| M8 - warm right-click | PASS | 4.12 s | A second file created a second live session beside the first with a distinct port. |
| M9 - `00 FS.docx` Word view | PASS | 12.48 s open; 25.46 s snapshot | Complex-layout detection found 107 textboxes, 284 shapes, and 14 drawings. The endpoint returned a valid 1,596,187-byte, 38-page searchable PDF. Visual comparison matched Word's cover: logo, title, plant image, icon list, labels, and geometry were intact. |
| M10 - menu position | PASS | <1 s registry check | `reg query` confirmed `Position REG_SZ Top` under all three per-user Ogent shell keys. Windows still controls ordering among Top entries. |
| M11 - regressions | PASS | mixed | Native Browse returned an existing DOCX path; paste-path open passed; PDF conversion completed in 8.72 s with unchanged source hash; Stop reached `stopped` in 0.41 s with no Codex child; model/reasoning controls, favicon, branding, and `ogent.cmd stop` passed. |
| M12 - idle footprint | PASS | measured | Three sessions: server 36.5 MB working set, three OfficeCLI watches 174.5 MB, 211.0 MB combined. |

The duplicate-tab extension to M3/M4 also passed: one session had two stable SSE
client IDs; closing one and waiting beyond grace left one client and a healthy
preview. Closing the second client started normal reap and backend-idle timing.

### Fidelity baseline and renderer decision

- The protected local copy of `00 FS.docx` reported 296 paragraphs, 6,818
  words, 50,364 characters, and 114 OfficeCLI issues (112 generic first-line
  indent warnings plus two consecutive-space warnings).
- OfficeCLI was upgraded from 1.0.140 to 1.0.141 through its official installer.
- The 1.0.141 HTML renderer materially improved the designed cover and did not
  justify destructive reconstruction. Its structured query still identified
  107 textboxes, 284 shapes, 14 drawings, and 10 pictures.
- Microsoft Word remains the canonical renderer for exact floating-object
  placement. The new Word view matched the native Word baseline at the same
  page aspect and visual geometry.
- The Codex agent brief now warns that HTML preview alone cannot prove floating
  content is missing and requires OfficeCLI get/query verification before any
  restore, delete, or rebuild action.

### Defects found and resolved during the matrix

- Reaper could race a long document open; `opening_source` now protects it.
- A tab closed during a long run could consume its grace before completion;
  terminal operations now start a fresh reconnect grace.
- Deduplicated CLI opens left empty transient sessions; those are now removed
  immediately, including failed new-session opens.
- Two tabs sharing one session could race beacon/disconnect ordering; stable
  client IDs now make presence removal idempotent.
- Registry removal could be broadcast before a child watch released its socket;
  cleanup and registry visibility are now atomic.
- Graceful OfficeCLI termination could leave its listener accepting briefly;
  Ogent now terminates the complete tree, issues `officecli unwatch` and
  `officecli close`, and waits for bounded port closure before announcing
  removal.
- Native Browse launched without a reliable foreground owner from a hidden
  backend; it now uses an explicit topmost WinForms owner.
- Expected SSE connection resets printed a Python traceback during self-exit;
  the server now suppresses only expected broken-pipe/reset disconnects.
- Simultaneous different-file opens in one session could interleave; a
  per-session opening claim now rejects the second request with HTTP 409.
- A failed PDF copy could strand a claimed session, and Stop during late PDF
  preparation could be overwritten; all preparation now runs inside guaranteed
  cleanup and preserves cancellation until the worker finishes.
- A vanished working file could leave stale dedupe keys; clearing a broken
  document now releases every registry key before reopening.
- Shutdown could race PDF, Browse, reaper, or Word-view startup. Process
  registration and close completion are now serialized, Browse is tracked, and
  shutdown waits for in-progress Word export to release COM cleanly.
- Forced cancellation of the Word converter could leave a hidden
  `WINWORD.EXE /Automation -Embedding` instance. The converter now records its
  exact PID, removes the sidecar after a clean quit, and Ogent validates and
  terminates only that tracked automation process on forced fallback.

### Post-review hardening evidence

- Two concurrent `/open` requests for different DOCX files in one session
  returned HTTP 200 and 409. The winner alone owned port 26320, its watch was
  healthy, and both source hashes were unchanged.
- After deliberately moving a disposable working copy away, `/watch/restart`
  returned 404, health showed no active/source document, and reopening the same
  source returned `document_opened` with a healthy watch rather than stale
  dedupe.
- While Word view was active, a competing `/open` returned 409 and the snapshot
  completed with HTTP 200. SSE header-reset cleanup, overlapping same-client
  streams, shutdown/create exclusion, concurrent close serialization, failed
  PDF-copy cleanup, and PDF stop-state preservation also passed targeted tests.
- An intentional shutdown during Word view left port 8765 and the session watch
  port closed, no session-owned OfficeCLI process, no Word process, no PID
  sidecar, no server record, and an empty stderr log.
- The forced-cleanup fallback was exercised with a disposable automation
  instance: it terminated both the PowerShell wrapper and its exact tracked
  Word PID. A standalone Word conversion then produced a valid 52,680-byte PDF
  in 4.99 seconds, removed its PID sidecar, left no new Word process, and
  preserved the DOCX source hash.

### Honest remaining limits

- Word view is DOCX-only and requires desktop Microsoft Word.
- The live HTML view remains an approximation for some floating Word objects;
  use Word view before judging designed pages.
- Windows exposes only Top/Bottom placement for classic verbs. Ogent cannot
  dictate exact ordering among other Top-positioned entries.
- Excel live preview still does not support click-to-select paths.
- PDF editing still happens through a protected converted DOCX, and image-only
  PDFs still require OCR.

Final v0.5.0 state: implementation and documentation are locally committed,
the per-user shell integration is enabled, the backend is stopped after the
clean self-exit test, personal/source-derived documents remain untracked, and
no public push was performed.

## v0.6.0 - focused shell open and seamless preview handoff

The user approved the existing Quiet Signal mark on 2026-07-26. Its approved
parameters remain the 240 x 240 rounded badge at `(8, 8)` with radius 56, navy
`#17324d` to teal `#0d9488`, a white ring centered at `(128, 120)` with radius
66 and stroke 30, and the `#14b8a6` live dot at `(175, 167)` with radius 16 and
white stroke 3. Revalidation confirmed all seven PNG dimensions and a
Windows-loadable 44,813-byte ICO.

### Shell behavior restored on the v0.5 session architecture

- Explorer opens now select the most recently focused connected workspace
  rather than silently creating a new session. Browser focus is recorded by
  `POST /session/focus`; the initial SSE connection also establishes activity.
- The selected workspace's SSE stream receives the document switch, while the
  CLI still opens the predictable extra tab required by the shell contract.
- A busy selected workspace returns HTTP 409, records the exact message in that
  workspace, and returns its session id so the extra tab opens on the visible
  error instead of an unrelated new workspace.
- Independent sessions created through **+ New window** or a normal second
  launch remain intact. Same-source dedupe still focuses the owning session.
- Warm switching now starts the replacement OfficeCLI watch on a new port,
  publishes it when ready, and retires the previous watch in the background.
  DOCX complex-layout inspection runs concurrently with watch startup.
- Ogent-owned OfficeCLI work now uses the workspace's validated direct mode,
  `OFFICECLI_NO_AUTO_RESIDENT=1`, with per-mutation flushing retained.

### v0.6 live evidence

| Check | Result |
|---|---|
| Cold `--open` | A clean backend started v0.6.0 on port 8900, opened the Word fixture in session `a2a0c28c`, launched a healthy protected watch, and emitted the exact browser session URL. |
| Warm Word | PASS in 2.919 seconds; the existing Playwright tab updated through SSE and displayed the complete Word fixture. |
| Warm Excel | PASS in 2.976 seconds. |
| Warm PowerPoint | PASS in 2.839 seconds. |
| Unicode path | `résumé test file.docx` opened in 2.907 seconds; the source name remained Unicode in session state and the live preview rendered correctly. |
| Busy guard | A real GPT-5.6 Sol read-only run was started, an immediate shell open returned exit 1 / HTTP 409, the document remained unchanged, the exact busy message appeared in the live transcript, and Stop ended the run. |
| PDF direct open | Returned `pdf_import`, completed via SSE without polling, produced a validated searchable working DOCX with a healthy watch, and preserved the PDF hash. |
| Same-source dedupe | A duplicate PDF open focused the existing session and left one session plus one isolated OfficeCLI watch. |
| Source preservation | SHA-256 hashes for Word, Unicode Word, Excel, PowerPoint, and PDF fixtures were identical before and after the matrix. |
| Browser brand | Inline SVG favicon present; toolbar mark measured 28 x 28; empty-state SVG present; zero browser console errors or warnings. |
| Shell registry | Unregister removed all six verb/command keys; absence was verified; register restored exact label, icon, `pythonw.exe` command, and quoted `%1` for `.docx`, `.xlsx`, and `.pptx`; `.pdf` remained absent; Explorer icon cache was refreshed. |
| Protocol regressions | Six stdlib unit tests passed: connected-focus selection, resident-backend session creation, replacement-port reservation, direct-mode environment, busy 409 transcript/session targeting, and warm workspace reuse. Python compilation, Ruff, and `git diff --check` also passed. |

The final Windows Explorer gesture checks for v0.6.0 remain human-operated:
confirm the icon and no-console behavior for one Word, Excel, and PowerPoint
right-click. The same registered `pythonw.exe` command and icon passed the
earlier v0.4.0 human matrix; the v0.6 command path and all downstream behavior
have been revalidated above.

## v0.7.0 - drag-and-drop reference workflow

Verified on 2026-07-26 with Python 3.14.3, OfficeCLI 1.0.141, Microsoft Word,
Chrome, and Playwright CLI. This release makes drag-and-drop the primary
document-entry workflow while retaining the v0.6 right-click behavior.

### Behavior delivered

- A file can be dropped on the visible drop target or anywhere in the Ogent
  browser window. Clicking the target opens the native file chooser.
- DOCX, XLSX, PPTX, and PDF are accepted one at a time up to 128 MB.
- Browser security does not expose the original Windows path. Ogent therefore
  preserves the exact received bytes under
  `%LOCALAPPDATA%\OgentLite\imports\`, then creates the independently editable
  working copy under `%LOCALAPPDATA%\OgentLite\work\`.
- The full-window drop overlay explains the action and source-file protection.
  Busy runs, snapshots, and uploads disable competing document opens.
- Dropping a supported file onto the Ogent desktop shortcut now forwards that
  file to `ogent.py --open`; normal double-click launch and `ogent.cmd stop`
  remain unchanged.
- Right-click **Open in Ogent** remains enabled for DOCX, XLSX, and PPTX.

### v0.7 acceptance evidence

| Check | Result |
|---|---|
| Browser chooser | PASS: a real Word file was selected through the file chooser, imported, copied to the session work folder, and rendered with `Baseline Test v2`, `Revenue grew 25% in Q4.`, and `LIVE-EDIT-MARKER-777`. |
| Drop target | PASS: Playwright's native file-drop action opened the Excel and PowerPoint fixtures. The live previews showed the monthly-sales sheet/chart and the `Baseline Deck` slide respectively. |
| Full-window drop | PASS: dropping `résumé test file.docx` on the page body preserved the Unicode display name and opened the correct Word content. |
| PDF drop | PASS: a 664,625-byte searchable PDF was imported and converted through the Word-first pipeline in about 9.1 seconds. The resulting 320,683-byte DOCX opened with a healthy preview and exposed searchable resume text. |
| Office validity | PASS: OfficeCLI validation reported no errors for the Word, Excel, PowerPoint, and PDF-derived working files; `officecli view ... text` returned the expected content for all four. |
| Byte preservation | PASS: SHA-256 hashes for all four source fixtures were unchanged after testing, and every browser import copy hash exactly matched its source fixture. |
| Native desktop gesture | PASS: Windows Computer Use dragged `OGENT-NATIVE-DROP-TEST.docx` from Explorer onto the real `Ogent.lnk`. Ogent v0.7.0 launched on port 8765, opened the exact source in session `62091768`, produced a valid working DOCX, and preserved the source hash. |
| Browser quality | PASS: the 1440 × 900 visual inspection showed a clean two-pane layout with the first-class drop target, no clipping or overlap, and a connected live preview. Browser console inspection returned zero errors and warnings. |
| Protocol regressions | PASS: eight stdlib unit tests covered session selection, replacement watch allocation, direct OfficeCLI mode, busy targeting, warm reuse, upload-byte preservation, Unicode upload names, path traversal, reserved names, and unsupported extensions. Python compilation also passed. |

The temporary Explorer test tab was closed without disturbing the user's four
existing tabs. The disposable desktop source was moved back into the ignored
workspace test area after validation. Both production and isolated test servers
were stopped, the right-click registration remains enabled, and no public push
was performed.

### Honest remaining limits

- Browser imports are retained locally until the user removes the Ogent local
  data; there is no automatic import-pruning control yet.
- Browser and shortcut drops intentionally accept one file at a time.
- Image-only PDFs still stop with `needs OCR`; PDF editing still occurs in a
  converted DOCX.
- Word view remains the fidelity check for complex floating Word layouts.

## v0.8.0 - temporary read-only chat references

Verified on 2026-07-26 with Windows 11, Python 3.14.3, pypdfium2 5.12.1,
Pillow 12.1.1, OfficeCLI 1.0.142, Codex CLI 0.145.0, Microsoft Office,
Microsoft Edge, Playwright CLI, and GPT-5.6 Sol with Max reasoning.

### Architecture delivered

- Composer references use dedicated `/reference/upload`, `/reference/remove`,
  and `/reference/clear` routes. They do not reuse the active-document
  `/upload` route.
- Every session owns an isolated pending set. Send atomically moves that set
  from a random pending directory into one random run directory; uploads made
  while the run works remain pending for the next run.
- Browser state exposes only attachment ID, sanitized filename, byte size,
  detected kind, status, and safe error text. Temporary absolute paths remain
  server-side.
- Upload reservations make the five-file and 100 MB combined limits atomic
  under concurrent requests. Inspection runs in a killable Python helper so
  native ZIP, PDFium, and image decoders do not execute inside the HTTP server.
- The validator checks actual PDF, OOXML, text-encoding, and image content. It
  also rejects ZIP prefix tricks, traversal or duplicate members, embedded
  executables, macros, ActiveX/OLE payloads, excessive expansion, oversized
  images, unsupported types, empty files, and extension/signature mismatches.
- `ogent_references.py` performs bounded extraction and rendering. OfficeCLI is
  restricted to read-only `view ... text`; searchable PDF text is grouped under
  page headings; scanned/low-text pages and images become normalized PNG inputs.
  Visually requested Office files export only inside the run directory, then
  render to PNG.
- Codex receives repeated image arguments before positional arguments, with an
  explicit `--` boundary for the installed CLI's variadic new-run image option.
  Reference runs use a fresh thread, workspace-write only inside the temporary
  run, and a prompt that treats all reference content as untrusted evidence.
- One path-contained, idempotent deletion primitive removes uploads and every
  derived artifact. Terminal cleanup runs after owned preprocessing, Office,
  and Codex processes release their files. Startup clears crash leftovers only
  after Ogent owns the selected listener.

### Automated verification

`py -3 -m unittest discover -s ogent-lite/tests -v` passed all 26 tests in
15.8 seconds. The reference coverage includes:

- active-document isolation, no recents/dedupe/watch changes, safe browser
  metadata, filename normalization, and manual Remove/Clear;
- empty, malformed, mismatched, embedded-OLE, prefixed-ZIP, oversized,
  over-page-limit, traversal-name, unsupported, and truncated upload rejection;
- five simultaneous successful reservations with the sixth rejected;
- frozen-run versus next-run attachment ownership;
- two-session metadata, preparation, findings, and transcript isolation;
- analysis-only success, direct image/PDF rendering, Codex failure,
  preprocessing failure, Stop, close, retryable cleanup failure, crash-root
  reset, and outside-root deletion refusal;
- correct new/resumed Codex image argument placement; and
- the eight pre-existing shell/open/upload regressions.

The following checks also passed:

```text
py -3 -m py_compile ogent-lite\ogent.py ogent-lite\ogent_references.py
py -3 -m ruff check ogent-lite/ogent.py ogent-lite/ogent_references.py ogent-lite/tests
PowerShell parse of tools\office-reference-to-pdf.ps1
git diff --check
```

### Edge and live GPT acceptance

| Check | Result |
|---|---|
| Composer DOCX drop | PASS: `marker-reference.docx` produced a Ready chip while the left pane remained `No document open`; recents, dedupe state, and preview were unchanged. |
| Paperclip and multiple files | PASS: the native browser file chooser attached a long TXT filename; a three-file composer drop produced five Ready chips total. The long name ellipsized without hiding its accessible full name. |
| Drag isolation | PASS: a synthetic file drag set `defaultPrevented=true`, added the composer highlight, suppressed the whole-page overlay, and displayed `Drop to attach as temporary references`. |
| Error state | PASS: a false `.pdf` displayed a red Failed chip with `The file extension does not match a PDF with a valid %PDF signature.` The server left no rejected upload artifact. |
| Responsive themes | PASS: 1440 × 900 light, 760 × 900 narrow, and 1440 × 900 dark captures showed no clipping, overlap, blank panel, or broken control. |
| Active document + DOCX reference | PASS: GPT-5.6 Sol Max read `DOCX-REFERENCE-MARKER-7429` and appended only `ACTIVE-DOC-EDIT-FROM-REFERENCE-7429` to the protected working DOCX. OfficeCLI readback found the exact final paragraph and validation returned no errors. The original DOCX and reference hashes were unchanged. |
| Empty-message searchable PDF | PASS: Ogent supplied the documented default request. GPT-5.6 Sol Max reported both unique markers from `searchable-reference.pdf`, cited page 1, and stated that searchable extracted text—not OCR—was used. No Office document was created. |
| Scanned PDF OCR | PASS: GPT-5.6 Sol Max read `Intake Review Process Flow` from page 1 of an image-only PDF and explicitly identified it as OCR with no searchable extracted text. |
| Direct image vision | PASS: from `process-flow-qa.png`, GPT-5.6 Sol Max inferred Yes → Archive and No → Revise → Review again. |
| Frozen versus next run | PASS: the scanned PDF and PNG chips were locked as OCR/vision during their run. A TXT attached while they worked stayed Ready and removable; terminal cleanup deleted only the frozen run. |
| Stop | PASS: Stop terminated a real Codex reference run, produced `Stopped. No further agent work is running.`, then `Temporary references deleted.`, with an empty reference root and no Codex child. |
| Crash recovery | PASS: a force-killed isolated server left one pending upload. Restarting v0.8.0 recreated an empty reference root before accepting sessions. |
| Shutdown and reap | PASS: shutdown deleted a pending reference. A four-second test grace reaped a disconnected session, stopped its exact OfficeCLI watch, and left an empty reference root. |
| Office visual reference | PASS: a disposable PPTX copy produced OfficeCLI-labeled extracted text, a temporary PDF, and one visually correct rendered slide. Source and temporary-copy SHA-256 hashes matched, and PowerPoint exited. |

The local, Git-ignored visual evidence is under
`output\ogent-reference-acceptance\` with descriptive light, dark, narrow,
drag-highlight, error-state, and frozen-run filenames.

The first scanned-PDF/image attempt found a real installed-CLI edge case:
`codex exec -i <file>...` greedily consumed the positional prompt. The command
builder now inserts `--` before a new-run prompt. Targeted tests and the repeated
live vision run passed after that correction.

### Existing-workflow regression evidence

- Whole-page drop still opened a protected Word working copy with its connected
  preview; composer drop of the same document type did not open it.
- Paste-path open displayed the expected Word fixture. Word view produced a
  Microsoft Word-rendered PDF tab.
- Whole-page drop of the searchable PDF preserved the source, converted it to a
  validated working DOCX, and exposed both unique text markers through
  `officecli view`.
- The native Browse button launched the topmost `Open in Ogent` Windows picker;
  the current automation capture canceled it after verifying the dialog. The
  earlier v0.5 matrix remains the full file-selection baseline.
- The registered DOCX/XLSX/PPTX commands still contain the exact Python,
  `ogent.py --open "%1"`, icon, and `Position=Top` values. A warm shell-route
  call opened the PowerPoint fixture as a protected document.
- Model and all six reasoning choices remained present; the live reference runs
  used GPT-5.6 Sol Max.
- A short isolated idle timeout exited the empty backend, removed
  `server.json`, and closed port 8765.

### Integrity and process evidence

Pre/post SHA-256 hashes matched for the Word, Excel, PowerPoint, PNG, DOCX
reference, searchable PDF, and scanned PDF fixtures. The final process audit
found no Ogent server, Codex run, OfficeCLI watch, Office-reference helper,
Word, Excel, PowerPoint, PDF-rendering, or OCR process owned by the test.

### Privacy and remaining limits

- Reference deletion is best-effort local deletion, not forensic erasure from
  NTFS, SSDs, backups, antivirus caches, or synchronized storage.
- Deleting local reference files does not remove their contents from an
  existing Codex conversation context.
- OCR and visual findings are model interpretations. Ogent labels them and
  requires the answer not to claim unreadable or unprocessed content was read.
- Visual Office reference export requires Microsoft Office or LibreOffice.
  Text-only Office extraction remains available through OfficeCLI.
- PDF references stop at 25 pages and image/renderer limits are intentionally
  conservative; Ogent rejects over-limit material instead of truncating it.

Final v0.8.0 acceptance state: all isolated test servers and owned child
processes are stopped, the right-click registration remains enabled, sources
are unchanged, unrelated worktree files remain untouched, and no public push
was performed.

## v0.9.0 - dynamic Codex and Claude Code providers

Verified on 2026-07-27 with Windows 11, Python 3.14.3, OfficeCLI 1.0.142,
Codex CLI 0.145.0, Claude Code 2.1.220, Microsoft Word, and the in-app browser.
This release removes Ogent's static model and effort assumptions and adds an
independent Claude Code execution path beside Codex.

### Capability architecture delivered

- The installed, authenticated CLI is the only production source of model and
  effort choices. A regression guard fails if a static Codex or Claude catalog
  is added to the production modules.
- Codex discovery uses App Server `model/list`, including pagination and
  per-model reasoning capabilities. `codex debug models` is a dynamic fallback
  for a compatible CLI when App Server discovery fails.
- Claude Code model aliases come from a local `/model` request that must report
  zero API duration, zero cost, and zero input/output/cache tokens. Global
  effort candidates come from `claude --help`; the selected model is checked
  lazily with bounded, zero-inference `/model` probes.
- Capability data is cached atomically under
  `%LOCALAPPDATA%\OgentLite\agent-capabilities-v1.json`. Cache identity includes
  provider, normalized executable path, and exact CLI version. Cached data is
  marked stale and can explain the interface while refreshing, but it cannot
  authorize a run.
- The browser exposes **Agent**, **Model**, and **Effort** selectors plus
  **Refresh**. Loading, ready, sign-in-required, unavailable, stale, and
  incompatible states have distinct messages. Send remains disabled until the
  server validates a live selection.
- Provider, model, and effort selections persist independently in the browser.
  A model change starts a fresh provider context. Codex and Claude session IDs
  never cross providers or documents; switching back may resume only a
  compatible context owned by that provider and document.
- Temporary-reference runs remain non-resumable and isolated. Their run
  directory is released only after the owned provider and preprocessing
  processes exit, then it is deleted through the existing contained cleanup
  primitive.
- Stop targets the active provider process tree. Claude runs use a minimal
  allowlist containing the OfficeCLI MCP tool and do not use permission bypass
  flags.

### Live CLI capability evidence

The verified Codex account reported seven models:

```text
gpt-5.6-sol
gpt-5.6-terra
gpt-5.6-luna
gpt-5.5
gpt-5.4
gpt-5.4-mini
gpt-5.3-codex-spark
```

Per-model Codex efforts came directly from the CLI. For example,
`gpt-5.6-sol` and `gpt-5.6-terra` reported low, medium, high, xhigh, max, and
ultra; Ogent does not assume those values for a different CLI, account, or
future version.

The verified Claude account reported ten aliases:

```text
sonnet
opus
haiku
fable
best
sonnet[1m]
opus[1m]
fable[1m]
opusplan
default
```

A lazy zero-inference check for `sonnet` verified low, medium, high, xhigh, and
max with zero reported usage. The browser also exercised lazy verification for
`opus`. Aliases and effort support remain account- and CLI-specific.

### Automated verification

`python -m unittest discover -s ogent-lite\tests -v` passed all 72 tests in
16.404 seconds. Coverage includes:

- cache expiry, executable/version invalidation, atomic replacement, stale/live
  gating, duplicate-refresh suppression, and secret-free serialization;
- Codex App Server pagination, malformed output, timeout cleanup, dynamic
  fallback, account filtering, and per-model capabilities;
- Claude zero-usage enforcement, account-visible alias parsing, wrapped help,
  lazy effort probes, exact-match rejection, inference detection, explicit
  snake_case and camelCase input/output/cache accounting, missing and
  unauthenticated states, and fail-closed malformed output;
- first-run, resume, model-change, provider-switch, multi-document, reference,
  stream parsing, structured error, Stop, failed/stopped-thread rejection, and
  session-isolation behavior;
- the complete temporary-reference safety suite and all existing shell,
  working-copy, upload, and direct-mode regressions; and
- narrow-screen stacking plus saved-provider restoration after a transient
  refresh fallback.

Python compilation and `git diff --check` passed. Ruff initially found two
unused imports in the new test modules; they were removed and the clean Ruff
result was rerun before completion.

A final read-only supervisor review found three issues before commit: camelCase
cache-token accounting was not fail-closed, failed/stopped Codex runs could
retain an unusable thread ID, and Claude's CLI-valid compatibility fallback was
labeled as model-verified. All three were corrected, covered by new regression
tests, and included in the 72-test rerun. The stricter validator was then
exercised against the installed Claude CLI; it reported zero input, output,
cache-creation, cache-read, and ephemeral-cache token fields, zero API time, and
zero cost.

### Browser verification

The in-app browser exercised the production interface at a disposable local
test server:

- Codex displayed all seven live models and their CLI-reported per-model
  efforts.
- Claude displayed all ten account-visible aliases. Selecting `opus` showed
  the checking state and then enabled only its verified effort choices.
- Refresh showed an explicit provider-refreshing state, disabled Send, and
  restored the validated selection after completion.
- The final desktop layout was visually inspected: the two panes, agent/model/
  effort controls, status text, document drop surface, and composer were
  visible without clipping or overlap.

A final real-Chromium Playwright pass exercised the exact post-review build at
1440 x 900 and 390 x 844. It found and corrected a narrow-screen defect in the
old side-by-side minimum widths; below 760 px the document and chat panes now
stack. At 390 px, the document scroll width equaled the viewport width, every
agent control and Send ended at or before x=376, and the full page remained
vertically scrollable. The 1440 px layout also had no horizontal overflow.
Desktop and mobile screenshots were inspected, and the final browser console
reported zero errors and zero warnings.

The same production renderer was exercised with normalized checking,
not-installed, authentication-required, catalog-error, cached-refresh,
CLI-default-only, and globally CLI-valid/model-unverified states. Send was
disabled for every unavailable or stale state, and the unverified fallback was
labeled explicitly. A live Codex refresh temporarily selected the still-ready
Claude provider; after refresh, the saved live Codex selection, model, and
effort were restored without reloading the page. Claude `opus` was selected,
verified lazily, and accepted a live CLI-reported explicit effort before the
provider was switched back to Codex.

### Live protected-copy Office edits

Two synthetic Word sources were created under the local test area. Each provider
received its model and effort from the live catalog, edited only Ogent's
protected working copy, and used OfficeCLI to read back and validate the result.

| Provider | Live selection | Marker in working copy | Source unchanged | OfficeCLI validation |
|---|---|---|---|---|
| Codex | CLI-reported model, Automatic | `CODEX-V090-FINAL-PASS` | PASS | No errors; zero issues |
| Claude Code | CLI-reported model, Automatic | `CLAUDE-V090-FINAL-PASS` | PASS | No errors; zero issues |

The Claude acceptance transcript showed successful
`mcp__officecli__officecli` calls. The first live attempt exposed two real CLI
integration defects: streamed print mode required `--verbose`, and the minimal
permission allowlist omitted the OfficeCLI MCP tool. Both command builders and
their regression tests were corrected before the passing rerun.

### Live API, isolation, Stop, and reference evidence

- The current-code API returned schema version 1 with both catalogs live and
  non-stale. A selected Claude model moved from Automatic-only to five
  zero-use verified effort choices while the other nine models remained lazy.
- A manual provider refresh immediately exposed cached/stale status, then
  returned to live without a page reload. The successful live refresh cleared
  old lazy-probe results so the current selected model must be verified again.
- Concurrent Codex and Claude edits used distinct Ogent run IDs, working
  documents, watch ports, and provider contexts. Neither provider ID appeared
  in the other document session, each live preview contained only its own
  marker, and both source hashes stayed unchanged.
- Stop returned success for both real provider processes. Each session ended
  with `stopped`, and both owned process trees had exited.
- A Claude analysis-only reference run reported the source marker, left the
  reference source hash unchanged, deleted the temporary copy, and persisted no
  Claude session. The next normal run created a new resumable context and did
  not contain the prior reference marker.

### Honest limits

- Claude Code does not expose a stable machine-readable model catalog endpoint
  equivalent to Codex App Server. Ogent therefore uses a tightly bounded,
  zero-inference CLI interaction and fails closed if usage accounting is
  missing, nonzero, or malformed.
- A provider may remove or rename a model between refresh and Send. The server
  revalidates the selected live catalog and returns an actionable error instead
  of silently substituting another model or effort.
- **Automatic — CLI default** is the only portable effort choice across every
  provider and model. Any explicit effort appears only after the corresponding
  CLI reports or verifies it.
- Provider authentication, usage limits, service availability, and data
  retention remain controlled by OpenAI or Anthropic, not by Ogent.

Final v0.9.0 acceptance state: both installed providers supplied their live
catalogs and completed protected-copy Office edits; source documents were
unchanged; automated, browser, and OfficeCLI checks passed; task-generated
document artifacts remain local and uncommitted; and no public push was
performed.

### Publication readiness gate (2026-07-27)

The isolated publication worktree added a minimal-permission Windows GitHub
Actions workflow and refreshed the public installation and usage documentation.
Before any remote write, 72 deterministic tests passed in 15.997 seconds,
Python compilation passed, Ruff reported no errors, `git diff --check` passed,
the CI YAML parsed successfully, and every relative Markdown link resolved.
The publication diff introduced no credential signatures, private absolute
paths, Office documents, capability caches, screenshots, logs, or generated
test output.

The application code was unchanged during publication preparation. In
accordance with the release plan, the live Codex and Claude inference checks
documented above were not repeated solely for README and CI changes.

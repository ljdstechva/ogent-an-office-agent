# Ogent Lite Verification Report

Date: 2026-07-23
Status: T1–T7 pass

## Runtime and architecture

- Runtime: system Python 3.14.3
- Application: one Python file with embedded HTML, CSS, and JavaScript
- Dependencies: Python standard library only
- Server bind: `127.0.0.1` only
- Preferred app port: 8765, with automatic upward fallback
- OfficeCLI watch port: 26315
- Agent backend: Codex CLI 0.144.1
- Models: selectable `gpt-5.6-sol` or `gpt-5.6-terra`
- Reasoning effort: selectable low, medium, high, xhigh, max, or ultra
- Recommended defaults: `gpt-5.6-sol` with medium reasoning
- OfficeCLI: 1.0.140
- Browser test engine: Playwright with installed Microsoft Edge

The direct Codex preflight returned `READY` in 15.724 seconds. The global Codex
instructions contain the mandatory OfficeCLI routing block.

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
- `ogent stop` left no server-owned process and released watch port 26315.

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

- One Codex run at a time.
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

<p align="center">
  <img src="assets/ogent-logo.svg" alt="Ogent" width="360">
</p>

# Ogent Lite

Ogent Lite 0.8.0 is a featherweight, local document workspace: OfficeCLI keeps a live
Word, Excel, or PowerPoint preview on the left, while Codex handles plain-language
editing requests in the chat pane on the right.

It runs entirely on `127.0.0.1`, uses the existing Codex CLI login, and does not
need a new API key. Source documents are never edited directly. Every opened
Office file is copied to `%LOCALAPPDATA%\OgentLite\work\` first. Files dropped
into the browser are also preserved byte-for-byte under
`%LOCALAPPDATA%\OgentLite\imports\` before the working copy is created.
Files attached at the chat composer follow a separate, temporary read-only
reference lifecycle and never become active documents.

For the AI-agent installation sentence and complete human setup, see the
[repository README](../README.md).

## Start

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
closed. If the selected workspace is running Codex, Ogent leaves its document
unchanged and shows the busy message in that workspace. If that exact source is
already open elsewhere, Ogent focuses the existing session instead of starting
a duplicate OfficeCLI watch. Other independent sessions remain untouched, and
the original document is still protected by Ogent's working-copy workflow.

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
2. Choose the model and reasoning effort, describe the change, and review it live on the left.
3. Use **+ New window** for another document. The session dropdown switches
   among open workspaces without merging their documents or chats.
4. For a complex DOCX, use **Word view** when exact floating-shape placement
   matters. It opens a Microsoft Word-rendered PDF in a new browser tab.
5. To analyze supporting material, click the paperclip or drop files on the
   composer. A composer drop attaches a temporary reference; a drop elsewhere
   opens a protected working document.

For PDFs, drag the PDF into Ogent or start with “Edit my PDF,” then paste its
absolute path. Ogent copies the PDF, converts the copy to a working DOCX through
the Word-first pipeline, and opens that DOCX for editing. Complex PDF reflow may
still need layout cleanup; image-only PDFs require OCR.

## Temporary read-only references

The chat composer accepts multiple DOCX, XLSX, PPTX, PDF, TXT, Markdown, CSV,
PNG, JPEG, WebP, BMP, and TIFF references. Limits are five files per run,
50 MB per file, 100 MB combined, and 25 pages per PDF. Ogent validates file
content and signatures; it rejects empty, malformed, mismatched, executable,
archive, legacy Office, and over-limit inputs with an actionable Failed state.

References may be sent with typed instructions or by pressing **Send** with an
empty message. The latter uses: `Read and analyze the attached reference files.
Summarize the important findings.` Once sent, those chips are locked to that
run. New attachments remain removable and belong to the next run.

OfficeCLI performs only read operations on Office references. Searchable PDF
text is extracted with page headings. Scanned or low-text PDF pages, supported
images, and visually requested Office content are normalized into bounded
temporary images and passed through Codex's image interface for OCR and visual
interpretation. Ogent does not use an external OCR service.

Every upload, extraction, rendered page, and manifest stays under
`%LOCALAPPDATA%\OgentLite\temporary-references\`. Ogent deletes a claimed run
directory after success, Codex error, Stop, or preparation failure; Remove,
Clear all, session cleanup, and backend shutdown delete their corresponding
files. Startup removes abandoned contents after a prior crash. Cleanup happens
only after owned preprocessing, Office, or Codex processes release the files.

This is ordinary best-effort local deletion, not secure forensic erasure on
NTFS, SSDs, backups, antivirus caches, or synchronized storage. **References
are temporary local copies and are deleted after this run. Their contents are
sent to Codex for analysis and may remain in the Codex conversation context.**

## Stop

- In PowerShell: `ogent stop`
- Or from this folder: `ogent.cmd stop`

Stopping Ogent also stops its OfficeCLI watch and any Codex process it owns.

## Sessions and automatic cleanup

Each fresh browser workspace creates one Ogent session. Each session has its own
protected working copy, transcript, Codex thread, run state, and OfficeCLI watch
port from 26320-26380. Different sessions may edit different files concurrently;
one individual session allows one Codex run at a time. Tabs that navigate to the
same deduplicated session share that workspace.

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
| Protected working copies | `%LOCALAPPDATA%\OgentLite\work\` |
| Temporary chat references | `%LOCALAPPDATA%\OgentLite\temporary-references\` |
| Running-server record | `%LOCALAPPDATA%\OgentLite\server.json` |

Recent paths and working documents stay local and are excluded from Git.
Temporary references are not written to recents or the active-document index.

## Requirements

- Windows 11 with Python 3 (`py -3 --version`)
- OfficeCLI (`officecli --version`)
- Codex CLI signed in (`codex --version`)
- Pinned Python packages:

  ```powershell
  py -3 -m pip install -r '.\requirements.txt'
  ```

  The tested pins are pypdfium2 5.12.1 for PDF inspection/rendering and
  Pillow 12.1.1 for image validation/normalization.

Ogent defaults to `gpt-5.6-sol` with medium reasoning and allows one document
run at a time per session.

## Model and reasoning

The controls above the message box apply to the next Codex request:

- **Model:** GPT-5.6 Sol or GPT-5.6 Terra
- **Reasoning:** Low, Medium, High, XHigh, Max, or Ultra

The recommended defaults are GPT-5.6 Sol and Medium. Ogent remembers the selected
combination in the local browser, restores it after a reload, and disables both
controls while a run is active. The server validates every selection before
starting Codex.

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
| Codex is not logged in | Open PowerShell, run `codex`, complete sign-in, then restart Ogent. |
| No preview port is available | Ogent allocates one port per session from 26320-26380. Close unused sessions or stale manual OfficeCLI watches, then retry. |
| A closed tab still appears briefly | The 120-second grace absorbs refreshes and accidental closes. Reopen its `/?s=<id>` URL to reconnect or let it reap automatically. |
| Complex DOCX preview looks incomplete | The live HTML view approximates some floating shapes. Click **Word view** and verify before concluding content is missing. |
| PDF opens with broken spacing | PDF Reflow preserved editable content but not exact layout; clean up the working DOCX or use the original source document. |
| PDF reports that OCR is needed | The PDF is image-only. Run OCR first, then import the searchable PDF. |
| A reference says Failed | Check the extension and file content, then confirm the 50 MB/file, 100 MB/run, five-file, and 25-page PDF limits. Rejected uploads are removed. |
| Reference PDF/image preparation is unavailable | Run `py -3 -m pip install -r .\requirements.txt`, restart Ogent, and attach the file again. |
| A visual Office reference cannot render | Text extraction can still work. Microsoft Office or LibreOffice is required for the optional temporary visual export; attach a PDF export if exact visual analysis is essential. |
| Files remain after an abnormal crash | Restart Ogent once. Startup clears the abandoned temporary-reference root before accepting sessions. |
| A run is taking too long | Click **Stop**; Ogent terminates the active Codex child process tree. |

## Privacy and limits

- Localhost only; no telemetry or external web assets.
- No direct edits to source documents.
- Composer references never become active documents, watches, recents, dedupe
  entries, or final outputs. Their browser metadata contains no temporary path.
- Reference contents are sent to Codex. Local deletion does not remove facts
  already present in the Codex conversation context.
- Browser drag/drop accepts one DOCX, XLSX, PPTX, or PDF at a time, up to
  128 MB, and retains a local import copy until the user removes it.
- Composer references accept up to five supported files per run, 50 MB each,
  100 MB combined, and 25 pages per PDF.
- One active document and one Codex run per session; sessions are independent.
- Excel live preview does not support click-to-select paths.
- PDF editing happens in a converted DOCX, never in the PDF itself.
- Word view currently supports DOCX only and requires Microsoft Word.

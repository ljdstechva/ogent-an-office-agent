<p align="center">
  <img src="assets/ogent-logo.svg" alt="Ogent" width="360">
</p>

# Ogent Lite

Ogent Lite is a featherweight, local document workspace: OfficeCLI keeps a live
Word, Excel, or PowerPoint preview on the left, while Codex handles plain-language
editing requests in the chat pane on the right.

It runs entirely on `127.0.0.1`, uses the existing Codex CLI login, and does not
need a new API key. Source documents are never edited directly. Every opened
Office file is copied to `%LOCALAPPDATA%\OgentLite\work\` first.

For the AI-agent installation sentence and complete human setup, see the
[repository README](../README.md).

## Start

- Double-click `ogent.cmd` in this folder.
- Or double-click the optional **Ogent** desktop shortcut after creating it
  with the instructions in the repository README.
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

The command starts Ogent when necessary or creates a new independent session
for the selected file. If that exact source is already open, Ogent focuses the
existing session instead of starting a duplicate OfficeCLI watch. The original
document is still protected by Ogent's working-copy workflow.

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

1. Start Ogent, paste the absolute `.docx`, `.xlsx`, or `.pptx` path, and click **Open**.
2. Choose the model and reasoning effort, describe the change, and review it live on the left.
3. Use **+ New window** for another document. The session dropdown switches
   among open workspaces without merging their documents or chats.
4. For a complex DOCX, use **Word view** when exact floating-shape placement
   matters. It opens a Microsoft Word-rendered PDF in a new browser tab.

For PDFs, start with “Edit my PDF,” then paste its absolute path. Ogent copies the
PDF, converts the copy to a working DOCX through the Word-first pipeline, and
opens that DOCX for editing. Complex PDF reflow may still need layout cleanup;
image-only PDFs require OCR.

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
| Protected working copies | `%LOCALAPPDATA%\OgentLite\work\` |
| Running-server record | `%LOCALAPPDATA%\OgentLite\server.json` |

Recent paths and working documents stay local and are excluded from Git.

## Requirements

- Windows 11 with Python 3 (`py -3 --version`)
- OfficeCLI (`officecli --version`)
- Codex CLI signed in (`codex --version`)

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
| A run is taking too long | Click **Stop**; Ogent terminates the active Codex child process tree. |

## Privacy and limits

- Localhost only; no telemetry or external web assets.
- No direct edits to source documents.
- One active document and one Codex run per session; sessions are independent.
- Excel live preview does not support click-to-select paths.
- PDF editing happens in a converted DOCX, never in the PDF itself.
- Word view currently supports DOCX only and requires Microsoft Word.

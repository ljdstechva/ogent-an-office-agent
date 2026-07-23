# Ogent Lite

Ogent Lite is a featherweight, local document workspace: OfficeCLI keeps a live
Word, Excel, or PowerPoint preview on the left, while Codex handles plain-language
editing requests in the chat pane on the right.

It runs entirely on `127.0.0.1`, uses the existing Codex CLI login, and does not
need a new API key. Source documents are never edited directly. Every opened
Office file is copied to `%LOCALAPPDATA%\OgentLite\work\` first.

## Start

- Double-click the **Ogent** desktop shortcut.
- Or run `ogent` from PowerShell.
- Or double-click `ogent.cmd` in this folder.

If Ogent is already running, another launch opens the existing browser page
instead of starting a second server.

## Daily recipe

1. Start Ogent, paste the absolute `.docx`, `.xlsx`, or `.pptx` path, and click **Open**.
2. Choose the model and reasoning effort, describe the change, and review it live on the left.

For PDFs, start with “Edit my PDF,” then paste its absolute path. Ogent copies the
PDF, converts the copy to a working DOCX through the Word-first pipeline, and
opens that DOCX for editing. Complex PDF reflow may still need layout cleanup;
image-only PDFs require OCR.

## Stop

- In PowerShell: `ogent stop`
- Or from this folder: `ogent.cmd stop`

Stopping Ogent also stops its OfficeCLI watch and any Codex process it owns.

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

Ogent currently uses `gpt-5.6-sol` with medium reasoning and allows one document
run at a time by default.

## Model and reasoning

The controls above the message box apply to the next Codex request:

- **Model:** GPT-5.6 Sol or GPT-5.6 Terra
- **Reasoning:** Low, Medium, High, XHigh, Max, or Ultra

The recommended defaults are GPT-5.6 Sol and Medium. Ogent remembers the selected
combination in the local browser, restores it after a reload, and disables both
controls while a run is active. The server validates every selection before
starting Codex.

## Troubleshooting

| Symptom | What to do |
|---|---|
| Preferred port 8765 is busy | Ogent automatically tries 8766 and higher. Launch again and use the browser page it opens. |
| Preview says reconnecting | Click the reload icon. Ogent also restarts the OfficeCLI watch before the next chat run. |
| Codex is not logged in | Open PowerShell, run `codex`, complete sign-in, then restart Ogent. |
| “Port 26315 is already in use” | Stop the stale OfficeCLI watch using that port, then click the reload icon. |
| PDF opens with broken spacing | PDF Reflow preserved editable content but not exact layout; clean up the working DOCX or use the original source document. |
| PDF reports that OCR is needed | The PDF is image-only. Run OCR first, then import the searchable PDF. |
| A run is taking too long | Click **Stop**; Ogent terminates the active Codex child process tree. |

## Privacy and limits

- Localhost only; no telemetry or external web assets.
- No direct edits to source documents.
- One active document and one Codex run at a time.
- Excel live preview does not support click-to-select paths.
- PDF editing happens in a converted DOCX, never in the PDF itself.

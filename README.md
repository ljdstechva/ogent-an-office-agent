<p align="center">
  <img src="ogent-lite/assets/ogent-logo.svg" alt="Ogent" width="420">
</p>

# Ogent — an office agent

Ogent is a local Windows workspace for editing real Word, Excel, and PowerPoint
files with plain-language instructions. It places an OfficeCLI live preview
beside a Codex chat, runs on `127.0.0.1`, and creates a protected working copy
before any edit touches a document.

The current app is **Ogent Lite 0.4.0**. It uses your existing Codex CLI login,
so it does not require a separate OpenAI API key. Ogent is open source under the
[MIT License](LICENSE).

## Install Ogent

### Option 1 — Let an AI agent install it (recommended)

Copy and paste this one sentence into Codex or another local AI agent that can
run PowerShell:

```text
Install and configure Ogent on this Windows 11 PC from https://github.com/ljdstechva/ogent-an-office-agent: read the repository README and AGENTS.md first; reuse compatible tools already installed; install or update Git, Python 3, OpenAI Codex CLI, and OfficeCLI only from their official sources; verify downloaded installers or scripts before running them; clone or fast-forward the repository into a folder I control; let me complete any unavoidable Windows elevation or ChatGPT sign-in without asking me to paste secrets into chat; verify that py -3, git, codex, and officecli all work; from the ogent-lite folder register the per-user Open in Ogent shell command, create or refresh an Ogent desktop shortcut targeting ogent.cmd with assets\ogent.ico, launch Ogent, and verify that its health endpoint reports version 0.4.0 and that a disposable DOCX opens as a protected working copy while the original file hash remains unchanged; leave the right-click integration enabled; and finish by reporting the installed versions, paths, test evidence, and any remaining limitation.
```

The prompt deliberately leaves sign-in and elevation with the human and never
asks for a password, token, or API key.

### Option 2 — Human install on Windows

1. Install [Git for Windows](https://git-scm.com/install/windows) and
   [Python 3](https://www.python.org/downloads/windows/), then open a new
   PowerShell window:

   ```powershell
   git --version
   py -3 --version
   ```

2. Clone Ogent into a folder you control:

   ```powershell
   git clone https://github.com/ljdstechva/ogent-an-office-agent.git
   Set-Location '.\ogent-an-office-agent'
   ```

3. Install [OpenAI Codex CLI](https://github.com/openai/codex), then sign in
   interactively with ChatGPT:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
   codex --version
   codex
   ```

4. Install [OfficeCLI](https://github.com/iOfficeAI/OfficeCLI):

   ```powershell
   irm https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.ps1 | iex
   officecli --version
   ```

5. Register **Open in Ogent** for your Windows account and launch the app:

   ```powershell
   Set-Location '.\ogent-lite'
   py -3 .\ogent.py --register-shell
   .\ogent.cmd
   ```

   Your browser opens the local app, normally at
   `http://127.0.0.1:8765/`. No AionUi installation or OfficeCLI MCP call is
   required: Ogent invokes Codex CLI and OfficeCLI automatically.

The Explorer command appears under **Right-click > Show more options > Open in
Ogent** for `.docx`, `.xlsx`, and `.pptx`. It is installed only for the current
Windows account and does not require administrator rights.

Microsoft Office is optional for normal DOCX/XLSX/PPTX editing. PDF import uses
Microsoft Word 2016 or later when available, with
[LibreOffice](https://www.libreoffice.org/download/download-libreoffice/) as a
less precise fallback.

### Optional desktop shortcut

Run this once from the `ogent-lite` folder:

```powershell
$ogentDir = (Resolve-Path '.').Path
$desktopDir = [Environment]::GetFolderPath('Desktop')
$shortcutShell = New-Object -ComObject WScript.Shell
$shortcut = $shortcutShell.CreateShortcut((Join-Path $desktopDir 'Ogent.lnk'))
$shortcut.TargetPath = Join-Path $ogentDir 'ogent.cmd'
$shortcut.WorkingDirectory = $ogentDir
$shortcut.IconLocation = (Join-Path $ogentDir 'assets\ogent.ico') + ',0'
$shortcut.Save()
```

After that, double-click **Ogent** on the desktop whenever you want to start or
return to the app.

## Use Ogent

### Edit an existing document

1. Right-click a `.docx`, `.xlsx`, or `.pptx` and select **Open in Ogent**.
   You can also launch `ogent.cmd`, paste the document's absolute path, and
   click **Open**.
2. Choose the model and reasoning effort above the message box. The recommended
   day-to-day setting is **GPT-5.6 Sol + Medium**.
3. Describe the change in plain language and press **Enter**.
4. Review each change in the live preview. Ogent asks Codex to use OfficeCLI,
   read the result back, and validate the working document.

### Start a new document

Create a blank file first in Word, Excel, PowerPoint, or OfficeCLI, then open it
in Ogent:

```powershell
$newDocument = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'new-report.docx'
officecli create $newDocument
```

Use `.xlsx` or `.pptx` instead when starting a workbook or presentation.

### Switch to another document

Open the next file from Explorer or paste its path into Ogent. The running
session switches to that file automatically. All open Ogent browser tabs show
the same active document.

Ogent currently supports **one active document and one Codex run per server**.
It does not create independent parallel workspaces for multiple browser tabs.
Finish or stop the current run before switching documents.

### Keep the finished file

Ogent edits a timestamped copy under
`%LOCALAPPDATA%\OgentLite\work\`; the source file remains untouched. Once the
result is approved, stop Ogent and copy the working file to the final location
and filename you want. Validate that final copy before delivery.

### Edit a PDF

Start Ogent, type `Edit my PDF`, and paste the PDF's absolute path. Ogent copies
the PDF, converts the copy to a working DOCX, and opens that DOCX for editing.
The original PDF is never edited. Image-only PDFs stop honestly because they
need OCR; complex layouts may require cleanup after Word PDF Reflow.

## Start, stop, update, and uninstall

From the `ogent-lite` folder:

```powershell
# Start or return to the existing app
.\ogent.cmd

# Stop Ogent, its OfficeCLI preview, and any Codex process it owns
.\ogent.cmd stop
```

To update:

```powershell
.\ogent.cmd stop
Set-Location '..'
git pull --ff-only
Set-Location '.\ogent-lite'
py -3 .\ogent.py --register-shell
.\ogent.cmd
```

Re-register after moving or renaming the cloned repository because the Explorer
command stores the absolute Ogent path.

To remove the Explorer integration:

```powershell
.\ogent.cmd stop
py -3 .\ogent.py --unregister-shell
```

You may then delete the desktop shortcut and cloned repository. Local working
copies and recent-path history remain under `%LOCALAPPDATA%\OgentLite` until you
remove them.

## How it works

```text
Browser UI (127.0.0.1) -> Ogent server -> Codex CLI -> OfficeCLI -> protected working copy
                                      \-> OfficeCLI live preview -> Browser UI
```

- Ogent owns the local web server, document session, working-copy creation,
  OfficeCLI preview, and Codex process lifecycle.
- Codex receives the selected model and reasoning level with document-specific,
  single-agent editing instructions.
- OfficeCLI performs and validates the actual Office-file changes.
- AionUi is optional. The earlier AionUi workflow remains documented in
  [AIONUI-WORKFLOW.md](AIONUI-WORKFLOW.md), but it is not required to run the
  Ogent app.

## Verified workstation

- Windows 11
- Ogent Lite 0.4.0
- OfficeCLI 1.0.140
- Codex CLI 0.144.1
- GPT-5.6 Sol with selectable Low, Medium, High, XHigh, Max, and Ultra reasoning
- Native Microsoft Word, Excel, and PowerPoint rendering

The app's launch, protected-copy workflow, live preview, Codex edit, model and
reasoning selectors, Stop control, PDF import, Explorer integration, desktop
shortcut, and reversible unregister flow were exercised end to end. See
[ogent-lite/OGENT-REPORT.md](ogent-lite/OGENT-REPORT.md) for the app evidence.
The repository's 13 original Office test artifacts also pass OpenXML validation;
see [TEST-REPORT.md](TEST-REPORT.md).

## What Ogent demonstrates

- A local two-pane Ogent app with live Office preview, Codex chat, model and
  reasoning controls, and Windows Explorer integration
- Word reports with cover pages, live tables of contents, styles, headers, footers, page fields, tables, charts, and equations
- Excel workbooks with real formulas, evaluated totals, formatting, conditional formatting, and native charts
- PowerPoint decks with consistent themes, backgrounds, editable shapes, and charts
- An optional AionUi workflow for round-trip Office editing and CSV-to-Excel conversion (operator-attested; no AionUi screen capture is published)
- Web research converted into a concise, cited Word brief
- Safe PDF-to-DOCX editing and PDF re-export with scanned-file detection
- An honest Visio capability check plus a working native Word diagram alternative
- Replayable JSON templates for common report, deck, and budget workflows

## Visual QA

### Flagship Word report

![Six-page flagship Word report](aionui-tests/flagship-report-qa.png)

### PowerPoint deck

![Five-slide GreenGrid deck](aionui-tests/pitch-qa.png)

### Excel budget workbook

![Formula-driven Excel budget](aionui-tests/budget-qa.png)

## Repository structure

```text
.
├── README.md
├── LICENSE
├── AGENTS.md
├── AIONUI-WORKFLOW.md
├── TEST-REPORT.md
├── ogent-lite/
│   ├── ogent.py
│   ├── ogent.cmd
│   ├── OGENT-REPORT.md
│   └── assets/
├── tools/
│   ├── pdf2docx.ps1
│   └── docx2pdf.ps1
├── templates/
│   ├── report-with-toc.json
│   ├── basic-deck.json
│   └── budget-workbook.json
└── aionui-tests/
    ├── baseline-batches/
    ├── *.docx / *.xlsx / *.pptx
    ├── *-batch.json
    └── *-qa.png
```

Installers, local logs, internal agent state, and machine-local source documents are intentionally excluded from version control.

All company, project, budget, and sales names or values in the demo Office artifacts are fictional or synthetic. The community-solar brief is a research demonstration and cites its external sources directly.

## Replay a template

From PowerShell with OfficeCLI installed:

```powershell
officecli create '.\new-report.docx'
officecli batch '.\new-report.docx' --input '.\templates\report-with-toc.json' --stop-on-error
officecli close '.\new-report.docx'
officecli refresh '.\new-report.docx'
officecli validate '.\new-report.docx'
```

Use `basic-deck.json` with a `.pptx` file or `budget-workbook.json` with an `.xlsx` file in the same way. Replace bracketed placeholders after replay, close the OfficeCLI resident before opening the file in Microsoft Office, and validate again after editing.

Choose a new output filename. The examples intentionally avoid overwriting an existing document.

## Edit a PDF safely

Ogent never overwrites or edits an original PDF directly. Copy the PDF, convert
the copy to DOCX, edit and validate the DOCX with OfficeCLI, then export a new
PDF:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '.\tools\pdf2docx.ps1' -Pdf '.\input-copy.pdf' -OutDocx '.\working.docx'
$env:OFFICECLI_NO_AUTO_RESIDENT = '1'
officecli view '.\working.docx' text
# Make the requested OfficeCLI edit, then verify both the new and old text.
officecli query '.\working.docx' 'p:contains("<new text>")'
officecli query '.\working.docx' 'p:contains("<old text>")'
officecli validate '.\working.docx'
powershell -NoProfile -ExecutionPolicy Bypass -File '.\tools\docx2pdf.ps1' -Docx '.\working.docx' -OutPdf '.\edited.pdf'
```

Word PDF Reflow is the preferred conversion engine. LibreOffice is the
automatic fallback. Image-only PDFs stop with `[SCANNED_PDF]` because they
need OCR. Complex columns, embedded fonts, and floating graphics can reflow,
so verify content and structure first; request one final rendered comparison or
edit the original design file when pixel-perfect fidelity is required. See [AIONUI-WORKFLOW.md](AIONUI-WORKFLOW.md)
for the complete agent workflow.

## Visio note

OfficeCLI 1.0.140 supports `.docx`, `.xlsx`, and `.pptx`, but not `.vsdx`. Ogent demonstrates a native editable Word drawing as the current alternative. A future OfficeCLI format-handler plugin or a separate Python `vsdx` workflow could add real Visio output.

## Safety and provenance

- No credentials, API keys, cookies, or tokens are stored in this repository.
- No installer binaries are committed.
- Machine-local source documents used during workstation setup are excluded; their integrity checks remain local and are not published.
- Research sources and execution deviations are documented in [TEST-REPORT.md](TEST-REPORT.md).

## License

Copyright © 2026 ljdstechva. Released under the [MIT License](LICENSE).

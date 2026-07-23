# Ogent workspace instructions

## Speed rules for document tasks (mandatory)
- Document tasks are SINGLE-AGENT. Never spawn a team, teammate, or subagent for
  creating/editing docx/xlsx/pptx/pdf. Reviews happen as a second sequential message
  in the same conversation, after the artifact exists.
- Never use Start-Sleep / sleep / polling loops. Check outputs directly.
- PDF is never edited directly. Pipeline: tools\pdf2docx.ps1 (Word COM first) ->
  edit the .docx with officecli -> tools\docx2pdf.ps1. Scanned/image PDFs: stop and
  report "needs OCR" honestly.
- Verify content with `officecli view <file> text` or `officecli get --json`.
  Do NOT render page images to check work; render at most once at final delivery,
  only if the user asks.
- officecli syntax unsure? Run `officecli help <format> <element>` — never guess.
- Prefer one atomic `officecli batch` over many single edits.
- Never commit or push personal documents; pushes to public repos require the
  user's explicit yes.

## Office document work

- Work single-agent for document tasks. Do not spawn a team.
- Use officecli for editable Word, Excel, and PowerPoint files. Inspect the relevant `officecli help` entry before guessing a command.
- Preserve user originals. Unless the user explicitly requests an in-place edit, create a clearly named working copy and a new final output.
- Close an officecli resident before another application reads the file. Validate final Office files and PDFs before reporting success; render once only when the user requests a final visual check.

## PDF workflow

PDFs are never edited directly. Convert the PDF to DOCX, edit the DOCX with officecli, then export a new PDF:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\tools\pdf2docx.ps1" -Pdf "<input.pdf>" -OutDocx "<working.docx>"
$env:OFFICECLI_NO_AUTO_RESIDENT = "1"
officecli view "<working.docx>" text
# Make the requested officecli edit in direct mode. Query both the old and new
# text afterward; do not trust the mutation command alone.
officecli query "<working.docx>" 'p:contains("<new text>")'
officecli query "<working.docx>" 'p:contains("<old text>")'
# Release any resident that was started before direct mode.
officecli close "<working.docx>"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\tools\docx2pdf.ps1" -Docx "<working.docx>" -OutPdf "<output.pdf>"
```

- In AionUi, set `OFFICECLI_NO_AUTO_RESIDENT=1` before officecli mutations. The live acceptance test found that auto-resident mode could report success without persisting the change; direct mode plus old/new queries prevented a false positive.
- If conversion reports `[SCANNED_PDF]`, stop and explain that OCR is required; do not pretend the image-only PDF is editable.
- Word PDF Reflow is the preferred engine. LibreOffice is an emergency fallback whose PDF import is shape based and may not expose normal paragraphs to officecli.
- Reflow is not pixel-perfect for complex columns, floating graphics, or embedded fonts. Verify content and structure by default; if exact visual fidelity matters, ask the user for one final rendered comparison or rebuild from the source document.

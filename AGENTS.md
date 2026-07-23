# Ogent workspace instructions

## Office document work

- Work single-agent for document tasks. Do not spawn a team.
- Use officecli for editable Word, Excel, and PowerPoint files. Inspect the relevant `officecli help` entry before guessing a command.
- Preserve user originals. Unless the user explicitly requests an in-place edit, create a clearly named working copy and a new final output.
- Close an officecli resident before another application reads the file. Validate and visually inspect final Office files and PDFs before reporting success.

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
- Reflow is not pixel-perfect for complex columns, floating graphics, or embedded fonts. Render and compare every page with the source before delivery.

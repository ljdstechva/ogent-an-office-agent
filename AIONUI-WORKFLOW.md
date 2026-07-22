# AionUi + OfficeCLI Daily Workflow

## Workstation setup

- Launch **AionUi 2.1.39** from the Start menu.
- Open the cloned repository in AionUi and set the workspace to its `aionui-tests` directory.
- Select **Codex CLI** with **GPT-5.6-Sol · high**. The tested permission mode was **Agent** (operator-observed; no AionUi screen capture is published).
- Keep the conversation and Project file panel visible together. Generated files land in the workspace unless the prompt names another path.

## Create a new Office file

Tell Codex the exact filename, required structure, source data, formulas, and visual rules. Ask it to validate and render the result before it finishes.

Example prompts:

- Word: `Create status-report.docx: one page, Heading1 title, Overview, a four-risk table, and Next Steps. Bold every occurrence of "critical". Validate it and render a preview.`
- Excel: `Create budget.xlsx with Item, Qty, Unit Price, formula-based Total, a SUM grand total, a styled header, conditional formatting, and a Totals by Item chart. Validate it.`
- PowerPoint: `Create pitch.pptx with five widescreen slides, a consistent dark background, white text, and a real bar chart on slide 4. Validate it and render every slide.`
- Research: `Research three current community-solar trends using authoritative sources, then create a one-page research-brief.docx with a Heading1 title, three Heading2 sections, and linked sources.`

## Receive and edit files

1. Use **Add files** in the AionUi message composer and choose the Word, Excel, PowerPoint, or CSV file.
2. Refer to the attached filename in the prompt and state whether Codex should edit it in place or create a new output.
3. If attachment is inconvenient, give a repository-relative or absolute path, for example: `Open .\aionui-tests\t1.docx and change "Baseline Test" to "Baseline Test v2".`
4. For CSV, ask for a new `.xlsx`, name the destination explicitly, and require header styling, formulas where applicable, and a chart.

The workstation operator attested that **Add files** completed end to end for `t1.docx` and `sales.csv`. The resulting artifacts are included, but an AionUi screen capture is not published.

## Use the reusable templates

Run the following from the repository root in PowerShell. Choose unused output filenames; these examples intentionally do not overwrite existing files.

```powershell
officecli create '.\aionui-tests\new-report.docx'
officecli batch '.\aionui-tests\new-report.docx' --input '.\templates\report-with-toc.json' --stop-on-error
officecli close '.\aionui-tests\new-report.docx'
officecli refresh '.\aionui-tests\new-report.docx'

officecli create '.\aionui-tests\new-deck.pptx'
officecli batch '.\aionui-tests\new-deck.pptx' --input '.\templates\basic-deck.json' --stop-on-error
officecli close '.\aionui-tests\new-deck.pptx'

officecli create '.\aionui-tests\new-budget.xlsx'
officecli batch '.\aionui-tests\new-budget.xlsx' --input '.\templates\budget-workbook.json' --stop-on-error
officecli close '.\aionui-tests\new-budget.xlsx'
```

Replace bracketed placeholder text after replay, then run `officecli validate '<file>'` and `officecli view '<file>' issues`.

## Live preview and handoff

- `officecli watch '<file>'` starts a live browser preview. One watch is allowed per file, and it stops after an idle timeout; restart it when needed.
- Run `officecli close '<file>'` before opening the file in Word, Excel, PowerPoint, or another non-OfficeCLI reader. This flushes resident edits and releases the file.
- For a Word TOC or page-number fields, close the resident and run `officecli refresh '<file.docx>'` on Windows before final review.
- Always validate and render the final artifact. A successful command alone is not visual proof.

## Common pitfalls

- Quote every path containing spaces or selectors containing brackets, such as `'/body/p[1]'`.
- In PowerShell, put text containing `$` in single quotes so it is not treated as a variable.
- All element attributes use `--prop key=value`; there is no generic `--text` flag.
- OfficeCLI does not create `.vsdx`. The tested error is an honest unsupported-format response. Use a native Word or PowerPoint diagram for editable process flows; longer-term options are an OfficeCLI format-handler plugin or a separate Python `vsdx` workflow.
- If a multi-step build fails, inspect `officecli help <format> <verb> <element>`, fix the batch, and replay it on a fresh file with `--stop-on-error`.

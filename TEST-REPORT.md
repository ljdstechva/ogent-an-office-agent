# OfficeCLI + AionUi Workstation Test Report

**Test date:** July 22, 2026 (Asia/Singapore)

**Workspace:** repository root

**Test-artifact directory:** `aionui-tests/`
**Overall result:** **PASS WITH AUTHORIZED DEVIATIONS**

The workstation is operational end to end: AionUi launches, Codex CLI responds inside the app, OfficeCLI is available in the project, Word/Excel/PowerPoint files can be created and edited from chat, attached files can be ingested, researched content can be turned into a cited document, and final artifacts can be validated and rendered with native Microsoft Office backends. Office artifacts, formulas, structure, validation, and native renders are reproducible from the staged evidence. AionUi engine, attachment, and live-interface observations are operator-attested because no AionUi UI capture is published.

## Versions and configuration

| Component | Verified state |
|---|---|
| Windows | Windows 11 Home |
| OfficeCLI | `1.0.140` |
| AionUi | `2.1.39`; running at final audit (operator-observed) |
| AionUi installer | Downloaded from the AionUi GitHub Releases page. The local file size and SHA-256 were recorded for reproducibility; no vendor checksum or code signature was independently verified. |
| Codex CLI | `0.144.1` |
| Engine used in AionUi | **Codex CLI — GPT-5.6-Sol · high**, permission mode **Agent** (operator-observed) |
| Microsoft Office backend | Word, Excel, and PowerPoint native rendering exercised successfully |

No credentials, API keys, remote writes, or external messages were used during workstation testing.

## Source-file integrity and evidence classification

Machine-local source documents were excluded from this repository. Local SHA-256 comparisons matched the Phase 0 baseline; filenames and hashes are intentionally omitted from this public report.

- Office validation, formulas, document structure, and native renders are reproducible from staged evidence.
- AionUi engine selection, attachment routing, and live-interface behavior are operator-attested; no AionUi UI capture is published.
- Delegated approval and visual-check substitutions are authorized deviations from the original acceptance procedure.

## Phase gates

| Gate | Result | Verification evidence |
|---|---|---|
| Gate 0 — Preflight | PASS | OfficeCLI `1.0.140`; help printed; `aionui-tests` workspace created; MCP state captured. |
| Gate 1 — CLI baseline | PASS | `t1.docx`, `t2.xlsx`, and `t3.pptx` created and validated. The watch page contained both `Baseline Test` and `LIVE-EDIT-MARKER-777` after the live edit. |
| Gate 2 — AionUi install | PASS WITH AUTHORIZED DEVIATION | Version `2.1.39` was downloaded from the project's GitHub Releases page; a local hash was recorded, but no vendor checksum/signature was independently verified. The delegated installer approval replaced the original user-confirmation pause. |
| Gate 3 — AionUi configuration | PASS (operator-attested) | Codex CLI selected; `GPT-5.6-Sol · high`; trivial `reply OK` test succeeded; AionUi terminal returned OfficeCLI `1.0.140`; workspace set to `aionui-tests`. |
| Gate 4 — T1–T7 matrix | PASS WITH OPERATOR ATTESTATION | All required artifact checks passed; AionUi-only interaction claims are operator-attested. Exact evidence is below. |
| Gate 5 — Flagship and design control | PASS WITH AUTHORIZED DEVIATION | Six-page flagship report validated and opened in real Word; TOC, page fields, styles, table, chart, and equation verified. Codex performed the delegated visual check. Deck theme/background and workbook conditional formatting verified. |
| Gate 6 — Hardening | PASS | Three JSON templates replayed on fresh files and validated; workflow guide written; MCP state recorded. |

## T1–T7 test matrix

| Test | Result | Exact evidence |
|---|---|---|
| T1 — Word from AionUi | PASS | Operator-attested AionUi route; independently validated `status-report.docx` has one Heading 1 title, Overview/Risks/Next Steps content, one risk table, and two bold `critical` runs. The final table has five rows: one header plus four risks after T7. Native render: `status-report-qa.png`. |
| T2 — Excel formulas and chart | PASS | Operator-attested AionUi route; independently validated `budget.xlsx` has `D2` formula `B2*C2` evaluated to `65000`, `D7` formula `SUM(D2:D6)` evaluated to `362400`, and one native column chart with five categories. Native render: `budget-qa.png`. |
| T3 — PowerPoint deck | PASS | Operator-attested AionUi route; independently validated `pitch.pptx` has five widescreen slides and one native chart. Slide 4 chart contains `2024,2025,2026` with values `2,3.5,5`; every slide background is `#071820`. All slides were visually inspected in `pitch-qa.png`; issue count `0`. |
| T4 — File receiving | PASS (operator-attested UI route) | **AionUi Add files** was the observed route for both `t1.docx` and `sales.csv`; resulting artifacts are included and independently validated. `t1.docx` contains `Baseline Test v2`. `sales-from-csv.xlsx` contains one header plus ten data rows and a ten-point line chart. |
| T5 — Research to document | PASS | Operator-attested AionUi route; independently validated `research-brief.docx` has one Heading 1, exactly three Heading 2 trend sections, 250 words, and four cited authoritative sources. It renders on one page in `research-brief-qa.png`. |
| T6 — Visio gap and alternative | PASS | `officecli create test.vsdx` exited `1`, created no file, and returned the exact unsupported-format message quoted below. `process-flow.docx` contains an editable native anchored drawing whose extracted labels are `Intake`, `Review`, `Approved?`, `Archive`, `Revise`, `Review again`, `Yes`, and `No`; it validates and renders in `process-flow-qa.png`. |
| T7 — Round-trip edit | PASS | `status-report.docx` title color is `#1B5E20`; the risk table has one header plus four data rows, including Weather delays. The document remains one page and validated after the targeted edit. |

### T4 answer: can AionUi receive files?

**Yes, based on operator-attested UI observation.** The tested route was AionUi's **Add files** button. It accepted a Word document for in-place proofreading/editing and a CSV for conversion into a new formatted workbook. The resulting artifacts are published, but no AionUi UI capture is included. Absolute-path reference remains the documented fallback.

### T5 authoritative sources

The research brief cites and uses these official sources:

1. [National Laboratory of the Rockies — Community Solar](https://www.nlr.gov/state-local-tribal/community-solar)
2. [U.S. Department of Energy — Clean Energy Connector](https://www.energy.gov/cmei/systems/clean-energy-connector)
3. [New Jersey Board of Public Utilities — March 5, 2026 action](https://www.nj.gov/bpu/newsroom/2026/approved/20260305.html)
4. [Illinois Power Agency — 2026–27 Illinois Shines opening](https://ipa.illinois.gov/announcements/press-release--state-solar-incentive-program-illinois-shines-ope.html)

## Visio verdict

OfficeCLI does not currently create `.vsdx`. The exact verified response was:

```text
Error: Unsupported file type: .vsdx. Supported: .docx, .xlsx, .pptx, or any extension served by an installed format-handler plugin that implements `create`.
```

The delivered alternative is a native diagram in Word (`process-flow.docx`), which preserves a usable Office workflow and renders correctly. The corresponding PowerPoint approach uses the same native-shape model. Longer-term real-Visio options are an OfficeCLI format-handler plugin or a separate Python workflow using a `vsdx` library; neither was implemented because `.vsdx` support was explicitly out of scope.

## Flagship report verification

Artifact: `aionui-tests/flagship-report.docx`

Replay batch: `aionui-tests/flagship-report-batch.json`

Visual proof: `aionui-tests/flagship-report-qa.png`

| Requirement | Evidence | Result |
|---|---|---|
| Cover | 28 pt Aptos Display title, subtitle, author/date, brand colors, and page break | PASS |
| TOC | Levels `1-2`, `hyperlinks=true`, `pageNumbers=true`; Word refresh produced entries for four H1 and nine H2 headings with pages 3–6 | PASS |
| Styles | Four Heading 1 and nine Heading 2 paragraphs; Aptos Display headings and Aptos body; blue/teal theme | PASS |
| Header/footer | Running header plus `Page [PAGE]`; page numbers visible in native Word render | PASS |
| Body | 990 words across four substantive sections | PASS |
| Table | One fixed-layout, 5×4 milestone table, width `7200`, repeated header, no row splitting | PASS |
| Chart | Native two-series line chart; Plan `100,85,70,40,15`; Actual `100,80,65,35,10` | PASS |
| Equation | Display equation `CV = EV - AC` | PASS |
| Pagination and visual QA | Six pages; all pages inspected in the native Word contact sheet and the document opened successfully in real Word | PASS |
| Package validation | OpenXML validation passes; no severity-2 issue findings | PASS |

The sole issue advisory is the empty paragraph used as the native display-equation anchor. It is severity 1 and is not a format or structure error. During the final audit, validation initially returned an I/O error while the real-Word inspection held the file open; after Word was closed, validation passed again.

## Design controls in PowerPoint and Excel

- `pitch.pptx` has an explicit deck theme of Aptos Display/Aptos, brand accents `#2DE38C` and `#4DA3FF`, and a consistent `#071820` background on all five slides. It validates with zero issues.
- `budget.xlsx` retains its bold dark-blue header, formula-based totals, and native chart, and now has a verified data-bar conditional-formatting rule on `D2:D6`. It validates with zero issues.

## Reusable templates and replay results

| Template | Operations | Fresh replay artifact | Verification |
|---|---:|---|---|
| `templates\report-with-toc.json` | 50 | `template-report-test.docx` | Six pages; live TOC, header/footer, 4 H1, 5 H2, table, chart, equation; validation PASS. |
| `templates\basic-deck.json` | 20 | `template-deck-test.pptx` | Three widescreen branded slides; all slides rendered and inspected; validation PASS, issue count 0. |
| `templates\budget-workbook.json` | 38 | `template-budget-test.xlsx` | Five formula rows, evaluated grand total `115800`, data bars, and one column chart; validation PASS, issue count 0. |

One-line replay commands and placeholder-editing guidance are documented in `AIONUI-WORKFLOW.md`.

## Final Office artifact audit

Every delivered Office artifact was revalidated after all edits. `Errors` counts severity-2-or-higher issue findings; all are zero.

| Artifact | OpenXML validation | Advisories | Errors |
|---|---:|---:|---:|
| `t1.docx` | PASS | 2 | 0 |
| `t2.xlsx` | PASS | 0 | 0 |
| `t3.pptx` | PASS | 0 | 0 |
| `status-report.docx` | PASS | 6 | 0 |
| `budget.xlsx` | PASS | 0 | 0 |
| `pitch.pptx` | PASS | 0 | 0 |
| `sales-from-csv.xlsx` | PASS | 0 | 0 |
| `research-brief.docx` | PASS | 6 | 0 |
| `process-flow.docx` | PASS | 1 | 0 |
| `flagship-report.docx` | PASS | 1 | 0 |
| `template-report-test.docx` | PASS | 1 | 0 |
| `template-deck-test.pptx` | PASS | 0 | 0 |
| `template-budget-test.xlsx` | PASS | 0 | 0 |

The Word advisories are only first-line-indent suggestions on short operational documents plus the equation-anchor paragraph in each long report. Native renders show no clipping, overlap, broken chart, or blank output.

## MCP final state

`officecli mcp list` returned:

- Claude Code: registered
- LM Studio: not registered
- Cursor: not registered
- VS Code: not registered

OfficeCLI does not expose a separate Codex/AionUi MCP registration target in this version. AionUi successfully drove OfficeCLI through Codex CLI, the installed OfficeCLI skill, and the inherited user PATH, so optional MCP registration was not a blocker.

## Deviations and fixes

1. **Engine changed by explicit user instruction.** The original objective named Claude Code as primary. The user later directed that testing use Codex, not Claude, and permitted GPT-5.6-Sol. All AionUi tests therefore used Codex CLI with GPT-5.6-Sol at high reasoning. Claude was not used for test execution.
2. **Approval pauses were superseded by standing authorization.** The user explicitly authorized local approvals and asked the work to continue until completion. This is an authorized deviation from the original acceptance procedure. The installer source and local hash were checked before execution, but no vendor checksum or code signature was independently verified. No login or credential entry was performed.
3. **Flagship fallback was required.** The first AionUi/Codex attempt encountered OfficeCLI resident-persistence/schema problems and left an incomplete file. The agent detected the blank output, stopped that attempt, rebuilt the report with a 75-operation atomic OfficeCLI batch, refreshed it through Word, and revalidated it. This direct-CLI fallback was allowed by Phase 5 and is preserved as `flagship-report-batch.json`.
4. **Optional MCP integration was not added.** There is no AionUi/Codex registration target in `officecli mcp`; shell plus skill integration passed every required test.
5. **Visual confirmation was performed by Codex.** The user delegated approval and instructed uninterrupted completion, so the real-Word visual checks were carried out directly and recorded through native QA renders. AionUi attachment and engine-selection claims remain operator-attested because no AionUi UI capture is published.

## Five-line daily use

1. Open AionUi, choose the `aionui-tests` project, and select **Codex CLI — GPT-5.6-Sol · high**.
2. Name the output file and specify structure, data, formulas, sources, and visual rules in the prompt.
3. Use **Add files** for an existing Office file or CSV; say clearly whether to edit in place or create a new file.
4. Require `officecli validate`, `officecli view <file> issues`, and a native render before accepting the result.
5. Run `officecli close '<file>'` before opening it in Office; for Word TOCs/page fields, also run `officecli refresh '<file.docx>'`.

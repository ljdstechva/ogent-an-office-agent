# Ogent Lite v0.10.1 release notes

Status: released. Until the compatible viewer changes land upstream, use the
verified [OfficeCLI 1.0.143 Ogent viewer preview fork prerelease](https://github.com/ljdstechva/OfficeCLI/releases/tag/v1.0.143-ogent-preview)
or a compatible upstream OfficeCLI 1.0.143-or-later release.

## Highlights

- Live Word, Excel, and PowerPoint updates preserve the latest viewport the user
  chose, including movement made while an agent is still working.
- Normal completion, provider error, and Stop no longer reload the preview
  iframe merely because the document revision or run state changed.
- Submitted preview-selection tags are accessible buttons. Clicking one centers
  its exact current target and shows a temporary gold viewer-only highlight.
- Multiple submitted tags replay independently without changing current
  composer selection, draft text, immutable chat history, provider-neutral
  memory, or the Office package.

## Security and failure behavior

- Historical focus accepts only a submitted message sequence and selection ID
  from the browser. Canonical session memory supplies every path, document,
  format, kind, revision, and text fingerprint.
- Cross-session, cross-document, unknown, moved, missing, and ambiguous targets
  fail closed. Conservative relocation is allowed only when exactly one target
  matches.
- OfficeCLI is invoked with argument arrays and no shell. The browser cannot
  submit a file path, OfficeCLI path, CSS selector, URL, or watch port.
- Ogent removes only its exact historical mark ID, preserving unrelated marks
  and the current teal preview selection.

## Compatibility

Ogent 0.10.1 requires OfficeCLI 1.0.143 or later. That viewer contract provides
semantic viewport anchoring plus public format-neutral `watch goto` and exact
mark-ID cleanup. Ogent checks the installed version before starting a watch and
reports an actionable error on older builds.

The temporary fork prerelease is built by public GitHub Actions from commit
`6f6100f4152e630883b3c44fdfc35b144c6942b0`. Its Windows x64 asset has SHA-256
`F32C6AF1B1AA1ACC70E4128B5E0BED9CA3EF01565DD986DCFD23E704FB0AE6E1`;
verify it against the release's `SHA256SUMS`. The clean seven-file viewer patch
is proposed upstream in [OfficeCLI PR #268](https://github.com/iOfficeAI/OfficeCLI/pull/268).
The fork's macOS assets are unsigned and unnotarized; v0.10.1 acceptance and
installation used the Windows x64 asset.

Opening another document, explicitly repairing a dead preview, or restarting a
dead watch remains a legitimate iframe-navigation boundary.

OfficeCLI navigation is watch-scoped. Separate Ogent document sessions remain
isolated because they own separate watches, but multiple browser tabs attached
to one session/watch move together when a historical tag is focused. The
current OfficeCLI watch protocol does not expose client-scoped navigation.

Excel ranges of at most 100 cells are marked cell by cell. Larger ranges center
and highlight their primary top-left cell only, keeping navigation bounded.

## Verification

- 148 deterministic test methods and 56 executed subtests passed.
- Real Codex `gpt-5.6-sol` edits passed in synthetic Word, Excel, and PowerPoint
  files with zero iframe loads and exact retained manual positions.
- Word, Excel, and PowerPoint historical targets finished within 49-51% of
  preview height with a gold highlight.
- All three edited Office files validated with zero OfficeCLI errors; every
  verified recovery copy matched its pre-edit SHA-256.
- Headed Microsoft Edge checks passed at 1440x900 and 390x844 with no horizontal
  application overflow or mobile transcript/composer overlap.
- A fresh three-format run against the downloaded public Windows x64 asset
  retained Word `/body/p[41]` at `scrollY=5970`, Excel `Sheet1!J225` at wrapper
  scroll `(315,4189)`, and PowerPoint slide 12 at `scrollTop=9037` exactly
  across edits. Off-screen historical targets then centered at `50.05%`,
  `(49.11%,50.49%)`, and `49.96%`, respectively, with gold marks and unchanged
  package hashes.

Detailed evidence is in
[OGENT-REPORT.md](OGENT-REPORT.md).

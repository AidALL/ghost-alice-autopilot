# ghost-alice-autopilot v0.1.2 Release Notes

Date: 2026-08-17

Scope: `v0.1.2` aligns the autopilot addon with Ghost-ALICE core `v0.2.2`, strengthens project-root and permission behavior, makes decision promotion status-aware, isolates all child scratch state, and prepares evaluator-visible diagnostics for separate hidden-purpose core evaluation.

Status: this file is the release-body source of truth for `v0.1.2`; the published release body must remain synchronized with this file and `CHANGELOG.md`.

## Main Changes

- Raised the minimum Ghost-ALICE core version to `0.2.2` and synchronized the addon and repository versions at `0.1.2`.
- Rejected explicit relative project roots, ignored relative derived candidates, and kept permission failures fail-soft only for implicitly derived run directories and locks.
- Prevented `promote-decision` from writing actions when the effective decision is incompatible with the target work-item status, including ready-item reopen attempts and missing targets.
- Added repository-local project runtimes and a canonical pytest launcher that owns temp, Python cache, clean child working directories, failure diagnostics, and successful cleanup.
- Preserved full continuation patterns without character-count truncation and resolved relative Codex last-message paths before clean-CWD execution.
- Kept Claude and Codex live diagnostic prompt transport symmetric while labeling this harness evaluator-visible; hidden-purpose installed behavior evidence is produced by the core blind controller.
- Replaced the fixed Claude live-diagnostic budget with a positive finite `--claude-max-budget-usd` per-scenario cap whose default is `1.00`.
- Unified the live scenario response contract and accepted one expected-key fenced JSON result around required governance control blocks while rejecting ambiguous candidates.
- Emitted live diagnostic JSON as UTF-8 under legacy Windows stdout encodings so Unicode model text cannot terminate an otherwise successful run.
- Removed display-width prose wrapping and shipped the same structural Markdown wrapping gate as core.

## Verification Surface

- Run `python scripts/run_project_tests.py tests` for the complete addon suite and package contract.
- Run the addon Markdown wrapping checker and compatibility-surface contract after all release files are in the Git index.
- Install core `v0.2.2` plus addon `v0.1.2` for both Claude and Codex, then run evaluator-visible diagnostics and the separate core-owned blind cases.

## Compatibility Boundary

- Ghost-ALICE core `v0.2.2` is the minimum supported core for this addon release.
- Claude Code and Codex are `verified-local` through installed status, credentialed live semantic diagnostics, and five purpose-hidden fresh-session cases per platform.
- Platform claims remain bounded by `compatibility-matrix.json` and fresh installed-session evidence.

## Release Boundary

- The Git tag, changelog section, release body, addon manifests, installed sidecars, and both platform adapters must identify `v0.1.2` before publication is considered complete.

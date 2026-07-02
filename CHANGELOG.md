# Changelog

All notable public changes to `ghost-alice-autopilot` should be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

Use this section for changes that have landed after the latest tagged public release.

## [0.1.1] - 2026-07-01

### Added

- Live semantic Codex E2E unittest coverage for stdin prompt delivery, hook-trust flag support, runtime config failures, and Windows command shim resolution.
- Release package tracking for the live semantic unittest surface.
- Admitted acceptance-criterion bootstrap from current session intent into approved `.autopilot` run state when explicit approval evidence exists.
- Validated criterion met-flip integration that marks admitted session-intent criteria as met after a criterion-bound `continue_next` completion.
- Reset-N io-trace resume budget coverage for approved autopilot runs that lack a promoted consistency decision.
- Platform-neutral continuation-signal rendering for structured Bash io-trace rows, source locators, and allowed surfaces.
- Invalid consistency-decision quarantine coverage that preserves the rejected file as evidence while keeping fail-closed validation.

### Changed

- Codex live semantic smoke now resolves `.cmd` shims and runtime-supported flags more defensively across Windows and non-Windows command surfaces.
- Prompt execution now feeds substantive prompts through stdin instead of depending on argv prompt handling.
- GitHub Actions now runs the addon suite through `python -m unittest discover` so CI matches the release verification contract.
- Stop-hook continuation now replenishes the io-trace resume allowance only when the session-intent ledger advances, then escalates to `ask_user_meta` when the reset budget is exhausted.
- Continuation messages now emit portable project/home-relative paths while stored work items and raw io-trace audit records remain absolute.
- The public compatibility boundary now names Ghost-ALICE core `v0.2.1` as the supported core floor for the `v0.1.1` addon contract.
- Autopilot no longer auto-attaches to a session on io-trace material alone; run bootstrap now requires admitted, unmet acceptance criteria plus explicit approval evidence.

### Fixed

- Prevented config/runtime errors and unsupported hook-trust flags from being misclassified as governance pass/fail outcomes.
- Kept compatibility-surface CI aligned with the files shipped by the addon.
- Prevented an approved run with persistent io-trace material and no promoted decision from re-firing the same continuation forever.
- Prevented an invalid promoted decision file from re-raising forever by moving it to `consistency-decision.rejected.json` before preserving the validation error.
- Prevented release documentation from retaining pre-publication staging language after the public `v0.1.1` release exists.

## [0.1.0] - 2026-06-22

### Added

- Release-prep notes under `docs/release/2026-06-22-release-notes.md`.
- `autopilot-mode` privileged adapter package with approval-gated `.autopilot/` state, task queue handling, continuation events, and Stop-event continuation payloads.
- Session-intent bridge surfaces that can materialize approved `.autopilot/` run state from Ghost-ALICE session intent ledgers after explicit approval.
- Governance signal and promotion flow for diagnostic candidates, promoted consistency decisions, approved conduct plans, and candidate-only boundary checks.
- Compatibility matrix SSOT covering macOS, Linux, Windows shells, Claude Code, and Codex support posture.
- Fresh install E2E and local compatibility tests covering installed skill layouts, adapter smoke behavior, release package tracking, and documented support surfaces.

### Changed

- Public docs now state that install commands run from the Ghost-ALICE core checkout; this addon repository does not provide a standalone root installer.
- Compatibility matrix evidence now describes the current support contract rather than retaining dated run logs.
- Release package tests now check tracked release surfaces instead of shallow source-string fixtures.

### Fixed

- Stop continuation now reuses existing session-intent, io-trace, and governance signal receptors instead of naturally stopping when unresolved runtime material exists.
- Candidate files remain diagnostic-only; adapter-consumable action files require explicit promotion.
- Claude support remains scoped as `simulated-local` until a credentialed Claude live semantic run passes, preventing an overbroad compatibility claim.


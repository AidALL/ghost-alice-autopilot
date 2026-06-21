# Changelog

All notable public changes to `ghost-alice-autopilot` should be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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


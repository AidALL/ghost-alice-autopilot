# ghost-alice-autopilot v0.1.1 Release Notes

Date: 2026-07-01

Scope: current `main` after `v0.1.0`, including Codex live semantic smoke hardening, reset-N autopilot resume budgeting, platform-neutral continuation signals, and invalid-decision quarantine.

Status: release documentation prepared for tag `v0.1.1`; GitHub Release publication follows the branch, PR, CI, and merge flow.

## Main Changes

- Hardened `scripts/live_semantic_e2e.py` so Codex live semantic runs detect config failures, command shim resolution issues, unsupported hook-trust flags, and missing prompt output without false pass/fail classification.
- Fed live prompts through stdin for more reliable high-context Codex prompt execution.
- Added unittest coverage for command resolution, config failure classification, stdin prompt handling, and runtime flag compatibility.
- Added the live semantic unittest module to the tracked release package contract.
- Added a reset-N io-trace resume budget: an approved run may retry a decision-less running item for a bounded number of io-trace-backed stops, replenished only when the session-intent ledger advances.
- Rendered Stop-hook continuation signals with portable project/home-relative paths for Bash rows, source locators, and allowed surfaces while preserving absolute/raw values in stored state and local audit logs.
- Quarantined invalid promoted consistency-decision files to `consistency-decision.rejected.json` before preserving the fail-closed validation error, preventing permanent re-raise loops.

## Verification Surface

- Local unit verification should run `python -m unittest discover -s tests -p "test_*.py"`.
- Focused adapter verification should include `tests/test_autopilot_state.py`, `tests/test_autopilot_messages.py`, and `tests/test_autopilot_session_bridge.py`.
- Live semantic verification should run `python scripts\live_semantic_e2e.py --runtime codex --scenario-source static --execute --jsonl` against the installed core plus addon runtime.
- GitHub Actions must pass on the release PR before publishing `v0.1.1`.

## Compatibility Boundary

- Codex live semantic smoke is the verified runtime target for this patch release.
- Claude support remains bounded by the compatibility matrix and does not become a full live compatibility claim through this release note.
- Linux and Windows support posture remains governed by `compatibility-matrix.json`; this release hardens Windows command resolution but does not claim a full Windows live run unless that matrix says so.

## Release Boundary

- This note does not create the tag or GitHub Release by itself.
- Use this note and `CHANGELOG.md` as the release body source for `v0.1.1`.

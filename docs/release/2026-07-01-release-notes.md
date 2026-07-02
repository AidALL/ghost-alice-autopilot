# ghost-alice-autopilot v0.1.1 Release Notes

Date: 2026-07-01

Scope: published `v0.1.1` changes after `v0.1.0`, including Codex live semantic smoke hardening, admitted acceptance-criterion bootstrap, validated criterion met-flips, reset-N autopilot resume budgeting, platform-neutral continuation signals, and invalid-decision quarantine.

Status: `v0.1.1` is the public patch release for the Ghost-ALICE core `v0.2.1` compatibility window. The local document is the release-body source of truth for the addon repository; if the GitHub Release body diverges, update the published body from this file.

## Main Changes

- Hardened `scripts/live_semantic_e2e.py` so Codex live semantic runs detect config failures, command shim resolution issues, unsupported hook-trust flags, and missing prompt output without false pass/fail classification.
- Fed live prompts through stdin for more reliable high-context Codex prompt execution.
- Added unittest coverage for command resolution, config failure classification, stdin prompt handling, and runtime flag compatibility.
- Added the live semantic unittest module to the tracked release package contract.
- Added a reset-N io-trace resume budget: an approved run may retry a decision-less running item for a bounded number of io-trace-backed stops, replenished only when the session-intent ledger advances.
- Added admitted acceptance-criterion bootstrap, replacing the previous io-trace-presence auto-attach heuristic (behavior change): the adapter no longer attaches to a session on io-trace material alone; it materializes a project-local `.autopilot` run only when session intent contains admitted, unmet acceptance criteria and explicit approval evidence exists.
- Added validated criterion met-flips: a promoted `continue_next` decision with criterion-bound `[completion-check]` evidence marks the corresponding admitted session-intent criteria as met through the core ledger API when that API is reachable.
- Rendered Stop-hook continuation signals with portable project/home-relative paths for Bash rows, source locators, and allowed surfaces while preserving absolute/raw values in stored state and local audit logs.
- Quarantined invalid promoted consistency-decision files to `consistency-decision.rejected.json` before preserving the fail-closed validation error, preventing permanent re-raise loops.
- Hardened the Stop adapter against cross-run/session contamination by binding run-state continuation to the approved project/run surface and rejecting foreign promoted decision material.

## Verification Surface

- Local unit verification should run `python -m unittest discover -s tests -p "test_*.py"`.
- GitHub Actions uses the same `python -m unittest discover` addon suite instead of a pytest wrapper.
- Focused adapter verification should include `tests/test_autopilot_state.py`, `tests/test_autopilot_messages.py`, and `tests/test_autopilot_session_bridge.py`.
- Live semantic verification should run `python scripts\live_semantic_e2e.py --runtime codex --scenario-source static --execute --jsonl` against the installed core plus addon runtime. A semantic mismatch, hook-incomplete run, timeout, auth/config blocker, or inference failure is a non-zero harness result.
- GitHub Actions passed before publication of `v0.1.1`; future patch edits must rerun the same unittest and live semantic surfaces before updating the published release body.

## Compatibility Boundary

- Codex live semantic smoke is the verified runtime target for this patch release.
- Ghost-ALICE core `v0.2.1` or newer is the supported core floor for the `v0.1.1` addon contract. Older core versions may lack the runtime-core audit, ledger met-flip, and privileged-adapter install semantics required by this release.
- Claude support remains bounded by the compatibility matrix and does not become a full live compatibility claim through this release note.
- Linux and Windows support posture remains governed by `compatibility-matrix.json`; this release hardens Windows command resolution but does not claim a full Windows live run unless that matrix says so.

# Ghost-ALICE Autopilot v0.3.0 Release Notes

Date: 2026-09-26

Language: English | [한국어](https://github.com/AidALL/ghost-alice-autopilot/blob/v0.3.0/docs/ko/release/2026-09-26-release-notes.md)

This release aligns the Autopilot product version with Ghost-ALICE core `0.3.0` and makes continuation depend on the selected session's current intent before pending state changes are applied. This file is the source for the GitHub release body; `VERSION`, the addon manifest, and the changelog identify the same product release.

## What Changed

- Current intent is checked before consuming pending completion receipts, importing conduct plans, or changing a work queue. A changed objective or missing/conflicting selected context parks the run before those actions.
- Hosts using `agent-runtime` must explicitly provide `GHOST_ALICE_PLATFORM=agent-runtime`, `GHOST_ALICE_SESSION_ID`, and an absolute `GHOST_ALICE_SESSION_INTENT_ROOT`. The selected `session-intent-ledger.v1` record must match that platform and session. Unknown platforms and invalid selected context do not borrow a native or sibling ledger.
- Ordinary refinements within an approved objective retain approval. Valid completion receipts are consumed once; reentry does not repeat the completed transition.
- English and Korean guidance now agrees on installation, continuation, compatibility, and the recommended release pair. Apache-2.0 licensing is unchanged.

## Compatibility and Upgrade

Use Ghost-ALICE core `0.3.0` with Autopilot `0.3.0` for the coordinated intent-record and session-binding changes. The existing installer compatibility floor remains core `0.2.2`; it is distinct from the recommended pair. Internal schema versions remain unchanged.

Run installation from the core checkout. This addon has no standalone root installer:

```bash
bash install.sh --addon autopilot --addon-tag v0.3.0
bash install.sh --status
```

The official installer targets Claude Code and Codex. The explicit `agent-runtime` adapter contract lets another host supply session intent, but that host must still implement model access, tools, approval boundaries, and event dispatch. This release does not add those capabilities to arbitrary models by installation alone.

## Verification and Limits

- The installed adapter was replayed against 238 frozen cases derived from supplied and local intent records. All 238 passed, including the four valid receipt cases that the earlier strengthened validator rejected; 797 assertions checked the recorded results.
- The complete addon suite passed with 276 tests and 377 subtests for the merged runtime changes. Regression coverage includes missing roots, declared-session mismatches, changed goals, OFF state, invalid context before mutation, and single-use receipt handling.
- These are bounded replay and regression results, not a general intent-understanding accuracy estimate. The replay cases are derived from recorded states and are not 238 independent user sessions.
- This release's ledger validation did not include a fresh Claude model-inference run. Earlier live Claude/Codex evidence in the compatibility matrix is not relabeled as fresh `0.3.0` evidence. Linux and Windows gaps in that matrix still prevent a full cross-platform compatibility claim.

See [compatibility-matrix.json](https://github.com/AidALL/ghost-alice-autopilot/blob/v0.3.0/compatibility-matrix.json) and the [core release notes](https://github.com/AidALL/ghost-alice/blob/v0.3.0/docs/release/2026-09-26-release-notes.md) for the respective verification boundaries.

## License

Ghost-ALICE Autopilot remains open source under [Apache-2.0](https://github.com/AidALL/ghost-alice-autopilot/blob/v0.3.0/LICENSE). The coordinated version does not change the license.

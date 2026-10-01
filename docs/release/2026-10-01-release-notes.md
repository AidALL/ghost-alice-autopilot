# Ghost-ALICE Autopilot v0.4.1 Release Notes

Language: English | [Korean](../ko/release/2026-10-01-release-notes.md)

The coordinated release pair is Core 0.4.1 and Autopilot 0.4.1. Autopilot's technical minimum remains Core 0.4.0; internal schema and catalog versions keep their existing identities. Both repositories retain Apache-2.0 licensing.

## Changes

- Select execution state for the exact current session and preserve foreign-session state.
- Share validated run selection across bootstrap, runtime provenance and completion publication.
- Preserve strict authority checks for explicit run-directory overrides; do not redirect an explicit run to another session.
- Handle permission errors and read-only filesystems at derived directory creation or lock acquisition without hiding later state-processing errors.
- Keep pause, approval generation and completion evidence bound to the selected run.

## Update

Update and reinstall Core and Autopilot together. Preserve existing session databases, user-owned files and undecided merge records. The Codex bootstrap and managed full governance file install together; status and doctor check that dependency.

## Verification Scope

The release uses repository CI and applicable local equivalents. Selected independent gpt-6.1-sol Codex cases covered original-like completion reporting, continuing approved work and actual scoped task execution. Claude guidance installation was checked without a fresh Claude inference run. Each case supports its own observed scope. These observations do not establish universal reliability, a measured causal effect size or quantified token reduction.

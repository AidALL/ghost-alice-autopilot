# ghost-alice-autopilot Release Prep Notes

Date: 2026-06-22

Scope: current `main` through `5e01c59b04c7e257db623cd6c7764154d87aa730`.

Status: release-prep complete; tag and GitHub Release publication are separate follow-up actions.

## Main Changes

- Packaged `autopilot-mode` as the official Ghost-ALICE autonomous-continuation addon.
- Added the privileged adapter path that reads approved `.autopilot/` run state, resumes unresolved running items when io-trace material exists, and emits continuation payloads.
- Added the session-intent bridge for Codex and Claude session ledger material after explicit approval evidence.
- Added governance signal candidate and promotion flows for consistency decisions and conduct plans.
- Hardened release evidence surfaces: compatibility matrix policy, tracked release package coverage, installed bridge layout checks, and docs that separate release support posture from full compatibility claims.
- Removed the shallow fresh-install harness fixture after replacing it with direct fresh-install E2E evidence.

## Verification Evidence

- Local autopilot suite during release prep: `144 passed, 211 subtests passed`.
- Targeted compatibility surface: `20 passed, 178 subtests passed`.
- Fresh install E2E: Claude and Codex install/status ok; adapter marker count `1`; legacy marker counts `0`; installed smoke emitted `continue_next_item` for both platforms.
- Temporary HOME install/uninstall verification: Codex install/uninstall ok; Claude install/uninstall ok.
- GitHub Actions on `main` at `5e01c59b04c7e257db623cd6c7764154d87aa730`: `CI` succeeded.

## Compatibility Boundary

- macOS and Codex are locally verified according to `compatibility-matrix.json`.
- Claude Code remains `simulated-local` until credentialed Claude live semantic E2E succeeds.
- Linux and Windows targets remain `not-run`, so the release must not claim full compatibility.

## Release Boundary

- This note does not create a tag or GitHub Release.
- Before publication, choose the release version and use this note plus `CHANGELOG.md` as the release body source.
- Keep `compatibility-matrix.json` as the support-posture SSOT; do not move dated run logs back into matrix evidence.

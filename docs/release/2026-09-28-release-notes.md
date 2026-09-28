# Ghost-ALICE Autopilot v0.4.0 Release Notes

Language: English | [Korean](../ko/release/2026-09-28-release-notes.md)

## Changes

- Move approved runs, tasks, events, consumed inbox receipts and approval history to SQLite. Bound runs share Core transactions; standalone runs retain a separate database.
- Bind admission and evidence to exact session/input identity, criterion definitions and approval generation. Preserve progress, budget and OFF for the same approved contract; archive superseded generations.
- Capture prospective provenance before tools, surface current preparation commands once, and publish unchanged proof before the initial final answer. Original source, time and proof digest remain bound through recovery.
- Keep explicit current-input reapproval attached to the user task instead of substituting an advisory conduct plan. Recovery reuses valid business evidence and preserves the user's requested answer.
- Ignore unrelated-session databases during pre-tool provenance discovery, while retaining errors for invalid current-session authority. Bind CI to the paired Core runtime and verify both registered hook events.

## Compatibility and upgrade

Autopilot 0.4.0 requires Core 0.4.0 or newer for shared SQLite APIs. Update and reinstall both packages together. Older installers gaining adapter support in 0.2.2 does not make their runtime sufficient for this release. Stop old writers before migration and preserve a consistent backup; migrated JSON files are no longer authoritative. Internal schema versions remain independent of product versions.

## Verification and limits

The delivered implementation's selected isolated evaluations observed initial completion publication, correct target and current-input handling, and no storage-only repeat inspection. Original failed attempts and evaluation limitations remain preserved. These observations do not establish universal reasoning quality or eliminate host UI duplication when a Stop hook rejects an answer.

Run the repository CI against the paired Core checkout. Existing platform evidence in compatibility-matrix.json is not relabeled as a fresh run for this version. Git tags and hosted release publication are separate from this source-version update.

## License

The project remains open source under Apache-2.0.

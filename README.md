# ghost-alice-autopilot

<p align="center">
  <img src="./logo/logo_inward_fade.png" alt="Ghost-ALICE Autopilot logo" width="360">
</p>

Official Ghost-ALICE addon for continuing approved work and checking its completion criteria.

Language: English | [Korean](./README_ko.md)

`autopilot-mode` helps Ghost-ALICE continue approved work. After an agent response ends, it checks the current request, approved scope, and completion records before selecting the next item or resuming unfinished work. It respects user approval and stop state, and checks completion criteria before moving on.

This repository provides the addon for Claude Code and Codex. You need Ghost-ALICE core and the corresponding host runtime first. Installation, a runnable example, and pause/stop controls are described below.

Internally, the addon reads the project's `.autopilot/` state after an agent stop event, chooses a `ready` or `reopened` item, or resumes an unresolved `running` item when current io-trace material exists, then emits a continuation message.

Current source version: `0.4.1`, paired with Ghost-ALICE core `0.4.1`. See the [release notes](./docs/release/2026-10-01-release-notes.md), [GitHub release](https://github.com/AidALL/ghost-alice-autopilot/releases), and [Ghost-ALICE website](https://aidall.github.io/ghost-alice/). Both projects remain open source under Apache-2.0.

## Quick Install

From the cloned Ghost-ALICE core repository, install Core and Autopilot together with automatic agent-platform detection:

```bash
bash install.sh --addon autopilot
```

See the [detailed installation guide](https://github.com/AidALL/ghost-alice/blob/main/docs/getting-started/installation.md) for cloning, native Windows commands and platform-specific options.

## What This Addon Does

- Installs the `autopilot-mode` skill.
- Registers the core-owned `[adapter:autopilot-mode] continue` hook through the Ghost-ALICE installer.
- Reads project-local run state from `.autopilot/`, preferring Claude's stable `CLAUDE_PROJECT_DIR` over a drifted hook-time `cwd`.
- Provides `skill/scripts/autopilot_session_bridge.py` plus the repository wrapper `scripts/autopilot_session_bridge.py` to bootstrap `.autopilot/` from the current session-intent ledger after explicit approval.
- Stop-hook bootstrap is session-lineage bounded: an explicit hook session id cannot fall back to an older `current-session.json` pointer from another session.
- Lets the Stop adapter materialize current-session `.autopilot/` state without adding a separate receptor when session intent records admitted, unmet acceptance criteria, or when open conduct feedback provides an approved conduct plan; io-trace material alone never bootstraps a run and flows through the `autopilot-observation-signal.v1` receptor as observation only.
- Provides `autopilot_governance_signal.py` for evidence-backed governance candidates and promotion.
- Imports approved `conduct-plan.json` proposal queues into durable `tasks.jsonl` work items.
- Emits either a no-op payload or a next-work-item message.
- Records adapter events in `<selected-run-dir>/events.jsonl`.

It does not invent work outside the current session. Session intent analysis, task routing, the user's explicit GO decision, and current-session runtime material create the approved run state.

## How It Works

Runtime loop:

1. The Ghost-ALICE core installer installs this addon and wires the privileged adapter hook.
2. A project creates `<selected-run-dir>/approved-run.json` and `<selected-run-dir>/tasks.jsonl` after user approval. A conduct-feedback run can instead provide an approved `<selected-run-dir>/conduct-plan.json`. The package bridge `skill/scripts/autopilot_session_bridge.py` or repository wrapper `scripts/autopilot_session_bridge.py` can create that run state from the exact selected Core session ledger when the caller supplies explicit approval evidence. The Stop adapter can also materialize the current session when session intent records admitted, unmet acceptance criteria, or when an approved conduct plan is present; io-trace material alone is observation/resume material, not bootstrap approval.
3. When the agent stops, the adapter reads `.autopilot/`.
4. Governance signals first write `consistency-decision.candidate.json` or `conduct-plan.candidate.json`; those candidate files are not adapter-consumable.
5. Only promotion creates adapter-consumable `consistency-decision.json` or approved `conduct-plan.json`.
6. If `conduct-plan.json` exists, the adapter imports new proposed queue items into `tasks.jsonl` before checking for a ready task.
7. If the run is approved, running, within budget, and has a ready or reopened task, the adapter marks that task `running`.
8. If a running task is missing a promoted decision but current io-trace exists, the adapter feeds io-trace through `autopilot-observation-signal.v1` and resumes the same task.
9. The adapter prints a continuation payload with the next work item and a `before-stop` instruction to write or promote `<selected-run-dir>/consistency-decision.json` when a decision is resolved.
10. If the run is not approved, paused, stopped, out of budget, or has no runnable item or runtime material, the adapter returns a no-op payload.

Identified-session default run directory (the same selector serves pretool, completion and Stop):

```text
<project>/.autopilot/sessions/<platform>/<session-id>/
  approved-run.json
  tasks.jsonl
  conduct-plan.candidate.json
  conduct-plan.json
  conduct-plan.applied.json
  consistency-decision.candidate.json
  consistency-decision.json
  consistency-decision.applied.json
  events.jsonl
  OFF
```

The project-level legacy run remains the default only when its platform/session and known authority root match the current session, or when no session identity is supplied. A foreign legacy run is preserved. Use the checked `automatic_target.run_dir` or completion receipt `run_dir` for action inboxes and state locators. A foreign explicit override is rejected and pretool reports `ownership-conflict` without a prepare command. `<project>/.autopilot/OFF` pauses all default session runs before inspecting state; `<selected-run-dir>/OFF` pauses just that run. Both also pause completion helpers.

`GHOST_ALICE_AUTOPILOT_RUN_DIR` is the strict authoritative run-directory override, and `GHOST_ALICE_AUTOPILOT_CWD` is the next project-root override. The project-root override must be absolute; a relative value surfaces as a blocking adapter reason instead of being resolved against the process directory. Without either override, Claude hooks select the first non-empty absolute path from `CLAUDE_PROJECT_DIR` and hook input `cwd`; Codex ignores an inherited `CLAUDE_PROJECT_DIR` and selects hook input `cwd`. Both platforms then fall back to the absolute adapter process directory. Relative derived candidates are skipped, while an access failure after selection does not retry a lower-priority source. Only permission or read-only-filesystem errors raised while creating a derived `<project>/.autopilot` directory or acquiring its lock becomes the empty no-op payload; later state-processing exceptions and explicit run-directory errors still surface.

## Governance Candidates And Promotion

`addons/autopilot-mode/skill/scripts/autopilot_governance_signal.py` converts session intent, conduct feedback, routing-surface corrections, and completion validation failures into evidence-backed candidate files. A candidate file is diagnostic only:

- `consistency-decision.candidate.json` uses `schema_version: "autopilot-consistency-decision-candidate.v1"`, `promotion_state: "candidate"`, and `action_file_allowed: false`.
- `conduct-plan.candidate.json` uses `schema_version: "autopilot-conduct-plan-candidate.v1"`, `promotion_state: "candidate"`, and `action_file_allowed: false`.
- The adapter rejects candidate schemas even if a candidate is accidentally placed at an adapter-consumable path.

Promotion is the boundary that creates adapter-consumable files. `promote-decision` writes a promoted `consistency-decision.json` with `schema_version: "autopilot-consistency-decision.v1"`, `promotion_state: "promoted"`, `promotion_evidence.decision`, `promotion_evidence.source`, `candidate_id`, `governance_signal_digest`, `state_hash`, `decision_key`, and `loop_key`. `promotion_evidence.decision` accepts `go`, `approve`, `approved`, `promote`, `promoted`, or `direct`; use `direct` only for a current-turn before-stop resolution without a candidate. In every promoted decision, `evidence` must be a JSON array of strings; do not nest `verdict`, `completion_check_digest`, or `text` inside it. `promote-conduct-plan` writes an approved `conduct-plan.json` with `promotion_state: "approved"`, approval evidence, source candidate id, and evidence digest.

State-aware promotion resolves the target work-item status from `--run-dir` or the candidate file's parent run directory before writing an action. `continue_next` accepts `running`, `ready`, or `reopened`; all other decisions require `running`. A missing or incompatible target exits without creating `consistency-decision.json`, while the candidate remains diagnostic. The same run state supplies retry attempts and prior decision/state loop keys so retry caps and repeated loops escalate to `ask_user_meta` instead of looping.

## Session-Intent Bridge

The Stop adapter also accepts a host that declares `GHOST_ALICE_PLATFORM=agent-runtime`, an explicit `GHOST_ALICE_SESSION_ID`, and an absolute `GHOST_ALICE_SESSION_INTENT_ROOT`. It resolves only that exact `agent-runtime` session through the Core ledger API; it does not borrow a native platform's ledger or the shared current-session pointer. The ledger must use `session-intent-ledger.v1` and match the selected platform and session. Missing or conflicting context parks the run before pending receipts or plans are applied. Valid receipts are consumed once, and ordinary refinement within the approved objective keeps the existing approval. Unknown explicit platforms never fall back to Codex or Claude. Hosts still own their model, tool execution, and event dispatch; this adapter contract does not install those host capabilities.

Installation alone does not create `.autopilot/`. To activate an approved run from the current Ghost-ALICE session ledger, use the package bridge `skill/scripts/autopilot_session_bridge.py` or the repository wrapper `scripts/autopilot_session_bridge.py`. The bridge reads the exact current-session state through the Core SQLite ledger API. `.tmp/session-intent/ghost-state.sqlite3` is runtime authority; `current-session.json`, `intent-state.json` and `intent-events.jsonl` are compatibility/export or validated legacy-import material. Admission writes approved state to the checked selected run directory, not a guessed project-level `.autopilot/` directory.

Before admission, inspect the exact session and target without creating state:

```bash
python3 scripts/autopilot_session_bridge.py \
  --intent-root <ghost-alice>/.tmp/session-intent \
  --platform codex --session-id <current-session-id> \
  --run-dir .autopilot --check
```

The result reports the latest input event and `automatic_target`, using the same run-directory selection code as the Stop adapter. A custom `--run-dir` does not change the host process's Stop target. `matches_requested_run: false` means the inspected environment selects a different directory; preserve the strict `GHOST_ALICE_AUTOPILOT_RUN_DIR` override rather than searching or redirecting other runs. This diagnostic describes the current process environment and working directory, not a guarantee about a future host hook with different overrides. Admission requires `--input-event-id <checked-event-id>` from the receipt used to approve the work. A changed input rejects that old approval instead of rebinding it.

Admission verifies the ledger schema, platform, session identity and latest input identity before writing. It refuses to replace a run bound to another session or an unbound existing run. Admission shares the adapter's run lock and rechecks its observed input and intent state before writing; a concurrent change requires a fresh inspection. These checks validate recorded provenance and do not infer user approval from similar topics.

Session-bound completion requires a compatible core writer accepting the captured input receipt and criterion definition. The adapter records core completion before setting the task to `completed`; a rejected or unavailable core receipt leaves the task unfinished and preserves the rejected decision. Older approvals without criterion snapshots need current-input reapproval. Legacy standalone runs without a session binding retain their separate task-only contract.

Session-bound runs carry `approval_generation`, a digest of the approved input, session identity, criterion definitions and scope. Capture this value when producing evidence, then preserve it in every decision or conduct-plan candidate and action. Both `decision-candidate` and `conduct-plan-candidate` accept `--approval-generation <captured-value>`. Promotion preserves that value; it does not attach the current generation to old evidence. The adapter rejects missing or stale generations before changing tasks or core criteria. New input, changed criterion definitions or changed scope require reapproval and new evidence. The bridge preserves the superseded run and pending artifacts under `.approval-history/`; late artifacts from the old generation remain invalid. Repeating the bridge for the same input and contract leaves existing progress, pending proof, remaining budget and `OFF` unchanged. This digest is a provenance check, not proof that the model's evidence is true.

The bridge supports `--platform codex` and `--platform claude`. It refuses to write run state unless `--approval-evidence-json` contains an approval decision (`GO`, `approve`, or `approved`) and a non-empty `source`, and it preserves session event metadata in `approved-run.json` approval evidence.

The Stop adapter has a separate automatic current-session path. When the selected current-session run has no approved state and the session ledger records admitted, not-yet-met acceptance criteria, the adapter bootstraps run state with `approval_evidence.decision: "AUTO"` (`source: "admitted-unmet-criterion"`). Io-trace presence alone never bootstraps a run; io-trace is routed through the existing `autopilot-observation-signal.v1` receptor in `autopilot_governance_signal.py`, and observation candidates stay diagnostic and are not promoted into adapter-consumable action files.

For the current-session inspection and admission walkthrough, see [Try It](#try-it).

## Requirements

- Ghost-ALICE Core `0.4.0` or newer for the shared SQLite runtime, privileged adapters and schema-preserving hooks.
- Python 3.11+.
- Claude Code and/or Codex hooks installed by the Core installer.

Use the recommended Core `0.4.1` / Autopilot `0.4.1` product pair. Schema versions remain independent; the addon relies on the host's model and tool runtime.

## Compatibility Matrix

The compatibility SSOT is `compatibility-matrix.json`. It must be checked before making a full compatibility claim. The matrix records the current support posture, not a chronological test log; dated run artifacts belong in CI/test reports or release notes.

The matrix below records the established support posture. Its live Claude/Codex entries include earlier release evidence; they are not a claim that every entry was rerun for `0.4.1`. The [current release notes](./docs/release/2026-10-01-release-notes.md#verification-scope) distinguish this release's installed replay and regression checks from fresh model-inference coverage.

Current target status:

- macOS: `verified-local` with local unit tests and adapter subprocess simulation.
- Claude Code: `verified-local` with local install status, credentialed Claude live semantic E2E, and five purpose-hidden core blind-controller cases.
- Linux: `not-run`.
- Windows Command Prompt: `not-run`.
- Windows PowerShell 5: `not-run`.
- Windows PowerShell 7: `not-run`.
- Codex: `verified-local` with local install status, Codex live semantic E2E, candidate-boundary checks, and five purpose-hidden core blind-controller cases.

Any `not-run` target blocks a full compatibility claim until runner evidence is attached to the matrix. Linux and Windows runner targets still block a full compatibility claim.

## Install

Run these commands from a Ghost-ALICE core checkout. This addon repository does not provide a standalone root `install.sh`.

Default install to detected Claude Code/Codex targets:

```bash
bash install.sh --addon autopilot
```

Install only to Codex:

```bash
bash install.sh --platform codex --addon autopilot
```

Development checkout override:

```bash
bash <ghost-alice>/install.sh --addon-source /path/to/ghost-alice-autopilot
```

Check install status:

```bash
bash <ghost-alice>/install.sh --platform codex --status
```

## Try It

Use an active Ghost-ALICE session whose objective, plan, allowed surfaces and completion criteria you actually approve. Run the repository bridge from that project's directory, using its absolute script path if your shell is elsewhere. Replace every placeholder with the current hook receipt or checked result; the examples do not grant approval.

- Inspect the current input and automatic Stop target without writing run state:

```bash
python3 <autopilot-repository>/scripts/autopilot_session_bridge.py \
  --intent-root <receipt-root>/.tmp/session-intent \
  --platform codex --session-id <current-session-id> \
  --run-dir "<project>/.autopilot" --check
```

- Read `latest_input_event.event_id` and `automatic_target.run_dir` from the result. The initial `--run-dir` is a probe, not an admission target. Recheck the exact selected directory and captured input receipt:

```bash
python3 <autopilot-repository>/scripts/autopilot_session_bridge.py \
  --intent-root <receipt-root>/.tmp/session-intent \
  --platform codex --session-id <current-session-id> \
  --input-event-id "<checked-event-id>" \
  --run-dir "<automatic_target.run_dir>" --check
```

- Confirm `automatic_target.matches_requested_run: true`, matching platform/session, and the same checked input. Review the objective, plan, criteria, permitted changes and budget, then give actual approval in that session. Use the receipt bound to that approval; if approval changes the input, repeat the inspection for the new receipt. Preserve an explicit `GHOST_ALICE_AUTOPILOT_RUN_DIR` override; an ownership conflict is not permission to redirect the run.
- After actual approval, admit the approved item into that exact selected run. `--approval-evidence-json` must contain the actual approval decision (`GO`, `approve` or `approved`) and a non-empty source identifying that approval. Replace the placeholder with that real JSON record:

```bash
python3 <autopilot-repository>/scripts/autopilot_session_bridge.py \
  --intent-root <receipt-root>/.tmp/session-intent \
  --platform codex --session-id <current-session-id> \
  --input-event-id "<approved-checked-event-id>" \
  --run-dir "<automatic_target.run_dir>" \
  --current-work-item-id "<approved-work-item-id>" \
  --plan-path "<approved-plan-path>" \
  --remaining-steps <approved-step-budget> \
  --allowed-surface "<approved-surface>" \
  --approval-evidence-json '<actual-approval-evidence-json>'
```

Use `--platform claude` for a Claude session. The bridge reports the selected `run_dir` and admission result. At a Stop event, continuation depends on that exact run's current approval, pause state, budget, task state and evidence; a command example does not promise a particular run ID or work item.

Use the selected `run_dir` from the checked result or completion receipt for status locators and action inboxes. Promoted `consistency-decision.json` and `conduct-plan.json` belong in that directory; candidate files remain diagnostic only. Completion requires the promoted schema, current `approval_generation`, criterion-bound passing evidence and the Core completion receipt. SQLite is authoritative after migration: editing or removing stale `approved-run.json` or `tasks.jsonl` exports does not update the stored run or stop it. The complete promotion schema and decision commands are in the [consistency decision contract](./addons/autopilot-mode/skill/SKILL.md#consistency-decisions).

## Pause, Resume, Stop

- To pause every default session run in the project, create `<project>/.autopilot/OFF`.
- To pause only the selected run, create `<automatic_target.run_dir>/OFF`.
- To resume, remove the specific `OFF` marker you created. A project-level marker continues to pause default runs even when a selected-run marker is absent.

From the project directory, the project-wide pause and resume commands are:

```bash
touch .autopilot/OFF
```

Resume:

```bash
rm .autopilot/OFF
```

Run only the command for the action you intend. For an individual run, use the checked selected directory instead of `.autopilot/`.

To stop work, tell the agent explicitly to stop and retain the appropriate `OFF` marker to prevent further adapter continuation. Inspect the exact selected run before any later reapproval. Do not try to stop a migrated run by changing or deleting its JSON export.

## Remove

Remove only this addon:

```bash
bash <ghost-alice>/install.sh \
  --platform codex \
  --uninstall --addon autopilot-mode
```

Use `--platform claude` for Claude Code. Uninstall is driven by the installed addon id and sidecar, not by `--addon-source`.

Full Ghost-ALICE uninstall still uses the core full-uninstall path:

```bash
bash <ghost-alice>/install.sh --uninstall
```

## Limits And Trust Notes

- Installing the addon is not runtime activation.
- The adapter accepts no arguments.
- The adapter updates the selected project-local run state and publishes criterion-bound completion to the Core SQLite ledger; it emits a continuation payload.
- The continuation payload contains a `before-stop` contract so an executing agent leaves a promoted `<selected-run-dir>/consistency-decision.json` before it stops.
- Candidate files such as `consistency-decision.candidate.json` and `conduct-plan.candidate.json` are not adapter-consumable.
- `conduct-plan.json` uses `schema_version: "autopilot-conduct-plan.v2"` and must carry `promotion_state: "approved"`, `approval_evidence`, source candidate id, and evidence digest.
- Conduct plan proposals must keep `proposal_status: "proposed"`, `approval_required: true`, and an approval transition that copies `task_template` as `ready`.
- Imported proposals preserve `observer_agent_required` and `observer_contract`, and the continuation message surfaces the read-only observer requirement.
- Existing task ids are skipped so conduct plan import is idempotent.
- Tool denial, installer policy, privileged adapter allowlists, hook markers, runner namespaces, and hook install/remove behavior are owned by Ghost-ALICE core.
- This addon package owns the skill content and adapter implementation.

## Repository Layout

```text
addons-manifest.json
compatibility-matrix.json
addons/autopilot-mode/
  addon.json
  skill/SKILL.md
  skill/adapters/autopilot_lineage.py
  skill/adapters/autopilot_messages.py
  skill/adapters/autopilot_mode.py
  skill/adapters/autopilot_state.py
  skill/adapters/autopilot_work_items.py
  skill/scripts/autopilot_governance_signal.py
  skill/scripts/autopilot_session_bridge.py
  skill/scripts/autopilot_session_material.py
tests/
scripts/autopilot_session_bridge.py
```

## License

Apache-2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).

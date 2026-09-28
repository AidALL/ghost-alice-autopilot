---
name: autopilot-mode
description: "Use when a Ghost-ALICE autonomous run has approved run state or current-session runtime material and verified work items must continue through the privileged adapter."
compatibility:
  - "Python 3.11+ standard library"
  - "Ghost-ALICE core installer 0.2.2+ and runtime with shared SQLite transaction APIs"
  - "Claude Code or Codex hooks installed by the Ghost-ALICE core installer"
---

# autopilot-mode

autopilot-mode is a Ghost-ALICE addon for approved autonomous continuation. The addon does not invent work outside the current session. Session intent analysis, task routing, explicit GO evidence, and current-session runtime material can create the approved run state; this addon advances that state after the agent stop event.

## Critical Rules

- Installation is not runtime activation. Installing this addon only registers a privileged adapter hook. The adapter is a no-op until an approved run state exists.
- Ghost-ALICE core must be 0.2.2 or newer. Older core installers may copy this skill without wiring the privileged adapter, runtime-core audit, ledger met-flip path, or schema-preserving hook renderer required by the current addon contract; that install is inert or incomplete and should be removed before upgrading.
- The adapter accepts no arguments. Any argv value is rejected with exit code 64.
- The adapter reads project-local state from `<project>/.autopilot/` by default. `GHOST_ALICE_AUTOPILOT_RUN_DIR` is a strict run-directory override and `GHOST_ALICE_AUTOPILOT_CWD` is the next project-root override. The project-root override must be absolute; a relative value surfaces as a blocking adapter reason instead of being resolved against the process directory. Without either override, Claude hooks select the first non-empty absolute path from `CLAUDE_PROJECT_DIR` and hook-time `cwd`; Codex ignores an inherited `CLAUDE_PROJECT_DIR` and selects hook-time `cwd`. Both platforms then fall back to the absolute process working directory. Relative derived candidates are skipped, while an access failure after selection does not retry a lower-priority source. Only `PermissionError` during derived-directory creation or lock acquisition becomes the empty no-op payload; later state-processing exceptions and explicit run-directory errors still surface.
- Runtime activation requires validated run state (legacy `approved-run.json` on first import, then SQLite authority) with `approved: true`, `status: "running"`, a positive `budget.remaining_steps`, non-empty `scope`, non-empty `allowed_surfaces`, non-empty `stop_conditions`, and non-empty `approval_evidence`.
- `skill/scripts/autopilot_session_bridge.py` can bootstrap approved run state through the Core authoritative state/event APIs for Codex or Claude only when explicit approval evidence is supplied. In an installed skill, run it as `scripts/autopilot_session_bridge.py` from the skill root; in a source checkout, the repository wrapper at `scripts/autopilot_session_bridge.py` delegates to the skill-local bridge.
- The Stop adapter can also materialize current-session `.autopilot/` state when authoritative Core state records admitted, not-yet-met acceptance criteria. That path records `approval_evidence.decision: "AUTO"` (`source: "admitted-unmet-criterion"`); io-trace material alone never bootstraps a run and is fed through the existing `autopilot-observation-signal.v1` receptor.
- SQLite is the durable source of truth after import; `tasks.jsonl` is a legacy import or export shape. The ready queue is derived from task status and dependencies; `ready` and `reopened` items can be selected when dependencies are satisfied, and work items are never popped.
- A pause file at `.autopilot/OFF` disables continuation without deleting state.
- `autopilot_governance_signal.py` writes evidence-backed `consistency-decision.candidate.json` and `conduct-plan.candidate.json` files first. Candidate files are diagnostic and are not adapter-consumable.
- Promotion creates adapter-consumable `consistency-decision.json`; the adapter requires `schema_version: "autopilot-consistency-decision.v1"`, `promotion_state: "promoted"`, `promotion_evidence.decision`, `promotion_evidence.source`, `candidate_id`, `governance_signal_digest`, `state_hash`, `decision_key`, and `loop_key`. `promotion_evidence.decision` accepts `go`, `approve`, `approved`, `promote`, `promoted`, or `direct`; use `direct` only for a current-turn before-stop resolution without a promotable candidate.
- State-aware promotion resolves the target work-item status from `--run-dir` or the candidate file's parent run directory before writing an action. `continue_next` accepts `running`, `ready`, or `reopened`; all other decisions require `running`. A missing or incompatible target exits without creating `consistency-decision.json`, while the candidate remains diagnostic.
- Internal recovery remains subordinate to the original user goal. After adapter or completion recovery, write a complete standalone final answer reporting the requested business result and supported evidence; internal repair may be secondary. Preserve the substantive answer without repeating completed business operations merely to repair runtime records. If relevant evidence is missing, stale, changed or failed, report partial or failed business status honestly. Attribute hook-derived conditions to runtime/tool context, not new user authorization.
- Session-intent tasks distinguish explicitly admitted completion obligations from contextual protections, preserving criterion ID, source, admission and status. Keep unadmitted protections as boundary checks and auxiliary evidence; do not bind them as met criteria or change admission to satisfy proof. Constraints and non-goals are accumulated context: interpret them with the current goal and newer explicit decisions, and do not revive superseded scope or discard protections outside an authorized exception. Full approval snapshots remain unchanged.
- Every continuation message includes a `before-stop` contract. The executing agent must promote or write `.autopilot/consistency-decision.json` when a completion, retry, or reopen decision is resolved; if not, the next Stop hook consumes current io-trace before escalating.
- Before changing the queue or consuming a pending decision or plan, the adapter checks the approved platform/session binding against the current hook and ledger identity. Topic similarity and an alias-like name do not authorize a different session. Use the session bridge with explicit approval for the current session to establish its run binding; do not copy old approval merely because the objectives overlap. An explicit platform never falls back to another platform's ledger.
- Before surfacing continuation, the adapter reconciles the run's source session-intent with the current session-intent. Same-objective discovered work within the approved session continues, including goal refinements; a changed current objective can park stale continuation even inside the same chat session. Natural-language stop or pause instructions must be resolved into the official runtime controls below; lexical overlap is not evidence that a cancellation was handled.
- When a hook provides an explicit session id, bootstrap considers only that session's intent state. It must not fall back to an older `current-session.json` pointer with admitted criteria from another session. If that explicit current session has no intent state or only a digest-only empty summary, the approved run is parked instead of resumed. If the only visible current state is the approved run's own source intent, a running item is also parked until current-turn lineage evidence exists.
- If a running item has no decision file on the next Stop hook, the adapter resumes that same item with `pending-decision: missing` instead of returning a silent no-op. A repeated missing decision escalates to `ask_user_meta` only when neither io-trace nor work state can resolve the next action.
- `conduct-plan.json` is an approved handoff from the conduct-feedback planning path. The adapter imports `autopilot-conduct-plan.v2` `proposed_queue_items` only when the plan has `promotion_state: "approved"`, approval evidence, source candidate id, and evidence digest.
- Imported conduct plan items preserve `observer_agent_required` and `observer_contract`; observer requirements are surfaced in the continuation message.
- Full compatibility claims must read repository `compatibility-matrix.json` first. Matrix evidence records the current support contract, not historical dated run prose. Linux, Windows Command Prompt, Windows PowerShell 5, and Windows PowerShell 7 targets marked `not-run` block a full compatibility claim until runner evidence is attached.
- The adapter never denies tools and never widens Ghost-ALICE core policy. Its Stop hook output is either a no-op payload or a continuation message for the next ready/reopened item or the current running item that still has unresolved runtime material.

## Run Directory

Run-directory inputs and locators (state JSON/JSONL files become legacy inputs after migration):

```text
<project>/.autopilot/
  authority.json
  ghost-state.sqlite3  # standalone runs; bound runs share the Core database
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

`approved-run.json` records the user-approved run boundary:

```json
{
  "schema_version": "autopilot-run.v1",
  "run_id": "run-1",
  "approved": true,
  "status": "running",
  "scope": {"summary": "Implement the approved work plan"},
  "budget": {"remaining_steps": 3},
  "allowed_surfaces": ["src/...", "tests/..."],
  "stop_conditions": ["budget_exhausted", "user_stop"],
  "approval_evidence": {"decision": "GO", "source": "user-confirmation"}
}
```

Each line in `tasks.jsonl` is one work item:

```json
{"id":"unit-1","status":"ready","focus_layer":"micro","depends_on":[],"prompt":"Implement unit 1","acceptance_criteria":["tests pass"],"allowed_surface":["src/..."],"completion":{"state":"not_started","verdict":null,"evidence":[],"completion_check_digest":null,"reopen_target":null},"attempt":0}
```

Allowed statuses are `ready`, `running`, `completed`, `reopened`, `blocked`, `stopped`, and `not_applicable`.

## Session-Intent Bridge

Use `skill/scripts/autopilot_session_bridge.py` when the current session-intent ledger is the source of the approved work. In an installed skill, run it as `scripts/autopilot_session_bridge.py` from the skill root; in a source checkout, the repository wrapper at `scripts/autopilot_session_bridge.py` delegates to the skill-local bridge. The bridge reads exact Core session coordinates through the shared storage API and commits the approved run and ready work on the same database connection. `authority.json` is a validated locator, not permission. Legacy JSON input files remain unchanged after migration; reading or editing them cannot override migrated state. It supports `--platform codex` and `--platform claude` and refuses to write adapter-consumable state unless approval evidence contains `decision: "GO"` or another explicit approval decision plus a non-empty `source`.

First inspect with `--intent-root`, `--platform`, `--session-id`, `--run-dir`, and `--check`. This writes no run state and reports the current input receipt and automatic Stop target for the command's environment. A custom run directory does not redirect the host Stop hook. Admission requires the `--input-event-id` used when approving the work; if it changed, inspect and re-evaluate the existing authorization against the new input. Do not replace a stale token with a freshly read token without that decision. The bridge preserves the exact criterion definitions and input anchor; completion can mark only those unchanged, still-admitted criteria through the core writer. A session-bound task and all matching Core criterion proofs commit together in one transaction. A failure rolls back both; a consumed inbox receipt prevents replay even if its post-commit archive move fails. Missing snapshots, stale proof, or an unavailable compatible core writer preserve the unfinished task and quarantine the rejected decision for correction.

Session-bound runs carry `approval_generation`, a digest of the approved input, session identity, criterion definitions and scope. Capture this value when producing evidence, then preserve it in every decision or conduct-plan candidate and action. Both `decision-candidate` and `conduct-plan-candidate` accept `--approval-generation <captured-value>`. Promotion preserves that value; it does not attach the current generation to old evidence. The adapter rejects missing or stale generations before changing tasks or core criteria. New input, changed criterion definitions or changed scope require reapproval and new evidence. The bridge preserves superseded generations in database history and archives pending inbox artifacts; late artifacts from the old generation remain invalid. Repeating the bridge for the same input and contract leaves existing progress, pending proof, remaining budget and `OFF` unchanged. This digest is a provenance check, not proof that the model's evidence is true.

```bash
python3 scripts/autopilot_session_bridge.py \
  --intent-root <ghost-alice>/.tmp/session-intent \
  --platform codex \
  --session-id <current-session-id> \
  --input-event-id <checked-event-id> \
  --run-dir .autopilot \
  --current-work-item-id current \
  --plan-path .tmp/implementation-plans/current.md \
  --approval-evidence-json '{"decision":"GO","source":"user-confirmation"}'
```

`conduct-plan.candidate.json` uses `schema_version: "autopilot-conduct-plan-candidate.v1"`, `promotion_state: "candidate"`, and `action_file_allowed: false`. `conduct-plan.json` accepts the `autopilot-conduct-plan.v2` shape only after promotion. The adapter requires `promotion_state: "approved"`, non-empty `approval_evidence`, `source_candidate_id`, and `evidence_digest`. Each proposal must keep `proposal_status: "proposed"`, `approval_required: true`, `approval_transition.status_on_approval: "ready"`, and `approval_transition.copy_task_template: true`. On import, the adapter copies each new `task_template` into authoritative task state, sets `status` to `ready`, skips task ids already present in the queue, writes the consumed plan snapshot to `conduct-plan.applied.json` after commit, and records a `conduct_plan_imported` event.

## Consistency Decisions

`consistency-decision.candidate.json` uses `schema_version: "autopilot-consistency-decision-candidate.v1"`, `promotion_state: "candidate"`, and `action_file_allowed: false`. `consistency-decision.json` is adapter-consumable only after promotion. Prefer `scripts/autopilot_governance_signal.py promote-decision` for an eligible evidence-backed candidate; observation_signal candidates remain diagnostic and must not be promoted or relabeled. If only diagnostic material exists, continue from observed work and create a fresh resolved decision after verification; if a completion decision must be written directly, it still needs the full promoted action schema: `schema_version`, `decision_id`, `work_item_id`, `decision`, `promotion_state: "promoted"`, `promotion_evidence.decision`, `promotion_evidence.source`, `candidate_id`, `governance_signal_digest`, `decision_key`, `state_hash`, `loop_key`, and `evidence`. `promotion_evidence.decision` must be one of `go`, `approve`, `approved`, `promote`, `promoted`, or `direct`; use `direct` only for a current-turn before-stop resolution without a promotable candidate. The `evidence` field must be a JSON array of strings. Do not nest `verdict`, `completion_check_digest`, or `text` inside `evidence`. The adapter accepts these promoted decisions:

- `continue_next`
- `retry_same_unit`
- `reopen_micro`
- `reopen_meso`
- `reopen_macro`
- `ask_user_meta`
- `stop`

`continue_next` requires passing completion evidence. Inline `unverified: none` and nested `- none` are equivalent; explicit claim fields may be on separate lines or separated by unquoted semicolons. Every claim begins with `claim:` and includes evidence, known criterion IDs and `verdict: pass`. Duplicate sections/fields, missing evidence and unresolved unverified values are rejected. Multiple criterion IDs may be separated by commas, semicolons or whitespace. The action includes: `verdict: "pass"`, `completion_check_digest` in `sha256:<64-hex>` form, and evidence text containing `[completion-check]`, `acceptance-criteria`, and `claim-evidence-map` entries that reference known acceptance-criteria criterion ids.

`retry_same_unit` returns the running item to the ready queue only with concrete evidence. `reopen_micro`, `reopen_meso`, and `reopen_macro` keep the same item open as `reopened`; the next continuation can select it again and includes `reopen-target: <micro|meso|macro>`.

## Continuation Message

When a next item is ready, the adapter emits this message shape:

```text
[autopilot]
run: <run_id>
work-item: <item_id>
focus-layer: <micro|meso|macro|meta>
pending-decision: missing
io-trace:
- <recent tool/path summary>
governance-signal:
- candidate: <candidate-id>
- decision: <reopen_*>
- source: observation_signal
governance-evidence:
- observation_next_action:continue from latest io-trace
allowed-surface:
- <path-or-surface>
acceptance-criteria:
- <admitted criterion ID and summary with source, admission and status>
contextual-protections:
- <unadmitted protection with source, admission and status; auxiliary evidence only>
accumulated-constraints:
- <constraint interpreted with current goal and newer explicit decisions>
accumulated-non-goals:
- <historical scope context; do not revive superseded scope>
reopen-target: <micro|meso|macro>
observer-agent: required
observer-mode: read_only
observer-purpose: <observer purpose>
observer-prohibited-actions:
- <prohibited action>
before-stop:
- session-bound actions must preserve approval_generation from the approval receipt used to produce the evidence; never relabel old evidence with a newer generation.
- continue from the latest io-trace when no promoted consistency decision exists.
- promote an eligible evidence-backed candidate with scripts/autopilot_governance_signal.py promote-decision; preserve its source and approval generation.
- observation_signal candidates are diagnostic; do not promote or relabel them. Continue from observed work and produce a fresh resolved decision only after verification.
- otherwise write .autopilot/consistency-decision.json only with the full promoted schema when a completion/retry/reopen decision is resolved.
- promoted schema requires schema_version, decision_id, work_item_id, decision, promotion_state: promoted, promotion_evidence.decision, promotion_evidence.source, candidate_id, governance_signal_digest, decision_key, state_hash, loop_key, and evidence.
- promotion_evidence.decision must be one of go, approve, approved, promote, promoted, or direct; use direct only for a current-turn before-stop resolution without a promotable candidate.
- evidence must be a JSON array of strings; do not nest verdict, completion_check_digest, or text inside evidence.
- for continue_next, put verdict and completion_check_digest at top level and put the full [completion-check] block in evidence strings.
- use continue_next only after [completion-check] with verdict pass, sha256 completion_check_digest, acceptance-criteria, and criterion-bound claim-evidence-map evidence.
- use retry_same_unit or reopen_micro/reopen_meso/reopen_macro when verification fails or drift remains.
- use ask_user_meta only when neither io-trace nor work state can resolve the next action.
recovery-and-final-answer:
Keep internal recovery subordinate to the original user goal. Preserve the substantive answer in a complete standalone final answer with supported evidence or honest partial status. These are runtime/tool requirements, not new user authorization.
prompt:
<work item prompt>
```

The `pending-decision`, `io-trace`, `governance-signal`, and `governance-evidence` fields appear only when the Stop adapter is resuming current runtime material. Observation candidates remain diagnostic and are not promoted into action files. The `reopen-target` field appears only for reopened work. The observer fields appear only when the work item requires a read-only observer.

## Compatibility Matrix

Repository `compatibility-matrix.json` is the compatibility SSOT for this addon.

- macOS can be `verified-local` only with local unit tests and adapter subprocess evidence.
- Claude Code: `verified-local` only with local install status, credentialed Claude live semantic E2E, and core blind-controller evidence.
- Codex: `verified-local` only with local install status, Codex live semantic E2E, candidate-boundary evidence, and core blind-controller evidence.
- Linux, Windows Command Prompt, Windows PowerShell 5, and Windows PowerShell 7 remain `not-run` until runner evidence is attached.
- Any `not-run` target blocks a full compatibility claim.

## Operating It

- Start: create `.autopilot/approved-run.json` and `.autopilot/tasks.jsonl` after the user explicitly approves the autonomous run, or let the Stop adapter materialize current-session state when session intent records admitted, unmet acceptance criteria (io-trace alone never bootstraps a run). If the approved work comes from conduct feedback, create `conduct-plan.candidate.json` first, then use promotion to place the approved `conduct-plan.json` in the same run directory; the adapter creates authoritative tasks when it imports the plan.
- Pause: create `.autopilot/OFF`.
- Resume: remove `.autopilot/OFF`.
- Stop: create `.autopilot/OFF` for immediate stop, then set the authoritative run status to `stopped` through the storage API inside its transaction. Deleting or editing a legacy `approved-run.json` does not stop a migrated run.
- Replan: preserve terminal work items and update open work through the validated bridge or adapter work-item APIs inside the authority transaction. Do not overwrite legacy `tasks.jsonl` or write SQL directly.

For inspection, import `autopilot_storage` from the installed `adapters` directory and call `read(run_dir / "approved-run.json")`, `read(run_dir / "tasks.jsonl")` or `read(run_dir / "events.jsonl")`. These are API locators, not instructions to open the files. Check `storage.pending(path)` for action inboxes: a physically retained file may already be consumed. After-commit archiving copies the consumed snapshot and never removes a producer-owned inbox, so a concurrently published newer action remains available. To stop after an explicit user instruction, run the following from that adapters directory:

```python
from pathlib import Path
import autopilot_storage as storage

run_dir = Path("/absolute/path/to/.autopilot")
(run_dir / "OFF").touch()
with storage.transaction(run_dir) as store:
    run = store.run()
    run["status"] = "stopped"
    store.write("approved-run.json", run)
```

Bound runs require a Core runtime exposing `storage_transaction` and transaction-aware state/event/proof APIs. Package version alone does not establish this capability. Named valid legacy standalone runs import into their own explicitly identified authority; they never infer a Core platform or session. Preserve the authority locator and database together. A missing or corrupt initialized database is a recovery error, not permission to resurrect stale JSON. Stop legacy writers before migration.

Core `intent-state.json` and `intent-events.jsonl` names remain compatibility locators for legacy imports and explicit exports. Runtime inspection uses `session_intent_ledger.py --read-state` and `--read-events` with the exact root, platform and session ID; do not open those files as current authority after migration.

## Package Surface

```text
skill/adapters/autopilot_storage.py
skill/adapters/autopilot_messages.py
skill/adapters/autopilot_lineage.py
skill/adapters/autopilot_mode.py
skill/adapters/autopilot_runtime_context.py
skill/adapters/autopilot_state.py
skill/adapters/autopilot_work_items.py
skill/scripts/autopilot_governance_signal.py
skill/scripts/autopilot_session_bridge.py
skill/scripts/autopilot_session_material.py
```

## Install and Remove

Use the Ghost-ALICE core installer from a Ghost-ALICE core checkout. This addon does not install hooks directly and does not provide a standalone root `install.sh`.

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
bash <ghost-alice>/install.sh --addon-source <this-repo>
```

Remove only this addon:

```bash
bash <ghost-alice>/install.sh --platform codex --uninstall --addon autopilot-mode
```

Use `--platform claude` for Claude Code. Uninstall is driven by the installed addon id and sidecar, not by `--addon-source`.

The addon manifest requests `privileged_adapters: ["autopilot-mode"]`. The core-owned privileged adapter allowlist chooses the event, marker, runner namespace, and adapter script path.

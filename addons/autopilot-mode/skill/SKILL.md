---
name: autopilot-mode
description: "Use when a Ghost-ALICE autonomous run has approved run state or current-session runtime material and verified work items must continue through the privileged adapter."
compatibility:
  - "Python 3.11+ standard library"
  - "Ghost-ALICE core 0.2.0+ with privileged adapter support"
  - "Claude Code or Codex hooks installed by the Ghost-ALICE core installer"
---

# autopilot-mode

autopilot-mode is a Ghost-ALICE addon for approved autonomous continuation. The addon does not invent work outside the current session. Session intent analysis, task routing, explicit GO evidence, and current-session runtime material can create the approved run state; this addon advances that state after the agent stop event.

## Critical Rules

- Installation is not runtime activation. Installing this addon only registers a privileged adapter hook. The adapter is a no-op until an approved run state exists.
- Ghost-ALICE core must be 0.2.0 or newer. Older core installers may copy this skill without wiring the privileged adapter; that install is inert and should be removed before upgrading.
- The adapter accepts no arguments. Any argv value is rejected with exit code 64.
- The adapter reads project-local state from `<cwd>/.autopilot/` by default. `GHOST_ALICE_AUTOPILOT_RUN_DIR` can point at a different run directory.
- Runtime activation requires `approved-run.json` with `approved: true`, `status: "running"`, a positive `budget.remaining_steps`, non-empty `scope`, non-empty `allowed_surfaces`, non-empty `stop_conditions`, and non-empty `approval_evidence`.
- `skill/scripts/autopilot_session_bridge.py` can bootstrap approved run state from `current-session.json`, `intent-state.json`, and `intent-events.jsonl` for Codex or Claude only when explicit approval evidence is supplied. In an installed skill, run it as `scripts/autopilot_session_bridge.py` from the skill root; in a source checkout, the repository wrapper at `scripts/autopilot_session_bridge.py` delegates to the skill-local bridge.
- The Stop adapter can also materialize current-session `.autopilot/` state when `intent-state.json` plus io-trace or open conduct feedback provide current runtime material. That path records `approval_evidence.decision: "AUTO"` and feeds io-trace through the existing `autopilot-observation-signal.v1` receptor.
- `tasks.jsonl` is the durable source of truth. The ready queue is derived from task status and dependencies; `ready` and `reopened` items can be selected when dependencies are satisfied, and work items are never popped.
- A pause file at `.autopilot/OFF` disables continuation without deleting state.
- `autopilot_governance_signal.py` writes evidence-backed `consistency-decision.candidate.json` and `conduct-plan.candidate.json` files first. Candidate files are diagnostic and are not adapter-consumable.
- Promotion creates adapter-consumable `consistency-decision.json`; the adapter requires `schema_version: "autopilot-consistency-decision.v1"`, `promotion_state: "promoted"`, promotion evidence, candidate id, evidence digest, state hash, decision key, and loop key.
- Every continuation message includes a `before-stop` contract. The executing agent must promote or write `.autopilot/consistency-decision.json` when a completion, retry, or reopen decision is resolved; if not, the next Stop hook consumes current io-trace before escalating.
- If a running item has no decision file on the next Stop hook, the adapter resumes that same item with `pending-decision: missing` instead of returning a silent no-op. A repeated missing decision escalates to `ask_user_meta` only when neither io-trace nor work state can resolve the next action.
- `conduct-plan.json` is an approved handoff from the conduct-feedback planning path. The adapter imports `autopilot-conduct-plan.v2` `proposed_queue_items` only when the plan has `promotion_state: "approved"`, approval evidence, source candidate id, and evidence digest.
- Imported conduct plan items preserve `observer_agent_required` and `observer_contract`; observer requirements are surfaced in the continuation message.
- Full compatibility claims must read repository `compatibility-matrix.json` first. Matrix evidence records the current support contract, not historical dated run prose. Linux, Windows Command Prompt, Windows PowerShell 5, and Windows PowerShell 7 targets marked `not-run` block a full compatibility claim until runner evidence is attached.
- The adapter never denies tools and never widens Ghost-ALICE core policy. Its Stop hook output is either a no-op payload or a continuation message for the next ready/reopened item or the current running item that still has unresolved runtime material.

## Run Directory

Default layout:

```text
<project>/.autopilot/
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

Use `skill/scripts/autopilot_session_bridge.py` when the current session-intent
ledger is the source of the approved work. In an installed skill, run it as
`scripts/autopilot_session_bridge.py` from the skill root; in a source checkout,
the repository wrapper at `scripts/autopilot_session_bridge.py` delegates to the
skill-local bridge. The bridge reads
`.tmp/session-intent/<platform>/current-session.json`, the pointed
`intent-state.json`, and sibling `intent-events.jsonl`; it then writes
`.autopilot/approved-run.json` and either an approved `conduct-plan.json` or a
ready `tasks.jsonl` item. It supports `--platform codex` and `--platform claude`
and refuses to write adapter-consumable state unless approval evidence contains
`decision: "GO"` or another explicit approval decision plus a non-empty
`source`.

```bash
python3 scripts/autopilot_session_bridge.py \
  --intent-root <ghost-alice>/.tmp/session-intent \
  --platform codex \
  --run-dir .autopilot \
  --current-work-item-id current \
  --plan-path .tmp/implementation-plans/current.md \
  --approval-evidence-json '{"decision":"GO","source":"user-confirmation"}'
```

`conduct-plan.candidate.json` uses `schema_version: "autopilot-conduct-plan-candidate.v1"`, `promotion_state: "candidate"`, and `action_file_allowed: false`. `conduct-plan.json` accepts the `autopilot-conduct-plan.v2` shape only after promotion. The adapter requires `promotion_state: "approved"`, non-empty `approval_evidence`, `source_candidate_id`, and `evidence_digest`. Each proposal must keep `proposal_status: "proposed"`, `approval_required: true`, `approval_transition.status_on_approval: "ready"`, and `approval_transition.copy_task_template: true`. On import, the adapter copies each new `task_template` into `tasks.jsonl`, sets `status` to `ready`, skips task ids already present in the queue, moves the plan to `conduct-plan.applied.json`, and records a `conduct_plan_imported` event.

## Consistency Decisions

`consistency-decision.candidate.json` uses `schema_version: "autopilot-consistency-decision-candidate.v1"`, `promotion_state: "candidate"`, and `action_file_allowed: false`. `consistency-decision.json` is adapter-consumable only after promotion. The adapter accepts these promoted decisions:

- `continue_next`
- `retry_same_unit`
- `reopen_micro`
- `reopen_meso`
- `reopen_macro`
- `ask_user_meta`
- `stop`

`continue_next` requires passing completion evidence: `verdict: "pass"`, `completion_check_digest` in `sha256:<64-hex>` form, and evidence text containing `[completion-check]`, `acceptance-criteria`, and `claim-evidence-map` entries that reference known acceptance-criteria criterion ids.

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
- <criterion>
reopen-target: <micro|meso|macro>
observer-agent: required
observer-mode: read_only
observer-purpose: <observer purpose>
observer-prohibited-actions:
- <prohibited action>
before-stop:
- continue from the latest io-trace when no promoted consistency decision exists.
- write .autopilot/consistency-decision.json when a completion/retry/reopen decision is resolved.
- use continue_next only after [completion-check] with verdict pass, sha256 completion_check_digest, acceptance-criteria, and criterion-bound claim-evidence-map evidence.
- use retry_same_unit or reopen_micro/reopen_meso/reopen_macro when verification fails or drift remains.
- use ask_user_meta only when neither io-trace nor work state can resolve the next action.
prompt:
<work item prompt>
```

The `pending-decision`, `io-trace`, `governance-signal`, and `governance-evidence` fields appear only when the Stop adapter is resuming current runtime material. Observation candidates remain diagnostic and are not promoted into action files. The `reopen-target` field appears only for reopened work. The observer fields appear only when the work item requires a read-only observer.

## Compatibility Matrix

Repository `compatibility-matrix.json` is the compatibility SSOT for this addon.

- macOS can be `verified-local` only with local unit tests and adapter subprocess evidence.
- Claude Code can be `simulated-local` with temporary hook install and removal evidence.
- Codex can be `verified-local` only with local install status, Codex live semantic E2E, and candidate-boundary evidence.
- Linux, Windows Command Prompt, Windows PowerShell 5, and Windows PowerShell 7 remain `not-run` until runner evidence is attached.
- Any `not-run` target blocks a full compatibility claim.

## Operating It

- Start: create `.autopilot/approved-run.json` and `.autopilot/tasks.jsonl` after the user explicitly approves the autonomous run, or let the Stop adapter materialize current-session state from session-intent plus io-trace/open conduct feedback. If the approved work comes from conduct feedback, create `conduct-plan.candidate.json` first, then use promotion to place the approved `conduct-plan.json` in the same run directory; the adapter creates `tasks.jsonl` when it imports the plan.
- Pause: create `.autopilot/OFF`.
- Resume: remove `.autopilot/OFF`.
- Stop: set `approved-run.json` `status` to `stopped`, set `approved` to false, set `budget.remaining_steps` to 0, or remove the approved run file.
- Replan: preserve terminal work items, then rewrite open work items in `tasks.jsonl`.

## Package Surface

```text
skill/adapters/autopilot_messages.py
skill/adapters/autopilot_mode.py
skill/adapters/autopilot_state.py
skill/adapters/autopilot_work_items.py
skill/scripts/autopilot_governance_signal.py
skill/scripts/autopilot_session_bridge.py
skill/scripts/autopilot_session_material.py
```

## Install and Remove

Use the Ghost-ALICE core installer from a Ghost-ALICE core checkout. This addon
does not install hooks directly and does not provide a standalone root
`install.sh`.

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

---
name: autopilot-mode
description: "Use after the user explicitly approves a Ghost-ALICE autonomous run and wants verified work items continued by the privileged adapter."
compatibility:
  - "Python 3.11+ standard library"
  - "Ghost-ALICE core 0.1.3+ with privileged adapter support"
  - "Claude Code or Codex hooks installed by the Ghost-ALICE core installer"
---

# autopilot-mode

autopilot-mode is a Ghost-ALICE addon for approved autonomous continuation. The addon does not decide that a run should start. Session intent analysis, task routing, and an explicit user GO decision create the approved run state; this addon only advances that state after the agent stop event.

## Critical Rules

- Installation is not runtime activation. Installing this addon only registers a privileged adapter hook. The adapter is a no-op until an approved run state exists.
- Ghost-ALICE core must be 0.1.3 or newer. Older core installers may copy this skill without wiring the privileged adapter; that install is inert and should be removed before upgrading.
- After session intent and task routing identify an explicit user GO decision, use `scripts/autopilot_start_run.py` to create the approved run state before expecting Stop continuation.
- The adapter accepts no arguments. Any argv value is rejected with exit code 64.
- The adapter reads project-local state from `<cwd>/.autopilot/` by default. `GHOST_ALICE_AUTOPILOT_RUN_DIR` can point at a different run directory.
- Runtime activation requires `approved-run.json` with `approved: true`, `status: "running"`, a positive `budget.remaining_steps`, non-empty `scope`, non-empty `allowed_surfaces`, non-empty `stop_conditions`, and non-empty `approval_evidence`.
- `tasks.jsonl` is the durable source of truth. The ready queue is derived from task status and dependencies; work items are never popped.
- A pause file at `.autopilot/OFF` disables continuation without deleting state.
- `consistency-decision.json` is produced by the core-owned consistency checker. The adapter only consumes that decision, applies allowed transitions, records events, and then emits the next continuation payload.
- The adapter never denies tools and never widens Ghost-ALICE core policy. Its Stop hook output is either a no-op payload or a continuation message for the next ready work item.

## Run Directory

Default layout:

```text
<project>/.autopilot/
  approved-run.json
  tasks.jsonl
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

## Consistency Decisions

The adapter accepts these checker decisions:

- `continue_next`
- `retry_same_unit`
- `reopen_micro`
- `reopen_meso`
- `reopen_macro`
- `ask_user_meta`
- `stop`

`continue_next` requires passing completion evidence: `verdict: "pass"`, a non-empty `completion_check_digest`, and non-empty `evidence`.

## Start Script

Use `scripts/autopilot_start_run.py` after explicit GO approval. It reads a JSON
spec from stdin by default, writes `.autopilot/approved-run.json` and
`.autopilot/tasks.jsonl`, refuses `approval_evidence.decision` values other
than `GO`, and refuses to overwrite an active run unless `--replace-active` is
passed.

```bash
python3 scripts/autopilot_start_run.py --project-dir /path/to/project <<'JSON'
{
  "run_id": "approved-run",
  "scope": {"summary": "Approved autonomous work"},
  "budget": {"remaining_steps": 3},
  "allowed_surfaces": ["src/...", "tests/..."],
  "stop_conditions": ["budget_exhausted", "user_stop"],
  "approval_evidence": {"decision": "GO", "source": "user-confirmation"},
  "tasks": [
    {
      "id": "unit-1",
      "focus_layer": "meso",
      "depends_on": [],
      "prompt": "Implement the first approved work unit.",
      "acceptance_criteria": ["tests pass"],
      "allowed_surface": ["src/...", "tests/..."]
    }
  ]
}
JSON
```

## Consistency Decision Script

Use `scripts/autopilot_consistency_decision.py` after a work item produces a
completion-check result. The script writes `.autopilot/consistency-decision.json`
for the adapter to consume on the next Stop event. Passing completion checks
become `continue_next`; failing or invalid completion checks become
`retry_same_unit`. Operator-controlled outcomes such as `stop`,
`ask_user_meta`, or `reopen_macro` are accepted through `--decision` with
explicit `--evidence`.

```bash
python3 scripts/autopilot_consistency_decision.py \
  --run-dir /path/to/project/.autopilot \
  --work-item-id unit-1 < final-response.txt
```

## Continuation Message

When a next item is ready, the adapter emits this message shape:

```text
[autopilot]
run: <run_id>
work-item: <item_id>
focus-layer: <micro|meso|macro|meta>
allowed-surface:
- <path-or-surface>
acceptance-criteria:
- <criterion>
prompt:
<work item prompt>
```

## Supervised Dogfood

Use `scripts/autopilot_dogfood_runner.py` when verifying the adapter signal
chain without claiming the host runtime hook path is solved. The runner creates
a local `.autopilot` run, calls the real no-args adapter with Stop-style input,
uses `autopilot_consistency_decision.py` to produce `continue_next`,
`retry_same_unit`, and `stop` decisions, and writes a summary JSON.

```bash
python3 scripts/autopilot_dogfood_runner.py \
  --project-dir /tmp/autopilot-dogfood \
  --prompt "approved work prompt" \
  --summary-json /tmp/autopilot-dogfood-summary.json
```

## Operating It

- Start: run `scripts/autopilot_start_run.py` after the user explicitly approves the autonomous run.
- Pause: create `.autopilot/OFF`.
- Resume: remove `.autopilot/OFF`.
- Stop: set `approved-run.json` `status` to `stopped`, set `approved` to false, set `budget.remaining_steps` to 0, or remove the approved run file.
- Replan: preserve terminal work items, then rewrite open work items in `tasks.jsonl`.

## Install and Remove

Use the Ghost-ALICE core installer. This addon does not install hooks directly.

From a Ghost-ALICE core checkout:

```bash
bash install.sh --addon autopilot
```

To install one platform only:

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

Use `--platform claude` for Claude Code.

The addon manifest requests `privileged_adapters: ["autopilot-mode"]`. The core-owned privileged adapter allowlist chooses the event, marker, runner namespace, and adapter script path.

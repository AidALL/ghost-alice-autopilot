# ghost-alice-autopilot

<p align="center">
  <img src="./logo/logo_inward_fade.png" alt="Ghost-ALICE Autopilot logo" width="360">
</p>

Official Ghost-ALICE addon for approved autonomous continuation.

Language: English | [Korean](./README_ko.md)

`autopilot-mode` lets Ghost-ALICE continue an approved run one work item at a time. After an agent stop event, it reads the project's `.autopilot/` state, chooses the next ready or reopened item, resumes an unresolved running item when current io-trace material exists, and emits the next continuation message.

## What This Addon Does

- Installs the `autopilot-mode` skill.
- Registers the core-owned `[adapter:autopilot-mode] continue` hook through the Ghost-ALICE installer.
- Reads project-local run state from `.autopilot/`.
- Provides `skill/scripts/autopilot_session_bridge.py` plus the repository wrapper `scripts/autopilot_session_bridge.py` to bootstrap `.autopilot/` from session-intent ledger files after explicit approval.
- Lets the Stop adapter materialize current-session `.autopilot/` state without adding a separate receptor when session intent records admitted, unmet acceptance criteria, or when open conduct feedback provides an approved conduct plan; io-trace material alone never bootstraps a run and flows through the `autopilot-observation-signal.v1` receptor as observation only.
- Provides `autopilot_governance_signal.py` for evidence-backed governance candidates and promotion.
- Imports approved `conduct-plan.json` proposal queues into durable `tasks.jsonl` work items.
- Emits either a no-op payload or a next-work-item message.
- Records adapter events in `.autopilot/events.jsonl`.

It does not invent work outside the current session. Session intent analysis, task routing, the user's explicit GO decision, and current-session runtime material create the approved run state.

## How It Works

Runtime loop:

1. The Ghost-ALICE core installer installs this addon and wires the privileged adapter hook.
2. A project creates `.autopilot/approved-run.json` and `.autopilot/tasks.jsonl` after user approval. A conduct-feedback run can instead provide an approved `.autopilot/conduct-plan.json`. The package bridge `skill/scripts/autopilot_session_bridge.py` or repository wrapper `scripts/autopilot_session_bridge.py` can create that run state from `current-session.json`, `intent-state.json`, and `intent-events.jsonl` when the caller supplies explicit approval evidence. The Stop adapter can also materialize the current session when session intent records admitted, unmet acceptance criteria, or when an approved conduct plan is present; io-trace material alone is observation/resume material, not bootstrap approval.
3. When the agent stops, the adapter reads `.autopilot/`.
4. Governance signals first write `consistency-decision.candidate.json` or `conduct-plan.candidate.json`; those candidate files are not adapter-consumable.
5. Only promotion creates adapter-consumable `consistency-decision.json` or approved `conduct-plan.json`.
6. If `conduct-plan.json` exists, the adapter imports new proposed queue items into `tasks.jsonl` before checking for a ready task.
7. If the run is approved, running, within budget, and has a ready or reopened task, the adapter marks that task `running`.
8. If a running task is missing a promoted decision but current io-trace exists, the adapter feeds io-trace through `autopilot-observation-signal.v1` and resumes the same task.
9. The adapter prints a continuation payload with the next work item and a `before-stop` instruction to write or promote `.autopilot/consistency-decision.json` when a decision is resolved.
10. If the run is not approved, paused, stopped, out of budget, or has no runnable item or runtime material, the adapter returns a no-op payload.

Default run directory:

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

## Governance Candidates And Promotion

`addons/autopilot-mode/skill/scripts/autopilot_governance_signal.py` converts session intent, conduct feedback, routing-surface corrections, and completion validation failures into evidence-backed candidate files. A candidate file is diagnostic only:

- `consistency-decision.candidate.json` uses `schema_version: "autopilot-consistency-decision-candidate.v1"`, `promotion_state: "candidate"`, and `action_file_allowed: false`.
- `conduct-plan.candidate.json` uses `schema_version: "autopilot-conduct-plan-candidate.v1"`, `promotion_state: "candidate"`, and `action_file_allowed: false`.
- The adapter rejects candidate schemas even if a candidate is accidentally placed at an adapter-consumable path.

Promotion is the boundary that creates adapter-consumable files. `promote-decision` writes a promoted `consistency-decision.json` with `schema_version: "autopilot-consistency-decision.v1"`, `promotion_state: "promoted"`, `promotion_evidence.decision`, `promotion_evidence.source`, `candidate_id`, `governance_signal_digest`, `state_hash`, `decision_key`, and `loop_key`. `promotion_evidence.decision` accepts `go`, `approve`, `approved`, `promote`, `promoted`, or `direct`; use `direct` only for a current-turn before-stop resolution without a candidate. In every promoted decision, `evidence` must be a JSON array of strings; do not nest `verdict`, `completion_check_digest`, or `text` inside it. `promote-conduct-plan` writes an approved `conduct-plan.json` with `promotion_state: "approved"`, approval evidence, source candidate id, and evidence digest.

The promotion command can read `.autopilot/tasks.jsonl` and `.autopilot/events.jsonl` with `--run-dir` so retry caps and repeated decision/state loops escalate to `ask_user_meta` instead of looping.

## Session-Intent Bridge

Installation alone does not create `.autopilot/`. To activate an approved run
from the current Ghost-ALICE session ledger, use the package bridge
`skill/scripts/autopilot_session_bridge.py` or the repository wrapper
`scripts/autopilot_session_bridge.py`. The bridge reads
`.tmp/session-intent/<platform>/current-session.json`, the pointed
`intent-state.json`, and sibling `intent-events.jsonl`, then writes
`.autopilot/approved-run.json` plus either a promoted `conduct-plan.json` or a
ready `tasks.jsonl` item.

The bridge supports `--platform codex` and `--platform claude`. It refuses to
write run state unless `--approval-evidence-json` contains an approval decision
(`GO`, `approve`, or `approved`) and a non-empty `source`, and it preserves
session event metadata in `approved-run.json` approval evidence.

The Stop adapter has a separate automatic current-session path. When the
project has no `.autopilot/` run state and the session ledger records
admitted, not-yet-met acceptance criteria, the adapter bootstraps run state
with `approval_evidence.decision: "AUTO"`
(`source: "admitted-unmet-criterion"`). Io-trace presence alone never
bootstraps a run; io-trace is routed through the existing
`autopilot-observation-signal.v1` receptor in
`autopilot_governance_signal.py`, and observation candidates stay diagnostic
and are not promoted into adapter-consumable action files.

```bash
/opt/homebrew/bin/python3 scripts/autopilot_session_bridge.py \
  --intent-root <ghost-alice>/.tmp/session-intent \
  --platform codex \
  --run-dir .autopilot \
  --current-work-item-id current \
  --plan-path .tmp/implementation-plans/current.md \
  --approval-evidence-json '{"decision":"GO","source":"user-confirmation"}'
```

## Requirements

- Ghost-ALICE core 0.2.1 or newer with privileged adapter support.
- Python 3.11+.
- Claude Code and/or Codex hooks installed by the Ghost-ALICE core installer.

Do not install this addon with Ghost-ALICE core older than 0.2.1. Older core installers may copy the skill without wiring the privileged adapter, runtime-core audit, or ledger met-flip path required by the current addon contract; that install is inert or incomplete and should be removed before upgrading.

## Compatibility Matrix

The compatibility SSOT is `compatibility-matrix.json`. It must be checked before making a full compatibility claim. The matrix records the current support posture, not a chronological test log; dated run artifacts belong in CI/test reports or release notes.

Current target status:

- macOS: `verified-local` with local unit tests and adapter subprocess simulation.
- Claude Code: `simulated-local` with temporary hook install and removal tests.
- Linux: `not-run`.
- Windows Command Prompt: `not-run`.
- Windows PowerShell 5: `not-run`.
- Windows PowerShell 7: `not-run`.
- Codex: `verified-local` with local install status, Codex live semantic E2E, and candidate-boundary checks.

Any `not-run` target blocks a full compatibility claim until runner evidence is attached to the matrix.
Linux and Windows runner targets still block a full compatibility claim.

## Install

Run these commands from a Ghost-ALICE core checkout. This addon repository does
not provide a standalone root `install.sh`.

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

From a project directory, create an approved run:

```bash
mkdir -p .autopilot
cat > .autopilot/approved-run.json <<'JSON'
{
  "schema_version": "autopilot-run.v1",
  "run_id": "demo-run",
  "approved": true,
  "status": "running",
  "scope": {"summary": "Demo autopilot continuation"},
  "budget": {"remaining_steps": 2},
  "allowed_surfaces": ["src/...", "tests/..."],
  "stop_conditions": ["budget_exhausted", "user_stop"],
  "approval_evidence": {"decision": "GO", "source": "user-confirmation"}
}
JSON

cat > .autopilot/tasks.jsonl <<'JSONL'
{"id":"unit-1","status":"ready","focus_layer":"micro","depends_on":[],"prompt":"Implement the first approved demo unit.","acceptance_criteria":["the next continuation message names unit-1"],"allowed_surface":["src/..."],"completion":{"state":"not_started","verdict":null,"evidence":[],"completion_check_digest":null,"reopen_target":null},"attempt":0}
JSONL
```

After the next agent stop event, the adapter should emit a continuation message shaped like this:

```text
[autopilot]
run: demo-run
work-item: unit-1
focus-layer: micro
io-trace:
- Bash n/a apply_patch current work
governance-signal:
- candidate: candidate-<digest>
- decision: reopen_micro
- source: observation_signal
governance-evidence:
- observation_next_action:continue from latest io-trace
allowed-surface:
- src/...
acceptance-criteria:
- the next continuation message names unit-1
before-stop:
- continue from the latest io-trace when no promoted consistency decision exists.
- promote a candidate with scripts/autopilot_governance_signal.py promote-decision when a candidate exists.
- otherwise write .autopilot/consistency-decision.json only with the full promoted schema when a completion/retry/reopen decision is resolved.
- promoted schema requires schema_version, decision_id, work_item_id, decision, promotion_state: promoted, promotion_evidence.decision, promotion_evidence.source, candidate_id, governance_signal_digest, decision_key, state_hash, loop_key, and evidence.
- promotion_evidence.decision must be one of go, approve, approved, promote, promoted, or direct; use direct only for a current-turn before-stop resolution without a candidate.
- evidence must be a JSON array of strings; do not nest verdict, completion_check_digest, or text inside evidence.
- for continue_next, put verdict and completion_check_digest at top level and put the full [completion-check] block in evidence strings.
- use continue_next only after [completion-check] with verdict pass, sha256 completion_check_digest, acceptance-criteria, and criterion-bound claim-evidence-map evidence.
- use retry_same_unit or reopen_micro/reopen_meso/reopen_macro when verification fails or drift remains.
- use ask_user_meta only when neither io-trace nor work state can resolve the next action.
prompt:
Implement the first approved demo unit.
```

The next stop event consumes promoted `.autopilot/consistency-decision.json`. A direct completion decision must still include the full promoted action schema named in the `before-stop` block; partial hand-written decisions are rejected and preserved as `.autopilot/consistency-decision.rejected.json`. `continue_next` completes the running item only with passing completion evidence: a `sha256:<64-hex>` `completion_check_digest` and evidence text containing `[completion-check]`, `acceptance-criteria`, and `claim-evidence-map` entries that reference known acceptance-criteria criterion ids. `retry_same_unit` queues the same item again only with concrete evidence. `reopen_micro`, `reopen_meso`, and `reopen_macro` keep the same item open and surface the requested focus layer in the next continuation message. If a running item has no decision file at the next stop, the adapter resumes that same item with `pending-decision: missing`; a repeated missing decision escalates to `ask_user_meta` only when neither io-trace nor work state can resolve the next action.

## Pause, Resume, Stop

Pause:

```bash
touch .autopilot/OFF
```

Resume:

```bash
rm .autopilot/OFF
```

Stop by doing any one of these:

- set `approved-run.json` `status` to `stopped`
- set `approved` to false
- set `budget.remaining_steps` to 0
- remove `approved-run.json`

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
- The adapter mutates only project-local `.autopilot/` run-state files and emits a continuation payload.
- The continuation payload contains a `before-stop` contract so an executing agent leaves a promoted `.autopilot/consistency-decision.json` before it stops.
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

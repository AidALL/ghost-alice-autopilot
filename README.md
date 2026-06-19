# ghost-alice-autopilot

Official Ghost-ALICE addon for approved autonomous continuation.

Language: English | [Korean](./README_ko.md)

`autopilot-mode` lets Ghost-ALICE continue an approved run one work item at a time. After an agent stop event, it reads the project's `.autopilot/` state, chooses the next ready item, marks that item as running, and emits the next continuation message.

## What This Addon Does

- Installs the `autopilot-mode` skill.
- Registers the core-owned `[adapter:autopilot-mode] continue` hook through the Ghost-ALICE installer.
- Reads project-local run state from `.autopilot/`.
- Emits either a no-op payload or a next-work-item message.
- Records adapter events in `.autopilot/events.jsonl`.

It does not decide that a run should start. Session intent analysis, task routing, and the user's explicit GO decision create the approved run state.

## How It Works

Runtime loop:

1. The Ghost-ALICE core installer installs this addon and wires the privileged adapter hook.
2. A project creates `.autopilot/approved-run.json` and `.autopilot/tasks.jsonl` after user approval.
3. When the agent stops, the adapter reads `.autopilot/`.
4. If the run is approved, running, within budget, and has a ready task, the adapter marks that task `running`.
5. The adapter prints a continuation payload with the next work item.
6. If the run is not approved, paused, stopped, out of budget, or has no ready item, the adapter returns a no-op payload.

Default run directory:

```text
<project>/.autopilot/
  approved-run.json
  tasks.jsonl
  consistency-decision.json
  consistency-decision.applied.json
  events.jsonl
  OFF
```

## Requirements

- Ghost-ALICE core 0.1.3 or newer with privileged adapter support.
- Python 3.11+.
- Claude Code and/or Codex hooks installed by the Ghost-ALICE core installer.

Do not install this addon with Ghost-ALICE core older than 0.1.3. Older core installers may copy the skill without wiring the privileged adapter; that install is inert and should be removed before upgrading.

## Install

Local checkout:

```bash
bash <ghost-alice>/install.sh \
  --platform claude \
  --addon-source /path/to/ghost-alice-autopilot
```

Git URL source:

```bash
bash <ghost-alice>/install.sh \
  --platform claude \
  --addon-source https://github.com/AidALL/ghost-alice-autopilot.git
```

Use `--platform codex` for Codex.

Check install status:

```bash
bash <ghost-alice>/install.sh --platform claude --status
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
allowed-surface:
- src/...
acceptance-criteria:
- the next continuation message names unit-1
prompt:
Implement the first approved demo unit.
```

## Demo Video

Recommended video flow:

1. Install from a local checkout or Git URL.
2. Create `.autopilot/approved-run.json`.
3. Create `.autopilot/tasks.jsonl`.
4. End an agent turn and show the `[autopilot]` continuation message.
5. Run per-addon uninstall and show the adapter hook is gone.

Add the final asset here when recorded:

```text
docs/demo/autopilot-mode.mp4
```

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
  --platform claude \
  --uninstall --addon autopilot-mode
```

Use `--platform codex` for Codex.

Full Ghost-ALICE uninstall still uses the core full-uninstall path:

```bash
bash <ghost-alice>/install.sh --platform claude --uninstall
```

## Limits And Trust Notes

- Installing the addon is not runtime activation.
- The adapter accepts no arguments.
- The adapter only reads `.autopilot/` state and emits a continuation payload.
- Tool denial, installer policy, privileged adapter allowlists, hook markers, runner namespaces, and hook install/remove behavior are owned by Ghost-ALICE core.
- This addon package owns the skill content and adapter implementation.

## Repository Layout

```text
addons-manifest.json
addons/autopilot-mode/
  addon.json
  skill/SKILL.md
  skill/adapters/autopilot_mode.py
  skill/adapters/autopilot_state.py
tests/
```

## License

Apache-2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).

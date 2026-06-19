# ghost-alice-autopilot

Official Ghost-ALICE addon for approved autonomous continuation.

Language: English | [Korean](./README_ko.md)

`autopilot-mode` installs a Ghost-ALICE P6 privileged adapter. The adapter continues the next approved work item after an agent stop event, but only when project-local approved-run state says the user has explicitly allowed the autonomous run.

This repository owns the addon package. The Ghost-ALICE core owns installer policy, privileged adapter allowlists, hook markers, runner namespaces, and hook install/remove behavior.

## Requirements

- Ghost-ALICE core with P6 privileged adapter support.
- Python 3.11+.
- Claude Code and/or Codex hooks installed by the Ghost-ALICE core installer.

## Install

Install through the Ghost-ALICE core installer. Do not run a separate addon hook installer for P6.

```bash
bash <ghost-alice>/install.sh --addon-source /path/to/ghost-alice-autopilot --platform claude
```

Use `--platform codex` for Codex. The core installer reads this repo's `addons-manifest.json`, installs the skill, and wires the core-owned `[adapter:autopilot-mode] continue` hook.

## Runtime Activation

Installation is inert by default. To activate a run, create approved state in the project:

```text
<project>/.autopilot/
  approved-run.json
  tasks.jsonl
```

`approved-run.json` must contain an explicit GO boundary:

```json
{
  "schema_version": "autopilot-run.v1",
  "run_id": "run-1",
  "approved": true,
  "status": "running",
  "scope": {"summary": "Approved work scope"},
  "budget": {"remaining_steps": 3},
  "allowed_surfaces": ["src/...", "tests/..."],
  "stop_conditions": ["budget_exhausted", "user_stop"],
  "approval_evidence": {"decision": "GO", "source": "user-confirmation"}
}
```

`tasks.jsonl` is the durable source of truth. The adapter derives the ready queue from task status and dependencies; it never pops lines from the file.

Pause a run with:

```bash
touch .autopilot/OFF
```

Resume with:

```bash
rm .autopilot/OFF
```

Stop by setting `approved-run.json` `status` to `stopped`, setting `approved` to false, exhausting `budget.remaining_steps`, or removing `approved-run.json`.

## Adapter Behavior

- Accepts no arguments.
- Defaults to `<cwd>/.autopilot/`.
- Supports `GHOST_ALICE_AUTOPILOT_RUN_DIR` for an explicit run directory.
- Consumes `consistency-decision.json` when present.
- Writes `events.jsonl` audit records.
- Emits either a no-op payload or the next work-item continuation message.

## Uninstall

Use the Ghost-ALICE core uninstall path:

```bash
bash <ghost-alice>/install.sh --uninstall --platform claude
```

The core uninstaller removes the managed adapter hook and preserves addon-owned files according to the core addon uninstall policy.

## Layout

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

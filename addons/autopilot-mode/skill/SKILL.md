---
name: autopilot-mode
description: "Use when the user wants continuous, uninterrupted autonomous execution of a known multi-task list across turns on Claude Code or Codex, with no manual nudging between tasks. Verification-gated and opt-in per project."
compatibility:
  - "Python 3.11+ standard library"
  - "Claude Code Stop + UserPromptSubmit hooks (~/.claude/settings.json)"
  - "Codex CLI Stop + UserPromptSubmit hooks (~/.codex/hooks.json), v0.114+"
  - "Ghost-ALICE core: reuses _shared/completion_check_validator.py and _shared/claude_stop_verification_hook.py"
---

# autopilot-mode

Ghost-ALICE addon. It drains a per-project task queue: after each turn it injects the next queued task, but only when that turn passed the Ghost-ALICE completion gate. It is an autonomy amplifier that rides on the verification gate; it never weakens it.

Required background: read coding-convention/verification-before-completion. autopilot-mode reuses that gate's own validator to decide when to advance.

## When to use

- The user wants a known list of tasks executed back to back without manual "continue" between each.
- Every task can close with a valid [completion-check]; nothing in the queue is irreversible or outward-facing.
- Do not use for one-off tasks, or when a human checkpoint between tasks is required.

## Cross-platform (Claude Code + Codex CLI)

The engine is one Stop hook (scripts/autopilot_stop_hook.py), selected by `--platform`. Both runtimes support a Stop hook that prints `{"decision":"block","reason":...}` to continue with `reason` as the next prompt.

| | Claude Code | Codex CLI |
|---|---|---|
| Hooks config | ~/.claude/settings.json | ~/.codex/hooks.json (v0.114+) |
| Final-text source | last assistant message in transcript | last_assistant_message (falls back to transcript) |
| Skill-loaded check | transcript Skill tool_use this turn | trusted at text level |
| Flag | --platform claude | --platform codex |

There is no `/autopilot` slash command on Codex; the engine is the hook, which runs on both.

## How a project opts in

The hook is cwd-gated: inert unless the current project has a queue file.

```
mkdir -p .autopilot
printf '%s\n' '{"task":"first task"}' '{"task":"second task"}' > .autopilot/queue.jsonl
```

State lives under `<project>/.autopilot/`:

| File | Role |
|---|---|
| queue.jsonl | Task list, one per line (`{"task":"..."}` or plain text). Presence = opt-in. |
| OFF | If present, autopilot is disabled (fast pause without deleting the queue). |
| inject_count | Auto-injection counter for this batch. Reset on each new user prompt. |

## Verification lock (the core invariant)

The autopilot advances only when the Ghost-ALICE verification gate would itself allow the stop for a valid completion claim. scripts/autopilot_stop_hook.py imports the gate's own `completion_check_validator.validate_completion_text` plus the gate transcript helpers, so its definition of "verified" is identical to the gate's, never a substring proxy. It fires only when all hold:

1. `<cwd>/.autopilot/queue.jsonl` exists and is non-empty.
2. `<cwd>/.autopilot/OFF` does not exist.
3. inject_count is below MAX_INJECTIONS (default 25).
4. The final response carries a [completion-check] marker and `validate_completion_text(..., require_completion_check=True)` returns None.
5. Claude: the verification skill was loaded this turn. Codex: the text-level skill-call is trusted.

Because the gate blocks exactly when (4) fails and the autopilot fires exactly when (4) passes, the two Stop hooks are mutually exclusive and can never both block.

## Operating it

- Start: create `.autopilot/queue.jsonl`. Effective from the next turn that passes verification.
- Pause: `touch .autopilot/OFF`. Resume: `rm .autopilot/OFF`.
- Add work mid-run: append lines to `.autopilot/queue.jsonl`.
- Stop entirely: empty or delete `.autopilot/queue.jsonl`.
- Budget: up to MAX_INJECTIONS auto-injections per user prompt; send any new message to reset the budget.

## Critical rules and gotchas

- Fail-safe: any error, a missing queue, the OFF switch, an exhausted counter, or an inability to import the core gate logic results in allow-stop. The hook never blocks on error and refuses to advance on a weaker check than the gate's.
- Prohibited: queueing irreversible or outward-facing tasks. Autopilot removes the per-task human checkpoint; only queue work safe to run unattended.
- Hooks load at session start. Registering or moving a hook only affects new sessions, not the current one.
- Do not point two autopilot Stop hooks at the same project and platform; that double-pops the queue. Keep one global registration per platform.
- Gate-format dependency: the gate's `_SKILLS_LOADED_RE` requires io-trace `- skills-loaded: [verification-before-completion, ...]` in square brackets. A turn whose completion-check omits the brackets is treated as unverified, so the autopilot will not advance on it.
- Core dependency: the hook imports from `~/ghost-alice/_shared`. This addon requires the Ghost-ALICE core install (declared in addon.json `depends_on_core`).

## Installation

This is a self-contained addon; it does not modify the Ghost-ALICE core installer. Install in two steps: the skill via the core installer, then the hooks via this addon's own script.

```
bash <ghost-alice>/install.sh --addon-source <this-addon-repo> --platform claude
bash <this-addon-repo>/install-hooks.sh --platform claude
```

Remove with `bash <this-addon-repo>/uninstall-hooks.sh --platform claude` (hooks) and `bash <ghost-alice>/install.sh --uninstall --platform claude` (skill). The hook engine lives in scripts/autopilot_stop_hook.py and scripts/reset_inject_count.py; install-hooks.sh wires them per platform pointing at the installed skill.

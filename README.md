<div align="center">

# ghost-alice-autopilot

Verification-gated autonomous task-drain for Claude Code and Codex CLI.

Language: 🇺🇸 English | [🇰🇷 한국어](./README_ko.md)

</div>

`autopilot-mode` is the first official [Ghost-ALICE OS](https://github.com/AidALL/ghost-alice) addon. It makes the agent keep pulling the next queued task after each turn, with no manual nudging — but only when that turn passed the Ghost-ALICE completion gate. It is an autonomy amplifier that rides on the verification gate; it never weakens it.

This addon is fully self-contained: it does not modify the Ghost-ALICE core or its installer. The core installs the skill; this addon ships its own hook lifecycle.

## Why it is safe (the verification lock)

The autopilot advances only when the Ghost-ALICE verification gate would itself allow the stop for a valid completion claim. The Stop hook imports the core gate's own validator (`completion_check_validator.validate_completion_text`) and transcript helpers, so its definition of "verified" is identical to the gate's, never a substring proxy. If it cannot import the core gate logic, it refuses to advance (fails safe). The verification Stop hook and the autopilot Stop hook are mutually exclusive: the gate blocks on an invalid completion-check, the autopilot fires only on a valid one.

## Requirements

- [Ghost-ALICE OS](https://github.com/AidALL/ghost-alice) core installed (this addon imports the core verification gate from `~/ghost-alice/_shared`).
- Python 3.11+.
- Claude Code and/or Codex CLI (v0.114+ for Codex hooks).

## Install

Two steps. First install the skill with the Ghost-ALICE core installer, then wire the hooks with this repo's script.

```bash
# 1) install the skill (core installer; records it in install-state)
bash <ghost-alice>/install.sh --addon-source /path/to/ghost-alice-autopilot --platform claude

# 2) wire the autopilot Stop + UserPromptSubmit hooks (this repo)
bash install-hooks.sh --platform claude
```

Replace `claude` with `codex` for Codex, or omit `--platform` to target every detected platform. Hooks load at session start, so they take effect in new sessions.

## Turn it on in a project

The autopilot is cwd-gated: inert unless the current project carries a queue file.

```bash
mkdir -p .autopilot
printf '%s\n' '{"task":"first task"}' '{"task":"second task"}' > .autopilot/queue.jsonl
```

State lives under `<project>/.autopilot/`:

| File | Role |
|---|---|
| `queue.jsonl` | Task list, one per line (`{"task":"..."}` or plain text). Presence = opt-in. |
| `OFF` | If present, autopilot is paused (without deleting the queue). |
| `inject_count` | Auto-injection counter for the current batch; reset on each new user prompt. |

Pause: `touch .autopilot/OFF`. Resume: `rm .autopilot/OFF`. Cap: `MAX_INJECTIONS` per batch (default 25). No `.autopilot/queue.jsonl` means the hook is fully inert.

## Uninstall

```bash
bash uninstall-hooks.sh --platform claude              # remove the hooks (this repo)
bash <ghost-alice>/install.sh --uninstall --platform claude   # remove the skill (core installer)
```

## Layout

```
addons-manifest.json            # Ghost-ALICE addon manifest (manifest_version 1)
addons/autopilot-mode/
  addon.json                    # addon metadata (depends_on_core: verification-before-completion)
  skill/SKILL.md                # the skill (operator interface)
  skill/scripts/                # the hook engine
    autopilot_stop_hook.py      # gate-locked Stop hook
    reset_inject_count.py       # UserPromptSubmit budget reset
install-hooks.sh                # self-contained hook installer (this repo)
uninstall-hooks.sh              # self-contained hook remover (this repo)
scripts/manage_hooks.py         # idempotent, marker-based hook wiring (no core dependency)
tests/                          # addon tests
```

## License

Apache-2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).

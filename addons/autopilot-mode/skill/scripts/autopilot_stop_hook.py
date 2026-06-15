#!/usr/bin/env python3
"""
Dependencies: Python 3.11+ stdlib; Ghost-ALICE core ~/ghost-alice/_shared (completion_check_validator, claude_stop_verification_hook).
Ghost-ALICE AUTOPILOT -- gate-locked Stop hook (cwd-gated, claude + codex).

This hook NEVER weakens the Ghost-ALICE completion gate. It runs as an ADDITIONAL
Stop hook beside the verification gate (`claude_stop_verification_hook.py`) and
injects the next queued task ONLY when that gate would itself have ALLOWED the
stop with a valid completion claim -- i.e. the prior turn produced a structurally
valid `[completion-check]`. To guarantee this, it reuses the gate's OWN validator
and transcript helpers rather than a substring heuristic, so "verified" is
identical to the gate's pass condition and the two hooks can never both block.

Composition (mutually exclusive by construction):
  - completion-check INVALID / missing-with-claim -> gate BLOCKS (fix verification),
    autopilot stays inert.
  - completion-check VALID -> gate ALLOWS, autopilot injects the next task.

Fail-safe: ANY error, missing queue, OFF switch, exhausted counter, or inability
to import the gate logic => allow stop (it never blocks on error, and it refuses
to advance on a weaker check than the gate's). At most it converts a stop into
ONE more turn, bounded by MAX_INJECTIONS + the monotonically draining queue.

Mechanism (Claude Code & Codex hooks both support this):
  - stdout {"decision":"block","reason":"..."} => one more turn, reason injected.
  - empty stdout + exit 0 => allow the agent to stop.

cwd resolution: payload 'cwd' (or 'cwd'-equivalent) else os.getcwd(). ALL state
(queue, OFF, inject_count) lives under <cwd>/.autopilot/, so projects never share
autopilot state. Platform is selected via `--platform claude|codex` (default
claude), mirroring the verification gate's own platform handling.

Safety bounds (cannot infinite-loop / cannot runaway):
  1. Opt-in: only active when <cwd>/.autopilot/queue.jsonl exists.
  2. Off-switch: <cwd>/.autopilot/OFF disables it.
  3. Queue is monotonically popped (strictly decreasing -> terminates when empty).
  4. MAX_INJECTIONS hard counter (reset by the UserPromptSubmit reset hook).
  5. Verification lock: only injects when the verification gate would have ALLOWED
     this stop for a valid completion claim (reuses the gate's validator).
"""
import argparse
import json
import os
import sys

MAX_INJECTIONS = 25
GHOST_ALICE_SHARED = os.path.expanduser("~/ghost-alice/_shared")


def allow_stop():
    # No stdout + exit 0 => the agent is allowed to stop.
    sys.exit(0)


def read_count(path):
    try:
        return int(open(path).read().strip())
    except Exception:
        return 0


def parse_platform(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--platform", choices=("claude", "codex"), default="claude")
    return parser.parse_known_args(argv)[0].platform


def gate_would_allow_completion(data, platform):
    """True iff the Ghost-ALICE verification gate would ALLOW this stop *and* the
    final turn is an actual valid completion claim.

    Reuses the gate's own validator + transcript helpers so this is IDENTICAL to
    the gate's pass condition. Returns False (refuse to advance) if the gate logic
    cannot be imported -- never falls back to a weaker check.
    """
    try:
        if GHOST_ALICE_SHARED not in sys.path:
            sys.path.insert(0, GHOST_ALICE_SHARED)
        from completion_check_validator import (  # noqa: E402
            looks_like_completion_claim,
            validate_completion_text,
        )
        from claude_stop_verification_hook import (  # noqa: E402
            _assistant_text_from_stop_input,
            _assistant_text_this_turn,
            _iter_transcript,
            _verification_skill_loaded_this_turn,
        )
    except Exception:
        return False

    entries = _iter_transcript(data.get("transcript_path") or data.get("transcriptPath"))
    input_text = _assistant_text_from_stop_input(data)
    if platform == "codex":
        # Mirror the gate: on codex the Skill tool_use is not reliably observable,
        # so the gate trusts the text-level skill-call evidence.
        final_text = input_text or _assistant_text_this_turn(entries)
        skill_loaded = True
    else:
        final_text = _assistant_text_this_turn(entries) or input_text
        skill_loaded = _verification_skill_loaded_this_turn(entries)

    if not final_text.strip():
        return False
    if not looks_like_completion_claim(final_text):
        return False
    if validate_completion_text(final_text, require_completion_check=True) is not None:
        return False
    return bool(skill_loaded)


def main():
    platform = parse_platform(sys.argv[1:])
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    cwd = data.get("cwd") or data.get("workdir") or os.getcwd()
    base = os.path.join(cwd, ".autopilot")
    queue = os.path.join(base, "queue.jsonl")
    off = os.path.join(base, "OFF")
    count_path = os.path.join(base, "inject_count")

    # 1. opt-in gate: this project must carry an .autopilot/queue.jsonl. Without it
    # the hook is fully inert -- this is what makes a GLOBAL Stop hook safe.
    if not os.path.exists(queue):
        allow_stop()
    # 2. off-switch
    if os.path.exists(off):
        allow_stop()
    # 3. hard counter (reset by the UserPromptSubmit reset hook on new input)
    n = read_count(count_path)
    if n >= MAX_INJECTIONS:
        allow_stop()
    # 4. queue present + non-empty
    try:
        lines = [l for l in open(queue, encoding="utf-8").read().splitlines() if l.strip()]
    except Exception:
        allow_stop()
    if not lines:
        allow_stop()

    # 5. VERIFICATION LOCK: only advance when the gate itself would have allowed
    # this stop for a valid completion claim. Identical to the gate's pass logic.
    if not gate_would_allow_completion(data, platform):
        allow_stop()

    # pop the next task
    first, rest = lines[0], lines[1:]
    try:
        task = json.loads(first).get("task", first)
    except Exception:
        task = first

    try:
        with open(queue, "w", encoding="utf-8") as f:
            f.write(("\n".join(rest) + "\n") if rest else "")
        with open(count_path, "w") as f:
            f.write(str(n + 1))
    except Exception:
        allow_stop()

    reason = (
        "[autopilot] 직전 작업의 [completion-check] 검증이 통과되어 다음 큐 작업을 자동 주입합니다. "
        "Ghost-ALICE 규율(task-router → boundary-contract → verification-before-completion)을 "
        "그대로 따르고 완료 시 [completion-check]로 닫으세요. 남은 큐: %d개.\n\n다음 작업:\n%s"
    ) % (len(rest), task)

    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()

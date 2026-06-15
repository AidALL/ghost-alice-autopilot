#!/usr/bin/env python3
"""
Dependencies: Python 3.11+ stdlib only.
Ghost-ALICE AUTOPILOT -- UserPromptSubmit reset hook.

A new human prompt should reset the autopilot's MAX_INJECTIONS budget, so the
queue can keep draining across the NEXT batch of forced-continuation turns. This
clears <cwd>/.autopilot/inject_count. cwd-gated like the Stop hook; fully
fail-safe no-op (never blocks input, never errors out of the hook chain).
"""
import json
import os
import sys


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    cwd = data.get("cwd") or os.getcwd()
    path = os.path.join(cwd, ".autopilot", "inject_count")
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    # exit 0, no stdout => does not modify or block the user prompt.
    sys.exit(0)


if __name__ == "__main__":
    main()

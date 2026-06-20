#!/usr/bin/env python3
"""Start an approved Ghost-ALICE autopilot run from a GO spec.

The script is not a privileged adapter. It is the explicit bridge from a
user-approved run plan to the durable `.autopilot` state consumed by the Stop
adapter.

Dependencies: Python 3.11+ standard library and the sibling `adapters/`
directory from this installed skill copy. It runs standalone with no network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = SCRIPT_DIR.parent / "adapters"
sys.path.insert(0, str(ADAPTER_DIR))

import autopilot_state as aps  # type: ignore[import-not-found]  # noqa: E402


def _read_spec(path: Path | None) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8") if path else sys.stdin.read()
    except OSError as exc:
        raise aps.AutopilotStateError(f"could not read start spec: {exc}") from exc
    if not raw.strip():
        raise aps.AutopilotStateError("start spec is empty")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise aps.AutopilotStateError(f"start spec is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise aps.AutopilotStateError("start spec must be a JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--spec-json", type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--replace-active", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        spec = _read_spec(args.spec_json)
        project_dir = args.project_dir.expanduser()
        run_dir = args.run_dir.expanduser() if args.run_dir else project_dir / ".autopilot"
        project_dir.mkdir(parents=True, exist_ok=True)
        summary = aps.start_approved_run(run_dir, spec, replace_active=args.replace_active)
    except aps.AutopilotActiveRunError as exc:
        sys.stderr.write(f"{exc}\n")
        return 65
    except aps.AutopilotStateError as exc:
        sys.stderr.write(f"{exc}\n")
        return 64

    if args.summary_json:
        _write_json(args.summary_json.expanduser(), summary)
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

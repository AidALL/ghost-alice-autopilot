#!/usr/bin/env python3
"""Supervised dogfood runner for the Ghost-ALICE autopilot adapter.

This runner is intentionally not a privileged adapter. It bootstraps a local
`.autopilot` run state, calls the real adapter with Codex-style Stop payloads,
and records the signal chain so completion can be verified without pretending
that the interactive runtime hook path is already solved.

Dependencies: Python 3.11+ standard library and the sibling `adapters/`
directory from this installed skill copy. It runs standalone with no network.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ADAPTER_DIR = SKILL_DIR / "adapters"
ADAPTER_SCRIPT = ADAPTER_DIR / "autopilot_mode.py"
DECISION_SCRIPT = SCRIPT_DIR / "autopilot_consistency_decision.py"
sys.path.insert(0, str(ADAPTER_DIR))

import autopilot_state as aps  # type: ignore[import-not-found]  # noqa: E402


def _json_dump(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _jsonl_read(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run_record(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": "autopilot-run.v1",
        "run_id": run_id,
        "approved": True,
        "status": "running",
        "scope": {
            "summary": "Supervised dogfood run for the autopilot signal chain",
        },
        "budget": {"remaining_steps": 4},
        "allowed_surfaces": ["report.md", ".autopilot/..."],
        "stop_conditions": ["budget_exhausted", "user_stop", "ask_user_meta"],
        "approval_evidence": {
            "decision": "GO",
            "source": "supervised-dogfood-runner",
        },
    }


def _work_item(
    item_id: str,
    *,
    focus_layer: str,
    prompt: str,
    acceptance_criteria: list[str],
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "status": "ready",
        "focus_layer": focus_layer,
        "depends_on": depends_on or [],
        "prompt": prompt,
        "acceptance_criteria": acceptance_criteria,
        "allowed_surface": ["report.md"],
        "completion": {
            "state": "not_started",
            "verdict": None,
            "evidence": [],
            "completion_check_digest": None,
            "reopen_target": None,
        },
        "attempt": 0,
    }


def bootstrap_run(project_dir: Path, prompt: str, *, run_id: str) -> Path:
    run_dir = project_dir / ".autopilot"
    project_dir.mkdir(parents=True, exist_ok=True)
    for residue in (aps.DECISION_FILE, aps.APPLIED_DECISION_FILE, aps.EVENTS_FILE, aps.OFF_FILE):
        try:
            (run_dir / residue).unlink()
        except FileNotFoundError:
            pass
    _json_dump(run_dir / aps.APPROVED_RUN_FILE, _run_record(run_id))
    aps.write_work_items(
        run_dir / aps.TASKS_FILE,
        [
            _work_item(
                "scope-map",
                focus_layer="macro",
                prompt=(
                    "Map the autopilot dogfood scope from the user prompt. Identify the work boundary, "
                    "signal choreography, and evidence needed before claiming autopilot completion.\n\n"
                    f"User prompt:\n{prompt}"
                ),
                acceptance_criteria=[
                    "scope boundary is explicit",
                    "signal producer and consumer blocks are named",
                    "completion evidence is listed",
                ],
            ),
            _work_item(
                "integration-gap",
                focus_layer="meso",
                depends_on=["scope-map"],
                prompt=(
                    "Use the scope map to inspect the remaining autopilot integration gap. Separate "
                    "command-level adapter success from prompt-level runtime hook firing.\n\n"
                    f"User prompt:\n{prompt}"
                ),
                acceptance_criteria=[
                    "command-level and prompt-level results are separated",
                    "remaining hook integration gap is represented as evidence",
                    "next implementation or verification action is explicit",
                ],
            ),
        ],
    )
    return run_dir


def _adapter_payload(project_dir: Path, run_dir: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)
    result = subprocess.run(
        [sys.executable, str(ADAPTER_SCRIPT)],
        cwd=project_dir,
        env=env,
        input=json.dumps({
            "hook_event_name": "Stop",
            "cwd": str(project_dir),
            "model": "gpt-5.5",
            "permission_mode": "bypassPermissions",
        }),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"adapter exited {result.returncode}: {result.stderr}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"adapter returned invalid JSON: {result.stdout!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("adapter returned non-object JSON")
    return payload


def _payload_text(payload: dict[str, Any]) -> str:
    value = payload.get("reason") or payload.get("systemMessage") or ""
    return value if isinstance(value, str) else ""


def _payload_kind(payload: dict[str, Any]) -> str:
    return "continuation" if _payload_text(payload) else "noop"


def _snapshot_step(stage: str, payload: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    tasks = _jsonl_read(run_dir / aps.TASKS_FILE)
    run = json.loads((run_dir / aps.APPROVED_RUN_FILE).read_text(encoding="utf-8"))
    return {
        "stage": stage,
        "payload_kind": _payload_kind(payload),
        "payload_text": _payload_text(payload),
        "tasks": [
            {
                "id": item["id"],
                "status": item["status"],
                "attempt": item["attempt"],
                "focus_layer": item["focus_layer"],
            }
            for item in tasks
        ],
        "events": _jsonl_read(run_dir / aps.EVENTS_FILE),
        "remaining_steps": run["budget"]["remaining_steps"],
    }


def _completion_response(work_item_id: str, verdict: str) -> str:
    return "\n".join([
        "[completion-check]",
        "- verification-before-completion: done",
        "- skill-call: verification-before-completion (this turn)",
        "- acceptance-criteria:",
        f"  - {work_item_id}: dogfood criterion [source: user-explicit]",
        "- claim-evidence-map:",
        f"  - claim: {work_item_id} dogfood work handled",
        f"    criterion: {work_item_id}",
        f"    evidence: supervised dogfood evidence for {work_item_id}",
        f"    verdict: {verdict}",
        "- unverified:",
        "  - none",
        f"- evidence: supervised dogfood evidence for {work_item_id}",
        "",
        "[io-trace]",
        "- commands-run: [autopilot dogfood runner]",
        "- skills-loaded: [verification-before-completion]",
    ])


def _decision_from_completion(run_dir: Path, *, work_item_id: str, verdict: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DECISION_SCRIPT),
            "--run-dir",
            str(run_dir),
            "--work-item-id",
            work_item_id,
        ],
        input=_completion_response(work_item_id, verdict),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"decision writer exited {result.returncode}: {result.stderr}")


def _explicit_decision(run_dir: Path, *, work_item_id: str, decision: str, evidence: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(DECISION_SCRIPT),
            "--run-dir",
            str(run_dir),
            "--work-item-id",
            work_item_id,
            "--decision",
            decision,
            "--evidence",
            evidence,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"decision writer exited {result.returncode}: {result.stderr}")


def run_dogfood(project_dir: Path, prompt: str, *, run_id: str) -> dict[str, Any]:
    run_dir = bootstrap_run(project_dir, prompt, run_id=run_id)
    steps: list[dict[str, Any]] = []

    payload = _adapter_payload(project_dir, run_dir)
    steps.append(_snapshot_step("start-first", payload, run_dir))

    _decision_from_completion(run_dir, work_item_id="scope-map", verdict="pass")
    payload = _adapter_payload(project_dir, run_dir)
    steps.append(_snapshot_step("continue-to-second", payload, run_dir))

    _decision_from_completion(run_dir, work_item_id="integration-gap", verdict="fail")
    payload = _adapter_payload(project_dir, run_dir)
    steps.append(_snapshot_step("retry-second", payload, run_dir))

    _explicit_decision(
        run_dir,
        work_item_id="integration-gap",
        decision="stop",
        evidence="supervised dogfood stop after retry evidence capture",
    )
    payload = _adapter_payload(project_dir, run_dir)
    steps.append(_snapshot_step("stop-second", payload, run_dir))

    tasks = _jsonl_read(run_dir / aps.TASKS_FILE)
    run = json.loads((run_dir / aps.APPROVED_RUN_FILE).read_text(encoding="utf-8"))
    return {
        "schema_version": "autopilot-dogfood-summary.v1",
        "prompt": prompt,
        "project_dir": str(project_dir),
        "run_dir": str(run_dir),
        "steps": steps,
        "final": {
            "statuses": [item["status"] for item in tasks],
            "attempts": [item["attempt"] for item in tasks],
            "remaining_steps": run["budget"]["remaining_steps"],
            "events": _jsonl_read(run_dir / aps.EVENTS_FILE),
            "applied_decision_exists": (run_dir / aps.APPLIED_DECISION_FILE).is_file(),
        },
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--run-id", default="autopilot-dogfood")
    parser.add_argument("--summary-json", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    summary = run_dogfood(args.project_dir.expanduser(), args.prompt, run_id=args.run_id)
    if args.summary_json:
        _json_dump(args.summary_json.expanduser(), summary)
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

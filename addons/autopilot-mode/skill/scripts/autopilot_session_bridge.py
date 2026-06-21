#!/usr/bin/env python3
"""Bootstrap autopilot run state from a session-intent ledger.

This bridge is deliberately approval-gated. It can read session-intent state
and event metadata, but it writes adapter-consumable autopilot state only when
explicit approval evidence is supplied by the caller.

Dependencies: Python 3.11+ standard library plus sibling skill scripts.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping


class BridgeError(ValueError):
    """Raised when the session-intent bridge cannot safely write run state."""


def _load_session_material_module():
    path = Path(__file__).resolve().parent / "autopilot_session_material.py"
    spec = importlib.util.spec_from_file_location("autopilot_session_material", path)
    if spec is None or spec.loader is None:
        raise BridgeError(f"cannot load session material module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SESSION_MATERIAL = _load_session_material_module()
DEFAULT_STOP_CONDITIONS = SESSION_MATERIAL.DEFAULT_STOP_CONDITIONS
APPROVAL_DECISIONS = {"go", "approve", "approved"}


def _read_json_object(path: str | Path) -> dict[str, Any]:
    try:
        return SESSION_MATERIAL.read_json_object(path)
    except ValueError as exc:
        raise BridgeError(str(exc)) from exc


def _read_jsonl_objects(path: str | Path) -> list[dict[str, Any]]:
    try:
        return SESSION_MATERIAL.read_jsonl_objects(path)
    except ValueError as exc:
        raise BridgeError(str(exc)) from exc


def _write_json_atomic(path: str | Path, value: Mapping[str, Any]) -> None:
    SESSION_MATERIAL.write_json_atomic(path, value)


def _load_governance_signal_module():
    try:
        return SESSION_MATERIAL.load_governance_signal_module(required=True)
    except ValueError as exc:
        raise BridgeError(str(exc)) from exc


def _safe_id(value: str) -> str:
    return SESSION_MATERIAL.safe_id(value)


def _write_jsonl_atomic(path: str | Path, rows: list[Mapping[str, Any]]) -> None:
    SESSION_MATERIAL.write_jsonl_atomic(path, rows)


def _resolve_state_path(intent_root: Path, platform: str, session_id: str | None) -> Path:
    platform_root = intent_root / platform
    if session_id:
        return platform_root / session_id / "intent-state.json"

    pointer_path = platform_root / "current-session.json"
    pointer = _read_json_object(pointer_path)
    if pointer.get("schema_version") != "session-intent-current.v1":
        raise BridgeError(f"{pointer_path}: schema_version must be 'session-intent-current.v1'")
    pointer_state_path = pointer.get("state_path")
    if not isinstance(pointer_state_path, str) or not pointer_state_path.strip():
        pointer_session_id = pointer.get("session_id")
        if isinstance(pointer_session_id, str) and pointer_session_id.strip():
            return platform_root / pointer_session_id / "intent-state.json"
        raise BridgeError(f"{pointer_path}: missing state_path and session_id")

    path = Path(pointer_state_path)
    if not path.is_absolute():
        path = pointer_path.parent / path
    return path


def _approval_evidence(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not parsed:
        raise BridgeError("--approval-evidence-json must be a non-empty JSON object")
    decision = str(parsed.get("decision") or "").strip().lower()
    source = parsed.get("source")
    if decision not in APPROVAL_DECISIONS or not isinstance(source, str) or not source.strip():
        raise BridgeError("--approval-evidence-json must include decision GO/approve/approved and a non-empty source")
    return parsed


def bridge_session_intent_to_run_state(
    *,
    intent_root: Path,
    platform: str,
    run_dir: Path,
    approval_evidence: Mapping[str, Any],
    current_work_item_id: str,
    plan_path: str,
    session_id: str | None = None,
    run_id: str | None = None,
    remaining_steps: int = 3,
    allowed_surfaces: list[str] | None = None,
    stop_conditions: list[str] | None = None,
) -> dict[str, Any]:
    state_path = _resolve_state_path(intent_root, platform, session_id)
    intent_state = _read_json_object(state_path)
    events_path = state_path.parent / "intent-events.jsonl"
    events = _read_jsonl_objects(events_path)
    latest_event = SESSION_MATERIAL.compact_event(events[-1] if events else None)
    latest_input_event = SESSION_MATERIAL.latest_event_of(events, "user-input-observed")
    latest_intent_update_event = SESSION_MATERIAL.latest_event_of(events, "intent-updated")

    resolved_session_id = session_id
    if not resolved_session_id:
        state_session_id = intent_state.get("session_id")
        resolved_session_id = state_session_id if isinstance(state_session_id, str) else state_path.parent.name

    session_evidence = {
        "platform": platform,
        "session_id": resolved_session_id,
        "state_path": str(state_path),
        "events_path": str(events_path),
        "event_count": len(events),
        "latest_event": latest_event,
        "latest_input_event": latest_input_event,
        "latest_intent_update_event": latest_intent_update_event,
        "recent_events": SESSION_MATERIAL.safe_recent_events(events),
    }
    merged_approval = dict(approval_evidence)
    merged_approval["session_intent"] = session_evidence

    run_id = run_id or f"session-intent-{platform}-{resolved_session_id}"
    allowed = allowed_surfaces or [plan_path]
    stops = stop_conditions or list(DEFAULT_STOP_CONDITIONS)

    governance_signal = _load_governance_signal_module()
    candidate = governance_signal.conduct_plan_candidate_from_governance(
        intent_state=intent_state,
        current_work_item_id=current_work_item_id,
        plan_path=plan_path,
    )

    run_dir.mkdir(parents=True, exist_ok=True)
    approved_run = SESSION_MATERIAL.build_approved_run(
        intent_state=intent_state,
        approval_evidence=merged_approval,
        run_id=run_id,
        remaining_steps=remaining_steps,
        allowed_surfaces=allowed,
        stop_conditions=stops,
    )
    _write_json_atomic(run_dir / "approved-run.json", approved_run)
    mode = "session-intent-task"
    conduct_plan_path: str | None = None
    conduct_plan_candidate_path: str | None = None
    conduct_plan_candidate_id: str | None = None
    tasks_path: str | None = None
    task_id: str | None = None
    if candidate is not None:
        approved_plan = governance_signal.promote_conduct_plan_candidate(
            candidate,
            approval_evidence=merged_approval,
        )
        if approved_plan is None:
            raise BridgeError("conduct-plan candidate was not promotable with supplied approval evidence")
        _write_json_atomic(run_dir / "conduct-plan.candidate.json", candidate)
        _write_json_atomic(run_dir / "conduct-plan.json", approved_plan)
        mode = "conduct-plan"
        conduct_plan_path = str(run_dir / "conduct-plan.json")
        conduct_plan_candidate_path = str(run_dir / "conduct-plan.candidate.json")
        conduct_plan_candidate_id = candidate.get("candidate_id") if isinstance(candidate.get("candidate_id"), str) else None
    else:
        task = SESSION_MATERIAL.session_intent_task(
            intent_state=intent_state,
            session_id=resolved_session_id,
            allowed_surfaces=allowed,
            source_locator=f"{state_path}#intent-state",
        )
        _write_jsonl_atomic(run_dir / "tasks.jsonl", [task])
        tasks_path = str(run_dir / "tasks.jsonl")
        task_id = task["id"]

    return {
        "mode": mode,
        "run_dir": str(run_dir),
        "state_path": str(state_path),
        "events_path": str(events_path),
        "event_count": len(events),
        "latest_event": latest_event,
        "latest_input_event": latest_input_event,
        "latest_intent_update_event": latest_intent_update_event,
        "approved_run_path": str(run_dir / "approved-run.json"),
        "conduct_plan_path": conduct_plan_path,
        "conduct_plan_candidate_path": conduct_plan_candidate_path,
        "conduct_plan_candidate_id": conduct_plan_candidate_id,
        "tasks_path": tasks_path,
        "task_id": task_id,
        "run_id": run_id,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent-root", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("codex", "claude"))
    parser.add_argument("--session-id")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--current-work-item-id", required=True)
    parser.add_argument("--plan-path", required=True)
    parser.add_argument("--approval-evidence-json", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--remaining-steps", type=int, default=3)
    parser.add_argument("--allowed-surface", action="append", default=[])
    parser.add_argument("--stop-condition", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.remaining_steps < 1:
        parser.error("--remaining-steps must be >= 1")

    try:
        approval = _approval_evidence(args.approval_evidence_json)
        summary = bridge_session_intent_to_run_state(
            intent_root=args.intent_root,
            platform=args.platform,
            session_id=args.session_id,
            run_dir=args.run_dir,
            approval_evidence=approval,
            current_work_item_id=args.current_work_item_id,
            plan_path=args.plan_path,
            run_id=args.run_id,
            remaining_steps=args.remaining_steps,
            allowed_surfaces=args.allowed_surface or [args.plan_path],
            stop_conditions=args.stop_condition or None,
        )
    except (BridgeError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"autopilot-session-bridge: {exc}\n")

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

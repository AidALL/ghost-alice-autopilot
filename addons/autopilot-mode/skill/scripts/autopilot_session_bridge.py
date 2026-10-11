#!/usr/bin/env python3
"""Inspect or admit an explicitly approved queue from exact current session intent.

The current-session assistant requests this CLI through the native host shell
tool with receipt-bound --intent-root, --platform, --session-id and the selected
--run-dir. --check returns latest_input_event and automatic_target without
writing run state. A requested directory does not configure the host Stop target.
Admission additionally requires the approval receipt --input-event-id, task
--current-work-item-id, approved --plan-path and actual --approval-evidence-json;
budget, allowed surfaces and stop conditions retain the user's authorization.
The result reports mode, writes_state, approval_generation and automatic_target.
The bridge validates and records supplied authority; it does not obtain user
approval, execute the task or guarantee that a future host hook selects this run.

Dependencies: Python 3.11+ standard library plus sibling skill scripts.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
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


def _safe_id(value: str) -> str:
    return SESSION_MATERIAL.safe_id(value)


def _approval_evidence(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not parsed:
        raise BridgeError("--approval-evidence-json must be a non-empty JSON object")
    decision = str(parsed.get("decision") or "").strip().lower()
    source = parsed.get("source")
    if decision not in APPROVAL_DECISIONS or not isinstance(source, str) or not source.strip():
        raise BridgeError("--approval-evidence-json must include decision GO/approve/approved and a non-empty source")
    return parsed


def _adapter_modules():
    """Use the hook's own resolver and identity checks, not a second path policy."""
    adapter_dir = Path(__file__).resolve().parents[1] / "adapters"
    sys.path.insert(0, str(adapter_dir))
    import autopilot_mode
    import autopilot_state

    return autopilot_mode, autopilot_state


def _session_material(intent_root: Path, platform: str, session_id: str | None,
                      input_event_id: str | None = None, *, recover_audit: bool = True,
                      source: Mapping[str, str] | None = None) -> dict[str, Any]:
    _, adapter = _adapter_modules()
    if platform not in {"codex", "claude"}:
        raise BridgeError("bridge supports codex and claude")
    return adapter.read_session_material(
        intent_root, platform, session_id, require_input=True,
        expected_input_event_id=input_event_id,
        source=os.environ if source is None else source, recover_audit=recover_audit,
    )


def _automatic_target(run_dir: Path, platform: str, session_id: str,
                      source: Mapping[str, str] | None = None) -> dict[str, Any]:
    mode, adapter = _adapter_modules()
    source = dict(os.environ if source is None else source)
    source["GHOST_ALICE_PLATFORM"] = platform
    source = mode._env_with_hook_cwd({"cwd": str(Path.cwd()), "session_id": session_id}, source)
    selected = adapter.resolve_run_target(source)
    target = selected["run_dir"].expanduser().resolve()
    return {"run_dir": str(target), "reason": selected["reason"],
            "matches_requested_run": target == run_dir.expanduser().resolve(),
            "context_scope": "current process environment and cwd; a future host hook may have different overrides",
            "activation_guaranteed": False}


def inspect_session_target(*, intent_root: Path, platform: str, session_id: str | None,
                           run_dir: Path, input_event_id: str | None = None) -> dict[str, Any]:
    material = _session_material(intent_root, platform, session_id, input_event_id, recover_audit=False)
    return {"mode": "check-only", "writes_state": False,
            "platform": platform, "session_id": material["session_id"],
            "state_path": str(material["state_path"]),
            "run_dir": str(run_dir.expanduser().resolve()),
            "latest_input_event": material["latest_input_event"],
            "automatic_target": _automatic_target(run_dir, platform, material["session_id"])}


def _require_compatible_existing_run(run_dir: Path, platform: str, session_id: str) -> None:
    path = run_dir / "approved-run.json"
    _, adapter = _adapter_modules()
    if not adapter.storage.exists(path):
        return
    existing = adapter.storage.read(path)
    approval = existing.get("approval_evidence")
    binding = approval.get("session_intent") if isinstance(approval, Mapping) else None
    if not isinstance(binding, Mapping) or binding.get("platform") != platform or binding.get("session_id") != session_id:
        raise BridgeError(f"{path}: refusing to replace a run without the same session and platform binding")


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
    input_event_id: str | None = None,
    source: Mapping[str, str] | None = None,
    bind_completion_contract: bool = False,
) -> dict[str, Any]:
    if not input_event_id:
        raise BridgeError("--input-event-id is required; submit the receipt used to approve this work")
    source = dict(os.environ if source is None else source)
    material = _session_material(intent_root, platform, session_id, input_event_id, source=source)
    explicit = {"GHOST_ALICE_SESSION_INTENT_ROOT": str(intent_root.expanduser().resolve()),
                "GHOST_ALICE_PLATFORM": platform, "GHOST_ALICE_SESSION_ID": material["session_id"]}
    for key, value in explicit.items():
        observed = source.get(key)
        if observed and key == "GHOST_ALICE_SESSION_INTENT_ROOT":
            observed = str(Path(observed).expanduser().resolve())
        if observed and observed != value:
            raise BridgeError("bridge coordinate disagrees with current " + key)
        source[key] = value
    if platform == "codex" and source.get("CODEX_THREAD_ID") not in (None, "", material["session_id"]):
        raise BridgeError("bridge session disagrees with current native session")
    from autopilot_intent_recovery import project_session_material, admission_preflight
    material, selection = project_session_material(material, source, run_dir)
    if selection["status"] != "selected":
        if selection["reason"] == "run-owner-mismatch":
            raise BridgeError("refusing to replace a run without the same session, platform and authority binding")
        return dict(selection, writes_state=False, mode="skipped", run_dir=str(run_dir))
    state_path, events_path = material["state_path"], material["events_path"]
    intent_state, events = material["intent_state"], material["events"]
    resolved_session_id = material["session_id"]
    approval_evidence = _approval_evidence(json.dumps(dict(approval_evidence)))
    latest_event = SESSION_MATERIAL.compact_event(events[-1] if events else None)
    latest_input_event = SESSION_MATERIAL.compact_event(material["latest_input_event"])
    latest_intent_update_event = SESSION_MATERIAL.latest_event_of(events, "intent-updated")

    session_evidence = SESSION_MATERIAL.session_evidence(material)
    merged_approval = dict(approval_evidence)
    merged_approval["session_intent"] = session_evidence

    run_id = run_id or f"session-intent-{platform}-{resolved_session_id}"
    allowed = allowed_surfaces or [plan_path]
    stops = stop_conditions or list(DEFAULT_STOP_CONDITIONS)

    _require_compatible_existing_run(run_dir, platform, resolved_session_id)
    automatic_target = _automatic_target(run_dir, platform, resolved_session_id, source)
    approved_run = SESSION_MATERIAL.build_approved_run(
        intent_state=intent_state,
        approval_evidence=merged_approval,
        run_id=run_id,
        remaining_steps=remaining_steps,
        allowed_surfaces=allowed,
        stop_conditions=stops,
    )
    if bind_completion_contract:
        # Opt-in prepared proof binds semantic restrictions that may change
        # without a new input event. Existing run scope hashing already covers
        # this snapshot, so other admission/generation consumers are unchanged.
        approved_run["scope"]["completion_contract"] = {
            key: copy.deepcopy(intent_state.get(key, []))
            for key in ("constraints", "non_goals", "decisions")}
        approved_run["scope"]["publication_mode"] = "session-intent-task"
        approved_run["scope"]["latest_scope"] = copy.deepcopy(intent_state.get("latest_scope", {}))
        approved_run["approval_generation"] = SESSION_MATERIAL.approval_generation(approved_run)
    _, adapter = _adapter_modules()
    with adapter.storage.transaction(run_dir, source, authority=adapter.storage.bound_authority(approved_run),
            preflight=admission_preflight(material, source, run_dir)) as store:
        _require_compatible_existing_run(run_dir, platform, resolved_session_id)
        current = _session_material(intent_root, platform, resolved_session_id, latest_input_event["event_id"], source=source)
        current, selection = project_session_material(current, source, run_dir)
        if selection["status"] != "selected":
            raise BridgeError("current response route changed during admission")
        if current["intent_state"] != intent_state:
            raise BridgeError("intent state changed during admission; inspect current input before retrying")
        existing_path = run_dir / "approved-run.json"
        if store.run() is not None:
            existing = store.run()
            existing_generation = SESSION_MATERIAL.approval_generation(existing)
            if existing_generation == approved_run["approval_generation"]:
                return {"mode": "existing-run", "run_dir": str(run_dir),
                        "approved_run_path": str(existing_path), "writes_state": False,
                        "approval_generation": existing_generation,
                        "latest_input_event": latest_input_event, "automatic_target": automatic_target}
            # Prior generation remains immutable in SQLite; inbox generations are checked.
        adapter.storage.migrate_bound_session(store, source)
        store.put_run(approved_run)
        # Generic run approval covers the admitted business task. A conduct
        # proposal requires its own explicit approval before queue import.
        task = SESSION_MATERIAL.session_intent_task(
            intent_state=intent_state,
            session_id=resolved_session_id,
            allowed_surfaces=allowed,
            source_locator=f"{state_path}#intent-state",
        )
        store.write("tasks.jsonl", [task])
        store.put_run(approved_run)

    return {
        "mode": "session-intent-task",
        "authority_database": str(intent_root.resolve() / "ghost-state.sqlite3"),
        "run_dir": str(run_dir),
        "state_path": str(state_path),
        "events_path": str(events_path),
        "event_count": len(events),
        "latest_event": latest_event,
        "latest_input_event": latest_input_event,
        "latest_intent_update_event": latest_intent_update_event,
        "approved_run_path": str(run_dir / "approved-run.json"),
        "conduct_plan_path": None,
        "conduct_plan_candidate_path": None,
        "conduct_plan_candidate_id": None,
        "tasks_path": str(run_dir / "tasks.jsonl"),
        "task_id": task["id"],
        "run_id": run_id,
        "approval_generation": approved_run["approval_generation"],
        "automatic_target": automatic_target,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent-root", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("codex", "claude"))
    parser.add_argument("--session-id")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="inspect current input and automatic Stop target without writing state")
    parser.add_argument("--input-event-id", help="required for admission: input receipt used when approving this work")
    parser.add_argument("--current-work-item-id")
    parser.add_argument("--plan-path")
    parser.add_argument("--approval-evidence-json")
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
    if not args.check:
        for key in ("current_work_item_id", "plan_path", "approval_evidence_json", "input_event_id"):
            if not getattr(args, key):
                parser.error(f"--{key.replace('_', '-')} is required unless --check is used")

    try:
        if args.check:
            print(json.dumps(inspect_session_target(
                intent_root=args.intent_root, platform=args.platform, session_id=args.session_id,
                run_dir=args.run_dir, input_event_id=args.input_event_id,
            ), ensure_ascii=False, sort_keys=True))
            return 0
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
            input_event_id=args.input_event_id,
        )
    except (ValueError, OSError) as exc:
        parser.exit(1, f"autopilot-session-bridge: {exc}\n")

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

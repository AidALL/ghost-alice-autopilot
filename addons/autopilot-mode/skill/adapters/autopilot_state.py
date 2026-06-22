#!/usr/bin/env python3
"""Durable Stop adapter orchestration for Ghost-ALICE autopilot mode.

Dependencies: Python 3.11+ standard library plus sibling adapter modules.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from autopilot_messages import (
    build_continuation_message,
    build_meta_intervention_message,
    compact_governance_candidate,
)
from autopilot_work_items import (
    APPROVAL_DECISIONS,
    COMPLETION_CHECK_DIGEST_PATTERN,
    AutopilotStateError,
    _find_item,
    _has_explicit_approval_evidence,
    _require_string,
    _validate_string_list,
    apply_conduct_plan_proposals,
    apply_consistency_decision,
    derive_ready_queue,
    materialize_met_criteria_from_continue_next,
    read_work_items,
    rewrite_open_work_items,
    validate_work_items,
    write_work_items,
)


APPROVED_RUN_FILE = "approved-run.json"
TASKS_FILE = "tasks.jsonl"
CONDUCT_PLAN_FILE = "conduct-plan.json"
APPLIED_CONDUCT_PLAN_FILE = "conduct-plan.applied.json"
DECISION_FILE = "consistency-decision.json"
APPLIED_DECISION_FILE = "consistency-decision.applied.json"
EVENTS_FILE = "events.jsonl"
OFF_FILE = "OFF"
LOCK_DIR = ".advance.lock"
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.02
NOOP_PAYLOAD = {"continue": True, "systemMessage": ""}
CONSISTENCY_DECISION_SCHEMA = "autopilot-consistency-decision.v1"
CONSISTENCY_DECISION_CANDIDATE_SCHEMA = "autopilot-consistency-decision-candidate.v1"
OBSERVATION_SIGNAL_SCHEMA = "autopilot-observation-signal.v1"
AUTO_APPROVAL_ENV = "GHOST_ALICE_AUTOPILOT_APPROVAL_EVIDENCE_JSON"
IO_TRACE_FILE_ENV = "GHOST_ALICE_IO_TRACE_FILE"
AUTOPILOT_APPROVAL_DECISION_IDS = frozenset({"autopilot-run-approval", "autopilot-approval"})
PROMOTION_DECISIONS = frozenset({"go", "approve", "approved", "promote", "promoted"})


def _load_session_material_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "autopilot_session_material.py"
    spec = importlib.util.spec_from_file_location("autopilot_session_material", path)
    if spec is None or spec.loader is None:
        raise AutopilotStateError(f"cannot load session material module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SESSION_MATERIAL = _load_session_material_module()
DEFAULT_STOP_CONDITIONS = SESSION_MATERIAL.DEFAULT_STOP_CONDITIONS


def _noop_payload() -> dict[str, Any]:
    return dict(NOOP_PAYLOAD)


@contextmanager
def _run_dir_lock(run_dir: Path):
    lock_dir = run_dir / LOCK_DIR
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise AutopilotStateError(f"timed out waiting for autopilot run lock {lock_dir}") from exc
            time.sleep(LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        try:
            lock_dir.rmdir()
        except FileNotFoundError:
            pass


def _has_non_empty_scope(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, dict) and bool(value)


def _has_non_empty_approval_evidence(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, dict) and bool(value)


def _has_valid_promotion_evidence(value: Any) -> bool:
    if not isinstance(value, Mapping) or not value:
        return False
    decision = str(value.get("decision") or "").strip().lower()
    source = value.get("source")
    return decision in PROMOTION_DECISIONS and isinstance(source, str) and bool(source.strip())


def _is_non_empty_string_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item for item in value)


def _surface_matches(allowed: str, candidate: str) -> bool:
    if allowed == candidate:
        return True
    if allowed.endswith("/..."):
        prefix = allowed.removesuffix("/...").rstrip("/")
        return candidate == prefix or candidate.startswith(f"{prefix}/")
    return False


def _work_item_within_run_surfaces(run: dict[str, Any], item: dict[str, Any]) -> bool:
    run_surfaces = run["allowed_surfaces"]
    item_surfaces = item["allowed_surface"]
    if not item_surfaces:
        return False
    return all(any(_surface_matches(allowed, surface) for allowed in run_surfaces) for surface in item_surfaces)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AutopilotStateError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AutopilotStateError(f"{path}: expected JSON object")
    return value


def _try_read_json_object(path: Path) -> dict[str, Any]:
    try:
        return _read_json_object(path)
    except (OSError, json.JSONDecodeError, AutopilotStateError):
        return {}


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    values: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AutopilotStateError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if isinstance(value, dict):
            values.append(value)
    return values


def _write_json_atomic(path: str | Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2, sort_keys=True)
            out.write("\n")
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _safe_id(value: str) -> str:
    return SESSION_MATERIAL.safe_id(value)


def _compact_event(event: Mapping[str, Any] | None) -> dict[str, Any]:
    return SESSION_MATERIAL.compact_event(event)


def _latest_event_of(events: list[dict[str, Any]], event_name: str) -> dict[str, Any]:
    return SESSION_MATERIAL.latest_event_of(events, event_name)


def _safe_recent_events(events: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    return SESSION_MATERIAL.safe_recent_events(events, limit=limit)


def _io_trace_path(source: Mapping[str, str]) -> Path:
    configured = str(source.get(IO_TRACE_FILE_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    home_text = str(source.get("HOME") or "").strip()
    if not home_text:
        return Path("__ghost_alice_home_unavailable__") / ".ghost-alice" / "io-trace.jsonl"
    home = Path(home_text).expanduser()
    return home / ".ghost-alice" / "io-trace.jsonl"


def _read_io_trace_rows(
    source: Mapping[str, str],
    *,
    session_id: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    path = _io_trace_path(source)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    selected: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if session_id and row.get("session") != session_id:
            continue
        compact = {
            key: row[key]
            for key in ("ts", "session", "tool", "path", "pattern")
            if isinstance(row.get(key), str)
        }
        if compact:
            selected.append(compact)
        if len(selected) >= limit:
            break
    return list(reversed(selected))


def _io_trace_observation_signal(
    *,
    work_item_id: str,
    focus_layer: str,
    run_id: str,
    session_id: str | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not rows:
        return None
    last = rows[-1]
    signal_id_parts = ["iotrace", run_id, work_item_id]
    if session_id:
        signal_id_parts.append(session_id)
    return {
        "schema_version": OBSERVATION_SIGNAL_SCHEMA,
        "source": "autopilot_state.io_trace",
        "signal_id": _safe_id("-".join(signal_id_parts)),
        "runtime": "stop-adapter",
        "scenario_id": "stop-iotrace-continuation",
        "classification": "semantic-observation",
        "inference_status": "ok",
        "semantic_status": "parsed",
        "hook_status": "io-trace-present",
        "agent_activity": str(last.get("tool") or "tool-using"),
        "mismatch_detected": True,
        "focus_layer": focus_layer or "macro",
        "verdict": "reopen_focus",
        "next_action": "continue from latest io-trace",
        "loop_guard": "do not stop while io-trace material remains unresolved",
    }


def _governance_candidate_from_iotrace(
    *,
    work_item_id: str,
    focus_layer: str,
    run_id: str,
    session_id: str | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    governance_signal = _load_governance_signal_module()
    if governance_signal is None:
        return None
    observation_signal = _io_trace_observation_signal(
        work_item_id=work_item_id,
        focus_layer=focus_layer,
        run_id=run_id,
        session_id=session_id,
        rows=rows,
    )
    if observation_signal is None:
        return None
    return governance_signal.decision_candidate_from_governance(
        work_item_id=work_item_id,
        governance_signal=observation_signal,
    )


def _valid_approval_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    decision = str(value.get("decision") or "").strip().lower()
    source = value.get("source")
    if decision not in APPROVAL_DECISIONS or not isinstance(source, str) or not source.strip():
        return None
    return dict(value)


def _approval_from_env(source: Mapping[str, str]) -> dict[str, Any] | None:
    raw = str(source.get(AUTO_APPROVAL_ENV) or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _valid_approval_evidence(parsed)


def _approval_from_session_decisions(intent_state: Mapping[str, Any]) -> dict[str, Any] | None:
    decisions = intent_state.get("decisions")
    if not isinstance(decisions, list):
        return None
    for raw in decisions:
        if not isinstance(raw, Mapping) or raw.get("superseded") is True:
            continue
        decision_id = str(raw.get("id") or "").strip()
        kind = str(raw.get("kind") or raw.get("type") or "").strip()
        if decision_id not in AUTOPILOT_APPROVAL_DECISION_IDS and kind != "autopilot_run_approval":
            continue
        evidence_source = raw.get("approval_evidence") if isinstance(raw.get("approval_evidence"), Mapping) else raw
        evidence = _valid_approval_evidence(evidence_source)
        if evidence is None:
            continue
        evidence.setdefault("decision_id", decision_id)
        evidence.setdefault("decision_summary", raw.get("summary", ""))
        return evidence
    return None


def _unmet_admitted_criteria_evidence(intent_state: Mapping[str, Any]) -> dict[str, Any] | None:
    criteria = intent_state.get("acceptance_criteria")
    if not isinstance(criteria, list):
        return None
    open_ids = [
        str(criterion.get("id")).strip()
        for criterion in criteria
        if isinstance(criterion, Mapping)
        and criterion.get("admitted") is True
        and str(criterion.get("status") or "unmet").lower() != "met"
        and str(criterion.get("id") or "").strip()
    ]
    if not open_ids:
        return None
    return {
        "decision": "AUTO",
        "source": "admitted-unmet-criterion",
        "reason": "session intent has admitted, not-yet-met acceptance criteria",
        "open_criteria": open_ids,
    }


def _project_cwd_from_env(source: Mapping[str, str]) -> Path:
    explicit = str(source.get("GHOST_ALICE_AUTOPILOT_CWD") or source.get("PWD") or "").strip()
    return Path(explicit).expanduser() if explicit else Path.cwd()


def _session_intent_root_candidates(source: Mapping[str, str], project_cwd: Path) -> list[Path]:
    candidates: list[Path] = []
    configured = str(source.get("GHOST_ALICE_SESSION_INTENT_ROOT") or "").strip()
    if configured:
        candidates = [Path(configured).expanduser()]
    else:
        candidates.extend([
            project_cwd / ".tmp" / "session-intent",
            project_cwd.parent / "ghost-alice" / ".tmp" / "session-intent",
        ])
        home_text = str(source.get("HOME") or "").strip()
        if home_text:
            home = Path(home_text).expanduser()
            candidates.extend([
                home / "ghost-alice" / ".tmp" / "session-intent",
                home / ".ghost-alice" / "session-intent",
            ])
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _platform_candidates(source: Mapping[str, str]) -> list[str]:
    platform = str(source.get("GHOST_ALICE_PLATFORM") or "").strip().lower()
    if platform in {"codex", "claude"}:
        return [platform, "claude" if platform == "codex" else "codex"]
    return ["codex", "claude"]


def _iter_current_session_intents(
    source: Mapping[str, str],
    project_cwd: Path,
):
    explicit_session = str(source.get("GHOST_ALICE_SESSION_ID") or "").strip()
    for root in _session_intent_root_candidates(source, project_cwd):
        for platform in _platform_candidates(source):
            if explicit_session:
                state_path = root / platform / _safe_id(explicit_session) / "intent-state.json"
                if state_path.is_file():
                    intent_state = _try_read_json_object(state_path)
                    yield {
                        "platform": platform,
                        "session_id": explicit_session,
                        "state_path": state_path,
                        "events_path": state_path.parent / EVENTS_FILE.replace("events", "intent-events"),
                        "intent_state": intent_state,
                    }
            pointer_path = root / platform / "current-session.json"
            pointer = _try_read_json_object(pointer_path)
            if pointer.get("schema_version") != "session-intent-current.v1":
                continue
            pointer_state = pointer.get("state_path")
            pointer_session = pointer.get("session_id")
            if isinstance(pointer_state, str) and pointer_state.strip():
                state_path = Path(pointer_state)
                if not state_path.is_absolute():
                    state_path = pointer_path.parent / state_path
            elif isinstance(pointer_session, str) and pointer_session.strip():
                state_path = root / platform / _safe_id(pointer_session) / "intent-state.json"
            else:
                continue
            if not state_path.is_file():
                continue
            intent_state = _try_read_json_object(state_path)
            session_id = str(intent_state.get("session_id") or pointer_session or state_path.parent.name)
            yield {
                "platform": platform,
                "session_id": session_id,
                "state_path": state_path,
                "events_path": state_path.parent / "intent-events.jsonl",
                "intent_state": intent_state,
            }


def _load_governance_signal_module():
    return SESSION_MATERIAL.load_governance_signal_module(required=False)


def _run_state_available(run_dir: Path) -> bool:
    return (run_dir / APPROVED_RUN_FILE).is_file() and (
        (run_dir / TASKS_FILE).is_file() or (run_dir / CONDUCT_PLAN_FILE).is_file()
    )


def _bootstrap_from_session_intent_if_approved(
    run_dir: Path,
    source: Mapping[str, str],
    project_cwd: Path,
) -> bool:
    if (run_dir / OFF_FILE).exists() or _run_state_available(run_dir):
        return False
    resolved = None
    approval = None
    for candidate in _iter_current_session_intents(source, project_cwd):
        intent_state = candidate["intent_state"]
        if not isinstance(intent_state, Mapping):
            continue
        candidate_approval = (
            _approval_from_env(source)
            or _approval_from_session_decisions(intent_state)
            or _unmet_admitted_criteria_evidence(intent_state)
        )
        if candidate_approval is None:
            continue
        resolved = candidate
        approval = candidate_approval
        break
    if resolved is None:
        return False
    intent_state = resolved["intent_state"]
    if not isinstance(intent_state, Mapping):
        return False
    if approval is None:
        return False

    events_path = resolved["events_path"]
    events = _read_jsonl_objects(events_path)
    session_evidence = {
        "platform": resolved["platform"],
        "session_id": resolved["session_id"],
        "state_path": str(resolved["state_path"]),
        "events_path": str(events_path),
        "event_count": len(events),
        "latest_event": _compact_event(events[-1] if events else None),
        "latest_input_event": _latest_event_of(events, "user-input-observed"),
        "latest_intent_update_event": _latest_event_of(events, "intent-updated"),
        "recent_events": _safe_recent_events(events),
    }
    io_trace_rows = _read_io_trace_rows(source, session_id=str(resolved["session_id"]), limit=8)
    if io_trace_rows:
        session_evidence["io_trace"] = io_trace_rows
    merged_approval = dict(approval)
    merged_approval["session_intent"] = session_evidence

    plan_path = str(
        source.get("GHOST_ALICE_AUTOPILOT_PLAN_PATH")
        or project_cwd / ".tmp" / "implementation-plans" / "autopilot-session-intent.md"
    )
    allowed_surfaces = [plan_path]
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(
        run_dir / APPROVED_RUN_FILE,
        {
            "schema_version": "autopilot-run.v1",
            "run_id": f"session-intent-{resolved['platform']}-{_safe_id(str(resolved['session_id']))}",
            "approved": True,
            "status": "running",
            "scope": {"summary": SESSION_MATERIAL.run_summary(intent_state)},
            "budget": {"remaining_steps": 3},
            "allowed_surfaces": allowed_surfaces,
            "stop_conditions": list(DEFAULT_STOP_CONDITIONS),
            "approval_evidence": merged_approval,
        },
    )
    _append_event(
        run_dir,
        {
            "schema_version": "autopilot-event.v1",
            "event": "session_intent_bootstrapped",
            "platform": resolved["platform"],
            "session_id": resolved["session_id"],
            "state_path": str(resolved["state_path"]),
            "approval_source": merged_approval.get("source"),
        },
    )

    governance_signal = _load_governance_signal_module()
    candidate = None
    if governance_signal is not None:
        candidate = governance_signal.conduct_plan_candidate_from_governance(
            intent_state=intent_state,
            current_work_item_id=str(source.get("GHOST_ALICE_AUTOPILOT_CURRENT_WORK_ITEM_ID") or "current"),
            plan_path=plan_path,
        )
    if candidate is not None and governance_signal is not None:
        approved_plan = governance_signal.promote_conduct_plan_candidate(
            candidate,
            approval_evidence=merged_approval,
        )
        if approved_plan is not None:
            _write_json_atomic(run_dir / "conduct-plan.candidate.json", candidate)
            _write_json_atomic(run_dir / CONDUCT_PLAN_FILE, approved_plan)
            return True

    write_work_items(
        run_dir / TASKS_FILE,
        [
            SESSION_MATERIAL.session_intent_task(
                intent_state=intent_state,
                session_id=str(resolved["session_id"]),
                allowed_surfaces=allowed_surfaces,
                source_locator=f"{resolved['state_path']}#intent-state",
            )
        ],
    )
    return True


def _approved_run_allows_continue(run: dict[str, Any]) -> bool:
    if run.get("schema_version") != "autopilot-run.v1":
        return False
    if run.get("approved") is not True:
        return False
    if run.get("status") != "running":
        return False
    if not _has_non_empty_scope(run.get("scope")):
        return False
    budget = run.get("budget")
    if not isinstance(budget, dict):
        return False
    remaining_steps = budget.get("remaining_steps")
    if not isinstance(remaining_steps, int) or isinstance(remaining_steps, bool) or remaining_steps <= 0:
        return False
    if not _is_non_empty_string_array(run.get("allowed_surfaces")):
        return False
    if not _is_non_empty_string_array(run.get("stop_conditions")):
        return False
    if not _has_explicit_approval_evidence(run.get("approval_evidence")):
        return False
    return True


def _append_event(run_dir: Path, event: dict[str, Any]) -> None:
    event_path = run_dir / EVENTS_FILE
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")


def _validate_promoted_decision_file(decision: Mapping[str, Any]) -> None:
    schema = decision.get("schema_version")
    if schema == CONSISTENCY_DECISION_CANDIDATE_SCHEMA or decision.get("promotion_state") == "candidate":
        raise AutopilotStateError("consistency decision candidate is not adapter-consumable")
    if schema != CONSISTENCY_DECISION_SCHEMA:
        raise AutopilotStateError(f"consistency decision schema_version must be {CONSISTENCY_DECISION_SCHEMA!r}")
    if decision.get("promotion_state") != "promoted":
        raise AutopilotStateError("consistency decision promotion_state must be 'promoted'")
    if not _has_valid_promotion_evidence(decision.get("promotion_evidence")):
        raise AutopilotStateError(
            "consistency decision promotion_evidence must include a valid promotion decision and source"
        )
    _require_string(decision.get("decision_id"), "consistency decision decision_id")
    _require_string(decision.get("candidate_id"), "consistency decision candidate_id")
    digest = _require_string(decision.get("governance_signal_digest"), "consistency decision governance_signal_digest")
    if not COMPLETION_CHECK_DIGEST_PATTERN.fullmatch(digest):
        raise AutopilotStateError("consistency decision governance_signal_digest must be a sha256 digest")
    _require_string(decision.get("decision_key"), "consistency decision decision_key")
    _require_string(decision.get("state_hash"), "consistency decision state_hash")
    _require_string(decision.get("loop_key"), "consistency decision loop_key")


def _apply_pending_decision(
    run_dir: Path,
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    decision_path = run_dir / DECISION_FILE
    if not decision_path.is_file():
        return items, None
    decision = _read_json_object(decision_path)
    _validate_promoted_decision_file(decision)
    item_id = _require_string(decision.get("work_item_id"), "consistency decision work_item_id")
    decision_value = _require_string(decision.get("decision"), "consistency decision decision")
    evidence = decision.get("evidence")
    evidence = _validate_string_list(evidence, "consistency decision evidence")
    updated = apply_consistency_decision(
        items,
        item_id,
        decision_value,
        completion_check_digest=decision.get("completion_check_digest"),
        verdict=decision.get("verdict"),
        evidence=evidence,
    )
    write_work_items(run_dir / TASKS_FILE, updated)
    os.replace(decision_path, run_dir / APPLIED_DECISION_FILE)
    _append_event(
        run_dir,
        {
            "schema_version": "autopilot-event.v1",
            "event": "consistency_decision_applied",
            "decision": decision_value,
            "decision_id": decision.get("decision_id"),
            "work_item_id": item_id,
            "candidate_id": decision.get("candidate_id"),
            "governance_signal_digest": decision.get("governance_signal_digest"),
            "decision_key": decision.get("decision_key"),
            "state_hash": decision.get("state_hash"),
            "loop_key": decision.get("loop_key"),
        },
    )
    return updated, {
        "decision": decision_value,
        "work_item_id": item_id,
        "evidence": evidence,
        "decision_id": decision.get("decision_id"),
        "completion_check_digest": decision.get("completion_check_digest"),
    }


def _apply_pending_conduct_plan(run_dir: Path, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plan_path = run_dir / CONDUCT_PLAN_FILE
    if not plan_path.is_file():
        return items
    current = validate_work_items(copy.deepcopy(items))
    before_ids = {item["id"] for item in current}
    plan = _read_json_object(plan_path)
    updated = apply_conduct_plan_proposals(current, plan)
    imported_ids = [item["id"] for item in updated if item["id"] not in before_ids]
    if imported_ids:
        write_work_items(run_dir / TASKS_FILE, updated)
    os.replace(plan_path, run_dir / APPLIED_CONDUCT_PLAN_FILE)
    _append_event(
        run_dir,
        {
            "schema_version": "autopilot-event.v1",
            "event": "conduct_plan_imported",
            "imported_work_item_ids": imported_ids,
            "source": plan.get("source"),
        },
    )
    return updated


def _missing_decision_resume_count(run_dir: Path, work_item_id: str) -> int:
    return sum(
        1
        for event in _read_jsonl_objects(run_dir / EVENTS_FILE)
        if event.get("event") == "resume_running_item_without_decision"
        and event.get("work_item_id") == work_item_id
    )


def _session_id_from_run(run: Mapping[str, Any], source: Mapping[str, str] | None) -> str | None:
    if source is not None:
        explicit = str(source.get("GHOST_ALICE_SESSION_ID") or "").strip()
        if explicit:
            return explicit
    approval = run.get("approval_evidence")
    if isinstance(approval, Mapping):
        session_intent = approval.get("session_intent")
        if isinstance(session_intent, Mapping):
            session_id = session_intent.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()
    return None


def _io_trace_rows_for_run(run: Mapping[str, Any], source: Mapping[str, str] | None) -> list[dict[str, Any]]:
    if source is None:
        return []
    return _read_io_trace_rows(source, session_id=_session_id_from_run(run, source), limit=8)


def _io_trace_candidate_for_item(
    run: Mapping[str, Any],
    item: Mapping[str, Any],
    source: Mapping[str, str] | None,
    io_trace_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not io_trace_rows:
        return None
    work_item_id = str(item.get("id") or "current")
    focus_layer = str(item.get("focus_layer") or "macro")
    run_id = str(run.get("run_id") or "unknown")
    return _governance_candidate_from_iotrace(
        work_item_id=work_item_id,
        focus_layer=focus_layer,
        run_id=run_id,
        session_id=_session_id_from_run(run, source),
        rows=io_trace_rows,
    )


def _select_ready_item(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    updated = validate_work_items(copy.deepcopy(items))
    item = _find_item(updated, item_id)
    item["status"] = "running"
    return {"item": item, "items": updated}


def _append_iotrace_resume_event(
    root: Path,
    run: Mapping[str, Any],
    item: Mapping[str, Any],
    governance_candidate: Mapping[str, Any] | None,
) -> None:
    compact_candidate = compact_governance_candidate(governance_candidate)
    event = {
        "schema_version": "autopilot-event.v1",
        "event": "resume_running_item_from_iotrace",
        "run_id": run.get("run_id"),
        "work_item_id": item.get("id"),
    }
    if compact_candidate:
        event.update(_governance_candidate_event_fields(compact_candidate))
    _append_event(root, event)


def _governance_candidate_event_fields(governance_candidate: Mapping[str, Any] | None) -> dict[str, Any]:
    compact_candidate = compact_governance_candidate(governance_candidate)
    if not compact_candidate:
        return {}
    fields = {
        "governance_candidate_id": compact_candidate.get("candidate_id"),
        "governance_candidate_source": compact_candidate.get("source"),
        "governance_candidate_decision": compact_candidate.get("decision"),
        "governance_source_signal_id": compact_candidate.get("source_signal_id"),
        "governance_candidate_evidence": compact_candidate.get("evidence"),
    }
    return {key: value for key, value in fields.items() if value not in (None, "", [])}


def _advance_approved_run_locked(root: Path, source: Mapping[str, str] | None = None) -> dict[str, Any]:
    root = Path(root)
    approved_run_path = root / APPROVED_RUN_FILE
    tasks_path = root / TASKS_FILE
    if (root / OFF_FILE).exists():
        return _noop_payload()
    conduct_plan_path = root / CONDUCT_PLAN_FILE
    if not approved_run_path.is_file() or (not tasks_path.is_file() and not conduct_plan_path.is_file()):
        return _noop_payload()

    run = _read_json_object(approved_run_path)
    if not _approved_run_allows_continue(run):
        return _noop_payload()
    io_trace_rows = _io_trace_rows_for_run(run, source)

    items = read_work_items(tasks_path) if tasks_path.is_file() else []
    items, applied_decision = _apply_pending_decision(root, items)
    if applied_decision is not None and applied_decision.get("decision") == "continue_next":
        materialize_met_criteria_from_continue_next(run, applied_decision, source)
    if applied_decision is not None and applied_decision["decision"] == "ask_user_meta":
        return {
            "continue": True,
            "systemMessage": build_meta_intervention_message(
                run,
                work_item_id=applied_decision["work_item_id"],
                evidence=applied_decision["evidence"],
            ),
        }
    items = _apply_pending_conduct_plan(root, items)
    ready_queue = derive_ready_queue(items)
    if not ready_queue:
        running_items = [item for item in items if item["status"] == "running"]
        if running_items:
            running_item = running_items[0]
            if _work_item_within_run_surfaces(run, running_item):
                governance_candidate = _io_trace_candidate_for_item(run, running_item, source, io_trace_rows)
                if _missing_decision_resume_count(root, running_item["id"]) >= 1:
                    if io_trace_rows:
                        _append_iotrace_resume_event(root, run, running_item, governance_candidate)
                        return {
                            "continue": True,
                            "systemMessage": build_continuation_message(
                                run,
                                running_item,
                                pending_decision=True,
                                io_trace_rows=io_trace_rows,
                                governance_candidate=governance_candidate,
                            ),
                        }
                    evidence = ["loop-guard: repeated missing decision"]
                    updated = apply_consistency_decision(
                        items,
                        running_item["id"],
                        "ask_user_meta",
                        evidence=evidence,
                    )
                    write_work_items(tasks_path, updated)
                    _append_event(
                        root,
                        {
                            "schema_version": "autopilot-event.v1",
                            "event": "missing_decision_escalated",
                            "run_id": run.get("run_id"),
                            "work_item_id": running_item["id"],
                        },
                    )
                    return {
                        "continue": True,
                        "systemMessage": build_meta_intervention_message(
                            run,
                            work_item_id=running_item["id"],
                            evidence=evidence,
                            pending_decision_state="repeated-missing-decision",
                        ),
                    }
                _append_event(
                    root,
                    {
                        "schema_version": "autopilot-event.v1",
                        "event": "resume_running_item_without_decision",
                        "run_id": run.get("run_id"),
                        "work_item_id": running_item["id"],
                    },
                )
                return {
                    "continue": True,
                    "systemMessage": build_continuation_message(
                        run,
                        running_item,
                        pending_decision=True,
                        io_trace_rows=io_trace_rows,
                        governance_candidate=governance_candidate,
                    ),
                }
        _append_event(
            root,
            {
                "schema_version": "autopilot-event.v1",
                "event": "no_ready_item",
                "run_id": run.get("run_id"),
            },
        )
        return _noop_payload()

    next_item = _find_item(items, ready_queue[0])
    if not _work_item_within_run_surfaces(run, next_item):
        _append_event(
            root,
            {
                "schema_version": "autopilot-event.v1",
                "event": "ready_item_outside_allowed_surfaces",
                "run_id": run.get("run_id"),
                "work_item_id": next_item["id"],
            },
        )
        return _noop_payload()

    selected = _select_ready_item(items, next_item["id"])
    selected_item = selected["item"]
    write_work_items(tasks_path, selected["items"])
    governance_candidate = _io_trace_candidate_for_item(run, selected_item, source, io_trace_rows)
    event = {
        "schema_version": "autopilot-event.v1",
        "event": "continue_next_item",
        "run_id": run.get("run_id"),
        "work_item_id": selected_item["id"],
        "focus_layer": selected_item["focus_layer"],
    }
    event.update(_governance_candidate_event_fields(governance_candidate))
    _append_event(root, event)
    return {
        "continue": True,
        "systemMessage": build_continuation_message(
            run,
            selected_item,
            io_trace_rows=io_trace_rows,
            governance_candidate=governance_candidate,
        ),
    }


def advance_approved_run(run_dir: str | Path, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    root = Path(run_dir)
    approved_run_path = root / APPROVED_RUN_FILE
    tasks_path = root / TASKS_FILE
    conduct_plan_path = root / CONDUCT_PLAN_FILE
    if (root / OFF_FILE).exists():
        return _noop_payload()
    if not approved_run_path.is_file() or (not tasks_path.is_file() and not conduct_plan_path.is_file()):
        return _noop_payload()
    with _run_dir_lock(root):
        return _advance_approved_run_locked(root, env)


def _bootstrap_then_advance(
    root: Path,
    source: Mapping[str, str],
    project_cwd: Path,
) -> dict[str, Any]:
    if not _run_state_available(root):
        _bootstrap_from_session_intent_if_approved(root, source, project_cwd)
    return advance_approved_run(root, source)


def adapter_payload_from_env(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    if env is not None and not source:
        return _noop_payload()
    prefer_process_cwd = env is None
    project_cwd = _project_cwd_from_env(source)
    run_dir = source.get("GHOST_ALICE_AUTOPILOT_RUN_DIR")
    if run_dir:
        return _bootstrap_then_advance(Path(run_dir).expanduser(), source, project_cwd)
    explicit_cwd = source.get("GHOST_ALICE_AUTOPILOT_CWD")
    if explicit_cwd:
        explicit_project = Path(explicit_cwd).expanduser()
        return _bootstrap_then_advance(explicit_project / ".autopilot", source, explicit_project)
    cwd_run_dir = Path.cwd() / ".autopilot"
    pwd = source.get("PWD")
    if pwd:
        if prefer_process_cwd and _run_state_available(cwd_run_dir):
            return advance_approved_run(cwd_run_dir, source)
        pwd_project = Path(pwd).expanduser()
        return _bootstrap_then_advance(pwd_project / ".autopilot", source, pwd_project)
    if _run_state_available(cwd_run_dir):
        return advance_approved_run(cwd_run_dir, source)
    return _bootstrap_then_advance(cwd_run_dir, source, project_cwd)

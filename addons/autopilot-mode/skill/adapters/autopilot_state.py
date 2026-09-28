#!/usr/bin/env python3
"""Durable Stop adapter orchestration for Ghost-ALICE autopilot mode.

Dependencies: Python 3.11+ standard library plus sibling adapter modules.
"""

from __future__ import annotations

import copy
import hashlib
import autopilot_storage as storage
from autopilot_provenance import bind_origin, reapproval_payload, default_intent_root
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
    build_semantic_delta_starvation_message,
    compact_governance_candidate,
)
from autopilot_intent_recovery import (
    open_conduct_feedback_evidence,
    semantic_delta_starvation_event,
    unmet_admitted_criteria_evidence,
)
from autopilot_runtime_context import (
    project_cwd_from_env as _project_cwd_from_env,
    read_session_material,
    resolve_run_target,
    run_state_available as _run_state_available,
)
from autopilot_lineage import (
    agent_runtime_context_is_explicit,
    continuation_context_error,
    current_session_context,
    session_binding_mismatch_event,
    stale_continuation_event,
    stale_continuation_missing_intent_event,
    stale_continuation_source_intent_event,
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
REJECTED_DECISION_FILE = "consistency-decision.rejected.json"
EVENTS_FILE = "events.jsonl"
OFF_FILE = "OFF"
LOCK_DIR = ".advance.lock"
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.02
# Ceiling on io-trace-backed resumes of a decision-less running item; past it, escalate to ask_user_meta even while io-trace remains (else it re-fires forever). Platform-neutral.
IOTRACE_RESUME_LIMIT_DEFAULT = 3
IOTRACE_RESUME_LIMIT_ENV = "GHOST_ALICE_AUTOPILOT_IOTRACE_RESUME_LIMIT"
NOOP_PAYLOAD = {"continue": True, "systemMessage": ""}
CONSISTENCY_DECISION_SCHEMA = "autopilot-consistency-decision.v1"
CONSISTENCY_DECISION_CANDIDATE_SCHEMA = "autopilot-consistency-decision-candidate.v1"
OBSERVATION_SIGNAL_SCHEMA = "autopilot-observation-signal.v1"
AUTO_APPROVAL_ENV = "GHOST_ALICE_AUTOPILOT_APPROVAL_EVIDENCE_JSON"
IO_TRACE_FILE_ENV = "GHOST_ALICE_IO_TRACE_FILE"
AUTOPILOT_APPROVAL_DECISION_IDS = frozenset({"autopilot-run-approval", "autopilot-approval"})
PROMOTION_DECISIONS = frozenset({"go", "approve", "approved", "promote", "promoted", "direct"})


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
        value = storage.read(path) if path.name not in storage.INBOXES else json.loads(path.read_text(encoding="utf-8"))
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
    if path.name == EVENTS_FILE and (storage.active(path.parent) or (path.parent / storage.AUTHORITY_FILE).exists()):
        try:
            return storage.read(path)
        except FileNotFoundError:
            return []
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
    if storage.write(target, dict(value)):
        return
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
            for key in ("ts", "session", "tool", "path", "pattern", "op")
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
    if not configured and (core_default := default_intent_root(source, project_cwd)) is not None:
        candidates.append(core_default)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen and candidate.is_dir():
            seen.add(key)
            unique.append(candidate)
    return unique

def _platform_candidates(source: Mapping[str, str]) -> list[str]:
    platform = str(source.get("GHOST_ALICE_PLATFORM") or "").strip().lower()
    if platform:
        return [platform] if platform in {"codex", "claude", "agent-runtime"} else []
    return ["codex", "claude"]


def _iter_current_session_intents(
    source: Mapping[str, str],
    project_cwd: Path,
    *, hook_input: Mapping[str, Any] | None = None,
):
    if str(source.get("GHOST_ALICE_PLATFORM") or "").strip().lower() == "agent-runtime":
        if not agent_runtime_context_is_explicit(source):
            return
    for root in _session_intent_root_candidates(source, project_cwd):
        for platform in _platform_candidates(source):
            context = current_session_context(source, run_platform=platform, hook_input=hook_input)
            explicit_session = str(context.get("GHOST_ALICE_SESSION_ID") or "").strip()
            try:
                yield read_session_material(root, platform, explicit_session or None, source=source)
            except FileNotFoundError:
                continue
            except (ValueError, OSError):
                return


def _load_governance_signal_module():
    return SESSION_MATERIAL.load_governance_signal_module(required=False)


def _has_explicit_session_intent_context(source: Mapping[str, str], project_cwd: Path) -> bool:
    if str(source.get("GHOST_ALICE_SESSION_ID") or "").strip():
        return True
    if str(source.get("GHOST_ALICE_SESSION_INTENT_ROOT") or "").strip():
        return True
    return (project_cwd / ".tmp" / "session-intent").is_dir()


def _bootstrap_from_session_intent_if_approved(
    run_dir: Path,
    source: Mapping[str, str],
    project_cwd: Path,
    *, hook_input: Mapping[str, Any] | None = None, permission_denied_noop: bool = False,
    expected_input_event: Mapping[str, Any] | None = None,
) -> bool:
    if (run_dir / OFF_FILE).exists() or _run_state_available(run_dir):
        return False
    if not _has_explicit_session_intent_context(source, project_cwd):
        return False
    resolved = None
    approval = None
    for candidate in _iter_current_session_intents(source, project_cwd, hook_input=hook_input):
        intent_state = candidate["intent_state"]
        if not isinstance(intent_state, Mapping):
            continue
        candidate_approval = (
            _approval_from_env(source)
            or _approval_from_session_decisions(intent_state)
            or unmet_admitted_criteria_evidence(intent_state)
            or open_conduct_feedback_evidence(intent_state)
        )
        if candidate_approval is None:
            continue
        resolved = candidate
        approval = candidate_approval
        break
    if resolved is None:
        return False
    if expected_input_event is not None and resolved.get("latest_input_event") != expected_input_event:
        raise AutopilotStateError("input changed before completion admission; original caller receipt is stale")
    intent_state = resolved["intent_state"]
    if not isinstance(intent_state, Mapping):
        return False
    if approval is None:
        return False

    events_path = resolved["events_path"]
    events = resolved["events"]
    session_evidence = SESSION_MATERIAL.session_evidence(resolved)
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
    intent_events_path = resolved.get("events_path")
    initial_watermark = _intent_events_count(Path(intent_events_path)) if intent_events_path else 0
    run_dir.mkdir(parents=True, exist_ok=True)
    approved_run = SESSION_MATERIAL.build_approved_run(
        intent_state=intent_state, approval_evidence=merged_approval,
        run_id=f"session-intent-{resolved['platform']}-{_safe_id(str(resolved['session_id']))}",
        remaining_steps=3, allowed_surfaces=allowed_surfaces,
        stop_conditions=list(DEFAULT_STOP_CONDITIONS),
    )
    approved_run["scope"]["completion_contract"] = {key: copy.deepcopy(intent_state.get(key, [])) for key in ("constraints", "non_goals", "decisions")}
    approved_run["approval_generation"] = SESSION_MATERIAL.approval_generation(approved_run)
    approved_run["budget"]["intent_watermark"] = initial_watermark
    approved_run["intent_source"] = {
        "events_path": str(intent_events_path) if intent_events_path else "",
        "state_path": str(resolved["state_path"]),
    }
    with storage.accessible_transaction(run_dir, source, authority=storage.bound_authority(approved_run),
                                       permission_denied_noop=permission_denied_noop) as store:
        if store is None or store.run() is not None:
            return False
        current = read_session_material(resolved["state_path"].parent.parent.parent,
            resolved["platform"], resolved["session_id"], source=source)
        if expected_input_event is not None and current.get("latest_input_event") != expected_input_event:
            raise AutopilotStateError("input changed during completion admission; original caller receipt is stale")
        if current["intent_state"] != intent_state:
            raise AutopilotStateError("intent state changed during bootstrap; retry against current approval")
        storage.migrate_bound_session(store, source)
        _write_json_atomic(run_dir / APPROVED_RUN_FILE, approved_run)
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
                approval_generation=approved_run["approval_generation"],
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
    storage.append_event(run_dir, event)


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


def _quarantine_rejected_decision(run_dir: Path, decision: Mapping[str, Any], reason: str) -> None:
    """Move an unconsumable consistency-decision file aside and record why.

    The strict validation still rejects (raises); this only stops the rejected
    file from re-raising on every subsequent Stop (a permanent stall). The file
    is renamed (preserved as evidence), never deleted, and the rejection is
    appended to the audit log. Best-effort: any IO error here must not mask the
    original validation error that the caller re-raises.
    """
    rejected_path = run_dir / REJECTED_DECISION_FILE
    if rejected_path.exists():
        stem = rejected_path.name.removesuffix(".json")
        for index in range(2, 1000):
            candidate = run_dir / f"{stem}.{index}.json"
            if not candidate.exists():
                rejected_path = candidate
                break
        else:
            rejected_path = run_dir / f"{stem}.{os.getpid()}.json"
    storage.consume(run_dir / DECISION_FILE, decision, rejected_path)
    _append_event(run_dir, {"schema_version": "autopilot-event.v1",
        "event": "consistency_decision_rejected", "schema_version_seen": decision.get("schema_version"),
        "reason": reason})


def _apply_pending_decision(
    run_dir: Path,
    items: list[dict[str, Any]],
    run: Mapping[str, Any] | None = None,
    source: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    decision_path = run_dir / DECISION_FILE
    try:
        decision = storage.inbox(decision_path)
    except ValueError as exc:
        raise AutopilotStateError(str(exc)) from exc
    if decision is None:
        return items, None
    try:
        _validate_promoted_decision_file(decision)
        try:
            SESSION_MATERIAL.validate_approval_generation(run or {}, decision)
        except ValueError as exc:
            raise AutopilotStateError(str(exc)) from exc
        item_id = _require_string(decision.get("work_item_id"), "consistency decision work_item_id")
        decision_value = _require_string(decision.get("decision"), "consistency decision decision")
        evidence = _validate_string_list(decision.get("evidence"), "consistency decision evidence")
        if decision_value == "continue_next":
            store = storage.active(run_dir)
            try:
                publication = store.read("completion-publication.json") if store else None
            except FileNotFoundError:
                publication = None
            if publication is not None and (publication.get("receipt") or {}).get("work_item_id") == item_id:
                path = Path(__file__).resolve().parents[1] / "scripts" / "autopilot_completion.py"
                spec = importlib.util.spec_from_file_location("autopilot_completion_validation", path)
                helper = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(helper)
                try:
                    helper.validate_publication_decision(publication, run or {}, decision, source or {})
                except ValueError as exc:
                    raise AutopilotStateError(str(exc)) from exc
            elif decision.get("completion_origin") is not None:
                raise AutopilotStateError("completion origin has no matching prepared publication")
        updated = apply_consistency_decision(
            items,
            item_id,
            decision_value,
            completion_check_digest=decision.get("completion_check_digest"),
            verdict=decision.get("verdict"),
            evidence=evidence,
        )
        approval = (run or {}).get("approval_evidence")
        if (decision_value == "continue_next" and isinstance(approval, Mapping)
                and isinstance(approval.get("session_intent"), Mapping)):
            # Validate and record proof under core's identity/input/criterion
            # lock before promoting the AP task. A rejection cannot leave the
            # task completed; an AP write failure leaves replayable core proof.
            materialize_met_criteria_from_continue_next(run, decision, source, require_success=True)
    except AutopilotStateError as exc:
        # Reject (raise) an unconsumable decision -- fail-closed; the agent is not root and must not apply unverified state. But quarantine the offending file first so it does not re-raise on every subsequent Stop (a permanent stall): the rename preserves it as evidence, the event records the rejection, and the entrypoint still degrades to a non-blocking no-op. Fallback is correction before the next forward step; the audit log is never reduced.
        raise
    write_work_items(run_dir / TASKS_FILE, updated)
    storage.consume(decision_path, decision, run_dir / APPLIED_DECISION_FILE)
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


def _apply_pending_conduct_plan(
    run_dir: Path, items: list[dict[str, Any]], run: dict[str, Any],
) -> list[dict[str, Any]]:
    plan_path = run_dir / CONDUCT_PLAN_FILE
    plan = storage.inbox(plan_path)
    if plan is None:
        return items
    current = validate_work_items(copy.deepcopy(items))
    before_ids = {item["id"] for item in current}
    try:
        SESSION_MATERIAL.validate_approval_generation(run, plan)
    except ValueError as exc:
        raise AutopilotStateError(str(exc)) from exc
    updated = apply_conduct_plan_proposals(current, plan)
    new_items = [item for item in updated if item["id"] not in before_ids]
    rejected_ids = [item["id"] for item in new_items if not _work_item_within_run_surfaces(run, item)]
    if rejected_ids:
        # Plan approval cannot expand this run's authority. Keep the entire plan
        # pending and its evidence intact, without blocking existing approved work.
        _append_event(
            run_dir,
            {
                "schema_version": "autopilot-event.v1",
                "event": "conduct_plan_outside_allowed_surfaces",
                "run_id": run.get("run_id"),
                "source_candidate_id": plan.get("source_candidate_id"),
                "rejected_work_item_ids": rejected_ids,
                "source": plan.get("source"),
            },
        )
        return current
    imported_ids = [item["id"] for item in new_items]
    if imported_ids:
        write_work_items(run_dir / TASKS_FILE, updated)
    storage.consume(plan_path, plan, run_dir / APPLIED_CONDUCT_PLAN_FILE)
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


def _iotrace_resumes_since_last_replenish(run_dir: Path, work_item_id: str) -> int:
    # "Reset N" budget: count io-trace resumes for this item since the last session-intent replenish. A new intent epoch (replenish event) resets the allowance to the base limit, rather than accumulating a lifetime ceiling.
    count = 0
    for event in _read_jsonl_objects(run_dir / EVENTS_FILE):
        if event.get("work_item_id") != work_item_id:
            continue
        name = event.get("event")
        if name == "resume_budget_replenished":
            count = 0
        elif name == "resume_running_item_from_iotrace":
            count += 1
    return count


def _iotrace_resume_limit(source: Mapping[str, str] | None) -> int:
    try:
        value = int(str((source or {}).get(IOTRACE_RESUME_LIMIT_ENV)).strip())
    except (TypeError, ValueError):
        return IOTRACE_RESUME_LIMIT_DEFAULT
    return value if value >= 1 else IOTRACE_RESUME_LIMIT_DEFAULT


# --- intent-driven resume budget --------------------------------------------
# The resume ceiling is not a dead static count: it is replenished whenever the session-intent ledger advances (a new user input, or new work the agent routed through session-intent-analyzer -> new intent-events lines). Each advance resets the allowance to the base limit (reset-N); with no new intent the ceiling holds and the run escalates to ask_user_meta instead of re-firing forever. Platform-neutral.
def _intent_events_count(events_path: Path) -> int:
    try:
        text = events_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def _run_intent_events_path(run: Mapping[str, Any]) -> Path | None:
    intent_source = run.get("intent_source")
    if not isinstance(intent_source, Mapping):
        return None
    events = intent_source.get("events_path")
    return Path(events) if isinstance(events, str) and events.strip() else None


def _last_intent_watermark(run_dir: Path, run: Mapping[str, Any], work_item_id: str) -> int:
    seen = [
        event.get("intent_events_seen")
        for event in _read_jsonl_objects(run_dir / EVENTS_FILE)
        if event.get("event") == "resume_budget_replenished"
        and event.get("work_item_id") == work_item_id
        and isinstance(event.get("intent_events_seen"), int)
        and not isinstance(event.get("intent_events_seen"), bool)
    ]
    if seen:
        return max(seen)
    budget = run.get("budget")
    base = budget.get("intent_watermark") if isinstance(budget, Mapping) else None
    return base if isinstance(base, int) and not isinstance(base, bool) else 0


def _maybe_replenish_resume_budget(run_dir: Path, run: Mapping[str, Any], work_item_id: str,
                                   current_intent: Mapping[str, Any] | None = None) -> bool:
    events_path = _run_intent_events_path(run)
    if events_path is None:
        return False
    binding = (run.get("approval_evidence") or {}).get("session_intent")
    if binding:
        if current_intent is None or not isinstance(current_intent.get("events"), list):
            return False
        current = len(current_intent["events"])
    else:
        current = _intent_events_count(events_path)  # Legacy standalone runs have no session binding.
    if current <= _last_intent_watermark(run_dir, run, work_item_id):
        return False
    _append_event(
        run_dir,
        {
            "schema_version": "autopilot-event.v1",
            "event": "resume_budget_replenished",
            "run_id": run.get("run_id"),
            "work_item_id": work_item_id,
            "intent_events_seen": current,
        },
    )
    return True


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


def _current_intent_for_source(
    source: Mapping[str, str] | None, project_cwd: Path, run_platform: str | None = None,
    *, hook_input: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if source is None or not _has_explicit_session_intent_context(source, project_cwd):
        return None
    if not str(source.get("GHOST_ALICE_PLATFORM") or "").strip() and run_platform in ("codex", "claude"):
        source = {**source, "GHOST_ALICE_PLATFORM": run_platform}
    for candidate in _iter_current_session_intents(source, project_cwd, hook_input=hook_input):
        if isinstance(candidate.get("intent_state"), Mapping):
            return candidate
    return None


def _io_trace_candidate_for_item(
    run: Mapping[str, Any],
    item: Mapping[str, Any],
    source: Mapping[str, str] | None,
    io_trace_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    publication = run.get("completion_publication")
    if (isinstance(publication, Mapping) and publication.get("status") == "prepared"
            and (publication.get("receipt") or {}).get("work_item_id") == item.get("id")):
        return None
    if run.get("completion_origin_gap") or not io_trace_rows:
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


def _advance_approved_run_locked(
    root: Path, source: Mapping[str, str] | None = None, *, hook_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(root)
    approved_run_path = root / APPROVED_RUN_FILE
    tasks_path = root / TASKS_FILE
    if (root / OFF_FILE).exists():
        return _noop_payload()
    conduct_plan_path = root / CONDUCT_PLAN_FILE
    if not _run_state_available(root):
        return _noop_payload()

    run = _read_json_object(approved_run_path)
    run["approval_generation"] = SESSION_MATERIAL.approval_generation(run)
    if not _approved_run_allows_continue(run):
        return _noop_payload()
    approval = run.get("approval_evidence")
    binding = approval.get("session_intent") if isinstance(approval, Mapping) else None
    run_platform = binding.get("platform") if isinstance(binding, Mapping) else None
    source = current_session_context(source, run_platform=run_platform, hook_input=hook_input)
    context_error = continuation_context_error(binding, source or {})
    if context_error:
        _append_event(root, {
            "schema_version": "autopilot-event.v1",
            "event": "stale_continuation_parked",
            "run_id": run.get("run_id"),
            "reason": context_error,
        })
        return _noop_payload()
    io_trace_rows = _io_trace_rows_for_run(run, source)
    signal_base = str(root.parent)
    signal_home = str((source or {}).get("HOME") or Path.home())
    project_cwd = _project_cwd_from_env(source or {})

    items = read_work_items(tasks_path) if storage.exists(tasks_path) else []
    current_intent = _current_intent_for_source(source, project_cwd, run_platform, hook_input=hook_input)
    starvation_event = semantic_delta_starvation_event(current_intent)
    if starvation_event is not None:
        _append_event(root, starvation_event)
        return {
            "continue": True,
            "systemMessage": build_semantic_delta_starvation_message(starvation_event),
        }
    binding_event = session_binding_mismatch_event(run, items, current_intent, source)
    if binding_event is not None:
        _append_event(root, binding_event)
        return _noop_payload()
    if current_intent is None:
        parked_event = stale_continuation_missing_intent_event(
            run, items, str((source or {}).get("GHOST_ALICE_SESSION_ID") or ""),
            require_current_intent=_has_explicit_session_intent_context(source or {}, project_cwd),
        )
        if parked_event is not None:
            _append_event(root, parked_event)
            return _noop_payload()
    if recovery := reapproval_payload(root, run, source or {}):
        return recovery
    parked_event = stale_continuation_event(run, items, current_intent)
    if parked_event is not None:
        _append_event(root, parked_event)
        return _noop_payload()
    run.update(bind_origin(root, run, items, source or {}))
    items, applied_decision = _apply_pending_decision(root, items, run, source)
    store = storage.active(root)
    if store is not None:
        try:
            run["completion_publication"] = store.read("completion-publication.json")
        except FileNotFoundError:
            pass
    if applied_decision is not None and applied_decision["decision"] == "ask_user_meta":
        return {
            "continue": True,
            "systemMessage": build_meta_intervention_message(
                run,
                work_item_id=applied_decision["work_item_id"],
                evidence=applied_decision["evidence"],
            ),
        }
    items = _apply_pending_conduct_plan(root, items, run)

    ready_queue = derive_ready_queue(items)
    if not ready_queue:
        running_items = [item for item in items if item["status"] == "running"]
        if running_items:
            parked_event = stale_continuation_source_intent_event(
                run,
                items,
                current_intent,
                str((source or {}).get("GHOST_ALICE_SESSION_ID") or ""),
            )
            if parked_event is not None:
                _append_event(root, parked_event)
                return _noop_payload()
            running_item = running_items[0]
            if _work_item_within_run_surfaces(run, running_item):
                governance_candidate = _io_trace_candidate_for_item(run, running_item, source, io_trace_rows)
                if _missing_decision_resume_count(root, running_item["id"]) >= 1:
                    _maybe_replenish_resume_budget(root, run, running_item["id"], current_intent)
                    resume_limit = _iotrace_resume_limit(source)
                    if io_trace_rows and _iotrace_resumes_since_last_replenish(root, running_item["id"]) < resume_limit:
                        _append_iotrace_resume_event(root, run, running_item, governance_candidate)
                        return {
                            "continue": True,
                            "systemMessage": build_continuation_message(
                                run,
                                running_item,
                                pending_decision=True,
                                io_trace_rows=io_trace_rows,
                                governance_candidate=governance_candidate,
                                base_path=signal_base,
                                home_path=signal_home,
                            ),
                        }
                    evidence = (
                        [f"loop-guard: io-trace resume limit reached ({resume_limit})"]
                        if io_trace_rows
                        else ["loop-guard: repeated missing decision"]
                    )
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
                        base_path=signal_base,
                        home_path=signal_home,
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
            base_path=signal_base,
            home_path=signal_home,
        ),
    }


def advance_approved_run(
    run_dir: str | Path, env: Mapping[str, str] | None = None, *, hook_input: Mapping[str, Any] | None = None,
    permission_denied_noop: bool = False,
) -> dict[str, Any]:
    root = Path(run_dir)
    approved_run_path = root / APPROVED_RUN_FILE
    tasks_path = root / TASKS_FILE
    conduct_plan_path = root / CONDUCT_PLAN_FILE
    if (root / OFF_FILE).exists():
        return _noop_payload()
    if not _run_state_available(root):
        return _noop_payload()
    source = os.environ if env is None else env
    run = _read_json_object(approved_run_path)
    if not _approved_run_allows_continue(run):
        return _noop_payload()
    if not (root / storage.AUTHORITY_FILE).exists():
        try:
            storage.bound_authority(run)
        except ValueError as exc:
            # Invalid legacy binding has no authority to migrate. Preserve an
            # audit-only diagnostic without changing approval/tasks/Core state.
            with (root / EVENTS_FILE).open("a", encoding="utf-8") as out:
                out.write(json.dumps({"event": "stale_continuation_parked", "reason": str(exc),
                    "authority": "unmigrated-legacy-diagnostic"}) + "\n")
            return _noop_payload()
    try:
        with storage.accessible_transaction(root, source, permission_denied_noop=permission_denied_noop) as store:
            return _noop_payload() if store is None else _advance_approved_run_locked(root, env, hook_input=hook_input)
    except AutopilotStateError as exc:
        # First roll back every Core/task mutation. Rejection is a separate
        # durable audit transaction and cannot accidentally commit partial proof.
        if (root / DECISION_FILE).is_file():
            try:
                raw = (root / DECISION_FILE).read_bytes()
                try:
                    decision = json.loads(raw)
                    if not isinstance(decision, dict):
                        raise ValueError("decision must be an object")
                    raw_digest = None
                except ValueError:
                    raw_digest = "raw:" + hashlib.sha256(raw).hexdigest()
                    decision = {"invalid_json": raw.decode("utf-8", errors="replace"), "raw_digest": raw_digest}
                with storage.transaction(root, source) as store:
                    store.inbox_bytes[DECISION_FILE] = raw
                    _quarantine_rejected_decision(root, decision, str(exc))
            except (OSError, ValueError):
                pass
        raise


def _bootstrap_then_advance(
    root: Path, source: Mapping[str, str], project_cwd: Path, *, derived_run_dir: bool = False,
    hook_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        if derived_run_dir:
            return _noop_payload()
        raise
    if not _run_state_available(root):
        _bootstrap_from_session_intent_if_approved(root, current_session_context(source, hook_input=hook_input),
            project_cwd, hook_input=hook_input, permission_denied_noop=derived_run_dir)
    return advance_approved_run(root, source, hook_input=hook_input, permission_denied_noop=derived_run_dir)


def adapter_payload_from_env(
    env: Mapping[str, str] | None = None, *, hook_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = os.environ if env is None else env
    if env is not None and not source:
        return _noop_payload()
    selected = resolve_run_target(env)
    if not selected["bootstrap"]:
        return advance_approved_run(selected["run_dir"], source, hook_input=hook_input)
    return _bootstrap_then_advance(
        selected["run_dir"], source, selected["project_cwd"],
        derived_run_dir=selected["derived_run_dir"], hook_input=hook_input,
    )

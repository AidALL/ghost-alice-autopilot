#!/usr/bin/env python3
"""Durable Stop adapter orchestration for Ghost-ALICE Autopilot.

Dependencies: Python 3.11+ standard library plus sibling adapter modules.
"""

from __future__ import annotations

import copy
import hashlib
import autopilot_storage as storage
from autopilot_provenance import bind_origin, reapproval_payload
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from autopilot_messages import (
    build_continuation_message,
    build_meta_intervention_message,
)
from autopilot_intent_recovery import (
    project_queue_session_material as project_session_material,
    admission_preflight,
)
from autopilot_runtime_context import (
    SessionAuthorityError,
    project_cwd_from_env as _project_cwd_from_env,
    read_session_material,
    resolve_run_target,
    session_intent_root_candidates as _session_intent_root_candidates,
    run_is_paused,
    run_state_available as _run_state_available,
)
from autopilot_lineage import (
    agent_runtime_context_is_explicit,
    continuation_context_error,
    current_session_context,
    session_binding_mismatch_event,
    stale_continuation_missing_intent_event,
)
from autopilot_work_items import (
    APPROVAL_DECISIONS,
    COMPLETION_CHECK_DIGEST_PATTERN,
    AutopilotStateError,
    _find_item,
    _has_explicit_approval_evidence,
    _require_string,
    _validate_string_list,
    _with_core_policy_source,
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
# Ceiling on io-trace-backed resumes of a decision-less running item; past it,
# park recording without inventing a human decision or stopping the business task.
IOTRACE_RESUME_LIMIT_DEFAULT = 3
IOTRACE_RESUME_LIMIT_ENV = "GHOST_ALICE_AUTOPILOT_IOTRACE_RESUME_LIMIT"
NOOP_PAYLOAD = {"continue": True, "systemMessage": ""}
CONSISTENCY_DECISION_SCHEMA = "autopilot-consistency-decision.v1"
CONSISTENCY_DECISION_CANDIDATE_SCHEMA = "autopilot-consistency-decision-candidate.v1"
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
    platform = source.get("GHOST_ALICE_PLATFORM")
    if platform not in {"codex", "claude"} or not isinstance(session_id, str) or not session_id.strip():
        return []
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
        # Audit diagnostics are not tool activity. Older rows without an exact
        # platform remain on disk, but cannot authenticate a continuation.
        if row.get("session") != session_id or row.get("platform") != platform:
            continue
        tool = row.get("tool")
        if not isinstance(tool, str) or not tool.strip() or tool.strip().lower() in {"unknown", "n/a"}:
            continue
        if row.get("event") not in (None, "PostToolUse", "AfterTool"):
            continue
        compact = {
            key: row[key]
            for key in ("ts", "platform", "session", "tool", "path", "pattern", "op")
            if isinstance(row.get(key), str)
        }
        if compact:
            selected.append(compact)
        if len(selected) >= limit:
            break
    return list(reversed(selected))


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
    if run_is_paused(run_dir) or _run_state_available(run_dir):
        return False
    if not _has_explicit_session_intent_context(source, project_cwd):
        return False
    resolved = None
    approval = None
    for candidate in _iter_current_session_intents(source, project_cwd, hook_input=hook_input):
        from autopilot_runtime_context import selected_session_context
        source = selected_session_context(candidate, source, hook_input=hook_input)
        candidate, selection = project_session_material(candidate, source, run_dir)
        if selection["status"] != "selected":
            continue
        intent_state = candidate["intent_state"]
        if not isinstance(intent_state, Mapping):
            continue
        candidate_approval = (
            _approval_from_env(source)
            or _approval_from_session_decisions(intent_state)
        )
        if candidate_approval is None:
            continue
        resolved = candidate
        approval = candidate_approval
        break
    if resolved is None:
        return False
    if expected_input_event is not None and resolved.get("latest_input_event") != expected_input_event:
        raise AutopilotStateError("input changed before completion admission; expected_input_event no longer matches current intake")
    intent_state = resolved["intent_state"]
    if not isinstance(intent_state, Mapping):
        return False
    if approval is None:
        return False

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
                                       permission_denied_noop=permission_denied_noop,
                                       preflight=admission_preflight(resolved, source, run_dir, queue=True)) as store:
        if store is None or store.run() is not None:
            return False
        current = read_session_material(resolved["state_path"].parent.parent.parent,
            resolved["platform"], resolved["session_id"], source=source)
        current, selection = project_session_material(current, source, run_dir)
        if selection["status"] != "selected":
            raise AutopilotStateError("current response route changed during bootstrap")
        if expected_input_event is not None and current.get("latest_input_event") != expected_input_event:
            raise AutopilotStateError("input changed during completion admission; expected_input_event no longer matches current intake")
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
    """Consume a rejected decision's receipt and archive its captured bytes.

    The caller rolls back work changes before recording rejection in a separate
    transaction. The consumed receipt prevents replay; after commit, storage
    archives the captured snapshot while leaving the producer's inbox intact.
    The caller preserves the original validation error if this recording fails.
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
                import autopilot_completion_service as helper
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
            # Core proof and Ghost-ALICE Autopilot task completion share one transaction under
            # the current identity/input/criterion lock. A rejection or later
            # Ghost-ALICE Autopilot write failure rolls back both.
            materialize_met_criteria_from_continue_next(run, decision, source, require_success=True)
    except AutopilotStateError as exc:
        # The outer caller rolls back this transaction before consuming the
        # rejected receipt and recording its diagnostic in a separate one.
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
# Standalone queues retain their supplied event watermark. Bound queues keep
# retries within the immutable approval generation; Core audit events cannot
# replenish it. Explicit reapproval creates a generation with its own history.
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


def _maybe_replenish_resume_budget(run_dir: Path, run: Mapping[str, Any], work_item_id: str) -> bool:
    if (run.get("approval_evidence") or {}).get("session_intent"):
        return False
    events_path = _run_intent_events_path(run)
    if events_path is None:
        return False
    current = _intent_events_count(events_path)
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


def _select_ready_item(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    updated = validate_work_items(copy.deepcopy(items))
    item = _find_item(updated, item_id)
    item["status"] = "running"
    return {"item": item, "items": updated}


def _append_iotrace_resume_event(
    root: Path,
    run: Mapping[str, Any],
    item: Mapping[str, Any],
) -> None:
    event = {
        "schema_version": "autopilot-event.v1",
        "event": "resume_running_item_from_iotrace",
        "run_id": run.get("run_id"),
        "work_item_id": item.get("id"),
    }
    _append_event(root, event)


def _advance_approved_run_locked(
    root: Path, source: Mapping[str, str] | None = None, *, hook_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(root)
    approved_run_path = root / APPROVED_RUN_FILE
    tasks_path = root / TASKS_FILE
    if run_is_paused(root):
        return _noop_payload()
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
            running_item = running_items[0]
            if _work_item_within_run_surfaces(run, running_item):
                if _missing_decision_resume_count(root, running_item["id"]) >= 1:
                    _maybe_replenish_resume_budget(root, run, running_item["id"])
                    resume_limit = _iotrace_resume_limit(source)
                    if io_trace_rows and _iotrace_resumes_since_last_replenish(root, running_item["id"]) < resume_limit:
                        _append_iotrace_resume_event(root, run, running_item)
                        return {
                            "continue": True,
                            "systemMessage": build_continuation_message(
                                run,
                                running_item,
                                pending_decision=True,
                                io_trace_rows=io_trace_rows,
                                base_path=signal_base,
                                home_path=signal_home,
                            ),
                        }
                    evidence = (
                        [f"loop-guard: io-trace resume limit reached ({resume_limit})"]
                        if io_trace_rows
                        else ["loop-guard: repeated missing decision"]
                    )
                    diagnostic = {
                        "schema_version": "autopilot-event.v1",
                        "event": "missing_decision_parked",
                        "run_id": run.get("run_id"),
                        "work_item_id": running_item["id"],
                        "approval_generation": run["approval_generation"],
                        "evidence": evidence,
                    }
                    history = _read_jsonl_objects(root / EVENTS_FILE)
                    if not history or any(history[-1].get(key) != value for key, value in diagnostic.items()):
                        _append_event(root, diagnostic)
                    return {
                        "continue": True,
                        "systemMessage": "[autopilot]\npending-decision: repeated-missing-decision\n"
                            + "\n".join(evidence)
                            + "\nRecording is parked until an authorized queue decision is available. "
                            "The original task and evidence remain unchanged.",
                        "completion_recording": {"status": "parked", "reason": "missing-decision",
                            "work_item_id": running_item["id"]},
                        "recovery": {"status": "parked", "claims_changed": False},
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
    event = {
        "schema_version": "autopilot-event.v1",
        "event": "continue_next_item",
        "run_id": run.get("run_id"),
        "work_item_id": selected_item["id"],
        "focus_layer": selected_item["focus_layer"],
    }
    _append_event(root, event)
    return {
        "continue": True,
        "systemMessage": build_continuation_message(
            run,
            selected_item,
            io_trace_rows=io_trace_rows,
            base_path=signal_base,
            home_path=signal_home,
        ),
    }


@_with_core_policy_source
def advance_approved_run(
    run_dir: str | Path, env: Mapping[str, str] | None = None, *, hook_input: Mapping[str, Any] | None = None,
    permission_denied_noop: bool = False,
) -> dict[str, Any]:
    root = Path(run_dir)
    approved_run_path = root / APPROVED_RUN_FILE
    if run_is_paused(root):
        return _noop_payload()
    if not _run_state_available(root):
        return _noop_payload()
    source = os.environ if env is None else env
    run = _read_json_object(approved_run_path)
    if not _approved_run_allows_continue(run):
        return _noop_payload()
    binding = (run.get("approval_evidence") or {}).get("session_intent")
    if error := continuation_context_error(binding, source):
        return dict(_noop_payload(), completion_recording={"status": "recording-unavailable", "reason": "authority-mismatch", "detail": error})
    if isinstance(binding, Mapping) and binding.get("platform") in {"codex", "claude", "agent-runtime"}:
        current = current_session_context(source, run_platform=binding["platform"], hook_input=hook_input)
        try:
            material = read_session_material(Path(binding["state_path"]).parent.parent.parent,
                binding["platform"], current.get("GHOST_ALICE_SESSION_ID") or None, source=current, require_input=True, recover_audit=False)
            from autopilot_runtime_context import selected_session_context
            current = selected_session_context(material, current, hook_input=hook_input, allow_discovered_identity=True)
            _, selection = project_session_material(material, current, root)
        except (ValueError, OSError) as exc:
            return dict(_noop_payload(), completion_recording={"status": "recording-unavailable", "reason": "authority-unavailable", "detail": str(exc)})
        if selection["status"] != "selected":
            return dict(_noop_payload(), completion_recording=selection)
        source = current
        if binding.get("selected_work_units") != selection["selected_work_units"]:
            if binding.get("selected_work_units"):
                with storage.accessible_transaction(root, source, permission_denied_noop=permission_denied_noop) as store:
                    if store is not None and (recovery := reapproval_payload(root, run, current)):
                        return recovery
            return dict(_noop_payload(), completion_recording={"status": "recording-unavailable", "reason": "unbound-work-unit"})
    if not (root / storage.AUTHORITY_FILE).exists():
        try:
            storage.bound_authority(run)
        except ValueError as exc:
            # Invalid source binding has no authority to import. Preserve an
            # audit-only diagnostic without changing approval/tasks/Core state.
            with (root / EVENTS_FILE).open("a", encoding="utf-8") as out:
                out.write(json.dumps({"event": "stale_continuation_parked", "reason": str(exc),
                    "authority": "unmigrated-legacy-diagnostic"}) + "\n")
            return _noop_payload()
    try:
        with storage.accessible_transaction(root, source, permission_denied_noop=permission_denied_noop) as store:
            return _noop_payload() if store is None else _advance_approved_run_locked(root, source, hook_input=hook_input)
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
    if run_is_paused(root):
        return _noop_payload()
    if not _run_state_available(root):
        current = current_session_context(source, hook_input=hook_input)
        selection = {"status": "recording-unavailable", "reason": "authority-unavailable"}
        for material in _iter_current_session_intents(current, project_cwd, hook_input=hook_input):
            from autopilot_runtime_context import selected_session_context
            current = selected_session_context(material, current, hook_input=hook_input)
            _, selection = project_session_material(material, current, root)
            break
        if selection["status"] != "selected":
            return dict(_noop_payload(), completion_recording=selection)
        source = current
        _bootstrap_from_session_intent_if_approved(root, current,
            project_cwd, hook_input=hook_input, permission_denied_noop=derived_run_dir)
    return advance_approved_run(root, source, hook_input=hook_input, permission_denied_noop=derived_run_dir)


def adapter_payload_from_env(
    env: Mapping[str, str] | None = None, *, hook_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = os.environ if env is None else env
    if env is not None and not source:
        return _noop_payload()
    source = current_session_context(source, hook_input=hook_input) or source
    from autopilot_lineage import completion_protocol
    if completion_protocol(source) == "autopilot-completion-receipt.v2":
        import autopilot_completion_service as helper
        try:
            from autopilot_continuation import advance_continuation
            applied = helper.apply_completion_v2(source, hook_input=hook_input)
            return advance_continuation(source, applied, hook_input=hook_input)
        except (ValueError, OSError) as exc:
            return helper.recording_failure_payload(source, hook_input, exc)
    try:
        selected = resolve_run_target(source if hook_input is not None else env)
    except SessionAuthorityError as exc:
        return dict(_noop_payload(), completion_recording={"status": "recording-unavailable", "reason": "authority-unavailable", "detail": str(exc)})
    source = selected["source"]
    if not selected["bootstrap"]:
        return advance_approved_run(selected["run_dir"], source, hook_input=hook_input)
    return _bootstrap_then_advance(
        selected["run_dir"], source, selected["project_cwd"],
        derived_run_dir=selected["derived_run_dir"], hook_input=hook_input,
    )

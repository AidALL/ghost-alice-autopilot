#!/usr/bin/env python3
"""Session-intent admission and recovery predicates for Ghost-ALICE Autopilot.

Dependencies: Python 3.11+, sibling adapters and Core's native completion API.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


MET_CRITERION_STATUSES = frozenset({"met", "done", "satisfied", "pass", "passed", "complete", "completed", "resolved", "closed"})
SEMANTIC_DELTA_STARVATION_THRESHOLD = 3


def _core_native_assessor(core):
    """Load Core's authority boundary, without assembling Core policy inputs."""
    shared = Path(core.__file__).resolve().parents[2] / "_shared"
    spec = importlib.util.spec_from_file_location("ap_core_native_completion", shared / "native_completion.py")
    if spec is None or spec.loader is None:
        raise ImportError("Core native completion assessment unavailable")
    sys.path.insert(0, str(shared))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(shared))
    return module.assess_native_completion


def _selection_shape_valid(route, selected, platform, session, input_event_id):
    """Validate the Core fields Ghost-ALICE Autopilot consumes before indexing frozen selections."""
    if (not isinstance(selected, dict) or selected.get("schema_version") != "completion-route.v1"
            or selected.get("route_binding") not in ("current", "missing", "stale", "invalid")
            or selected.get("publication_route") not in ("none", "prepare", "publish", "blocked")
            or not all(isinstance(selected.get(k), list) for k in ("selected_criteria", "selected_work_unit_ids"))):
        return False
    ids, criteria = selected["selected_work_unit_ids"], selected["selected_criteria"]
    if not all(isinstance(unit_id, str) and unit_id.strip() for unit_id in ids) or len(ids) != len(set(ids)):
        return False
    if selected["route_binding"] != "current":
        return not ids and not criteria
    if (not isinstance(route, dict) or route.get("schema_version") != "response-route.v1"
            or route.get("status") != "ready" or route.get("platform") != platform or route.get("session_id") != session
            or not isinstance(input_event_id, str) or not input_event_id
            or route.get("input_event_id") != input_event_id or route.get("current_input_event_id") != input_event_id
            or not isinstance(route.get("work_units"), list) or not isinstance(route.get("selected_criteria"), list)
            or route.get("selected_work_unit_ids") != ids or route["selected_criteria"] != criteria):
        return False
    surface = route.get("routing_surface")
    if (not isinstance(surface, dict) or surface.get("response_mode") not in ("normal", "direct-response", "clarification-only")
            or surface.get("verification_complexity") not in ("level-1", "level-2", "level-3")
            or surface.get("boundary_contract") not in ("required", "n/a")
            or (surface["boundary_contract"] == "required" and
                (not isinstance(route.get("boundary_ref"), str) or not route["boundary_ref"].strip()))):
        return False
    units = {}
    for unit in route["work_units"]:
        if (not isinstance(unit, dict) or not isinstance(unit.get("work_unit_id"), str) or not unit["work_unit_id"].strip()
                or unit["work_unit_id"] in units or type(unit.get("contract_revision")) is not int or unit["contract_revision"] < 1
                or unit.get("owner") != {"platform": platform, "session_id": session}
                or unit.get("kind") not in ("execution", "nonexecution") or type(unit.get("admitted")) is not bool):
            return False
        units[unit["work_unit_id"]] = unit
    if any(unit_id not in units or units[unit_id]["admitted"] is not True for unit_id in ids):
        return False
    for rows in (criteria, route["selected_criteria"]):
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("work_unit_id"), str) or row["work_unit_id"] not in ids
                    or type(row.get("contract_revision")) is not int or row["contract_revision"] < 1
                    or row["contract_revision"] != units[row["work_unit_id"]]["contract_revision"]
                    or not isinstance(row.get("criterion_id"), str) or not row["criterion_id"].strip()
                    or row.get("id") != row["criterion_id"] or row.get("admitted") is not True
                    or row.get("status") not in ("met", "unmet")
                    or not all(isinstance(row.get(k), str) and row[k].strip() for k in ("summary", "source"))):
                return False
            key = (row["work_unit_id"], row["contract_revision"], row["criterion_id"])
            if key in seen:
                return False
            seen.add(key)
    return True


def current_execution_projection(material, source, run_dir, *, transaction=None, queue=False, claim_scope=None):
    """Select Ghost-ALICE Autopilot work through Core's current route; never infer it from backlog.

    A recording status grants no permission and never changes Core/Ghost-ALICE Autopilot state.
    v1 binds exactly one execution unit. Explicit v2 dispatch retains exact
    independent qualified selections without adopting accumulated history.
    """
    from autopilot_lineage import completion_protocol
    protocol = completion_protocol(source)
    from autopilot_runtime_context import run_is_paused, run_owner_hint, run_owner_matches
    from autopilot_work_items import _load_core_ledger_module
    from autopilot_storage import core_transaction
    result = {"status": "skipped", "reason": "no-current-execution"}
    if run_is_paused(Path(run_dir)):
        return dict(result, reason="autopilot-off")
    root = Path(material["state_path"]).parent.parent.parent.resolve()
    platform, session = material["platform"], material["session_id"]
    if any(not source.get(k) for k in ("GHOST_ALICE_PLATFORM", "GHOST_ALICE_SESSION_ID", "GHOST_ALICE_SESSION_INTENT_ROOT")):
        return {"status": "recording-unavailable", "reason": "authority-missing"}
    if (platform not in ({"codex", "claude", "agent-runtime"} if queue else {"codex", "claude"})
            or source["GHOST_ALICE_PLATFORM"] != platform or source["GHOST_ALICE_SESSION_ID"] != session
            or Path(source["GHOST_ALICE_SESSION_INTENT_ROOT"]).expanduser().resolve() != root):
        return {"status": "recording-unavailable", "reason": "authority-mismatch"}
    state = material["intent_state"]
    owner = run_owner_hint(Path(run_dir))
    authority = dict(source, GHOST_ALICE_PLATFORM=platform, GHOST_ALICE_SESSION_ID=session,
                     GHOST_ALICE_SESSION_INTENT_ROOT=str(root))
    if owner is not None and not run_owner_matches(owner, authority):
        return {"status": "recording-unavailable", "reason": "run-owner-mismatch"}
    core = _load_core_ledger_module(material["state_path"], source)
    if core is None or not callable(getattr(core, "read_response_route", None)):
        return {"status": "recording-unavailable", "reason": "core-route-api-unavailable"}
    scope = claim_scope if isinstance(claim_scope, dict) else {}
    text = scope.get("text", "")
    phase = scope.get("phase", "before_tool")
    if not isinstance(text, str) or phase not in {"before_tool", "publication", "final"}:
        return {"status": "recording-unavailable", "reason": "core-assessment-incompatible"}
    assessment = None
    try:
        connection = transaction if transaction is not None else core_transaction(root)
        assessment = _core_native_assessor(core)(authority, text=text, phase=phase, transaction=connection,
            native_input=scope.get('native_input'))
        if (not isinstance(assessment, dict) or assessment.get("schema_version") != "native-completion.v1"
                or assessment.get("platform") != platform or assessment.get("session_id") != session
                or assessment.get("phase") != phase
                or assessment.get("input_event_id") != state.get("latest_input_event_id")
                or assessment.get("body_digest") != "sha256:" + hashlib.sha256(text.encode()).hexdigest()
                or "body_issue" not in assessment
                or not isinstance(assessment["body_issue"], (str, type(None)))
                or not isinstance(assessment.get("validation_text"), str)
                or type(assessment.get("has_evidence_marker")) is not bool):
            return {"status": "recording-unavailable", "reason": "core-assessment-incompatible"}
        route = assessment.get("response_route")
        selected = assessment.get("completion_route")
    except (sqlite3.Error, OSError) as exc:
        return {"status": "recording-unavailable", "reason": "core-route-recording-failed",
                "detail": str(exc), "error_type": type(exc).__name__}
    except (ValueError, ImportError, AttributeError, TypeError) as exc:
        return {"status": "recording-unavailable", "reason": "core-assessment-incompatible", "detail": str(exc)}
    if not _selection_shape_valid(route, selected, platform, session, state.get("latest_input_event_id")):
        return {"status": "recording-unavailable", "reason": "core-route-incompatible"}
    result.update(completion_route=selected, response_route=route,
                  selected_criteria=copy.deepcopy(selected["selected_criteria"]))
    if selected.get("claim_report_issue"):
        return dict(result, status="recording-unavailable", reason="claim-evidence-invalid",
                    detail=selected["claim_report_issue"])
    if assessment is not None and assessment.get("body_issue"):
        return dict(result, status="recording-unavailable", reason="completion-body-invalid",
                    detail=assessment["body_issue"])
    if selected["route_binding"] != "current":
        if selected["publication_route"] == "none":
            return dict(result, reason="awaiting-current-route" if phase == "before_tool" else "response-no-publication")
        return dict(result, status="recording-unavailable", reason="route-" + selected["route_binding"])
    units = [u for u in route["work_units"] if u["work_unit_id"] in selected["selected_work_unit_ids"]
             and u["kind"] == "execution" and u["admitted"] is True]
    if not units:
        return result
    if protocol != "autopilot-completion-receipt.v2" and (len(units) != 1 or len(selected["selected_work_unit_ids"]) != 1):
        return dict(result, status="recording-unavailable", reason="v1-multiple-work-units")
    keys = {(c["work_unit_id"], c["contract_revision"], c["criterion_id"]) for c in selected["selected_criteria"]}
    criteria = [copy.deepcopy(c) for c in state.get("acceptance_criteria", [])
                if (c.get("work_unit_id"), c.get("contract_revision"), c.get("id")) in keys]
    projected = copy.deepcopy(state)
    projected["acceptance_criteria"] = criteria
    result.update(intent_state=projected, selected_work_units=copy.deepcopy(units))
    if selected["publication_route"] == "none":
        return dict(result, reason="response-no-publication")
    if selected["publication_route"] != "prepare":
        return dict(result, status="recording-unavailable", reason="core-publication-blocked")
    if not any(_criterion_is_unmet(c) for c in criteria):
        return dict(result, reason="no-current-unmet-criteria")
    return dict(result, status="selected", reason="current-admitted-execution")


def project_session_material(material, source, run_dir, *, transaction=None, queue=False, claim_scope=None):
    """Return the selected contract plus a non-mutating typed selection status."""
    selection = current_execution_projection(material, source, run_dir, transaction=transaction, queue=queue, claim_scope=claim_scope)
    if "intent_state" not in selection:
        return material, selection
    projected = dict(material, intent_state=selection["intent_state"],
        selected_work_units=selection["selected_work_units"],
        selected_criteria=selection["selected_criteria"], response_route=selection["response_route"])
    return projected, selection


def project_queue_session_material(material, source, run_dir, *, transaction=None):
    """Approved queue supports agent-runtime without enabling publication there."""
    return project_session_material(material,source,run_dir,transaction=transaction,queue=True)


def admission_preflight(material, source, run_dir, *, queue=False):
    """Recheck under the authority lock before storage creates any Ghost-ALICE Autopilot surface."""
    def check(connection):
        from autopilot_work_items import _load_core_ledger_module
        core = _load_core_ledger_module(material["state_path"], source)
        state = core.read_session_state(root=Path(material["state_path"]).parent.parent.parent,
            platform=material["platform"], session_id=material["session_id"], transaction=connection)
        current, selected = project_session_material(dict(material, intent_state=state), source, run_dir,
            transaction=connection, queue=queue)
        if selected["status"] != "selected" or current["intent_state"] != material["intent_state"]:
            raise ValueError("current input or response route changed before admission")
    return check


def _criterion_is_unmet(criterion: Mapping[str, Any]) -> bool:
    status = str(criterion.get("status") or "unmet").strip().lower()
    return status not in MET_CRITERION_STATUSES


def semantic_delta_starvation_event(current_intent: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not current_intent:
        return None
    intent_state = current_intent.get("intent_state")
    if not isinstance(intent_state, Mapping):
        return None
    if str(intent_state.get("current_goal") or intent_state.get("user_intent_summary") or "").strip():
        return None
    if intent_state.get("last_semantic_delta_status") != "not-provided":
        return None
    events_path_value = current_intent.get("events_path")
    if not events_path_value:
        return None
    events = current_intent.get("events")
    if not isinstance(events, list):
        return None
    recent = events[-SEMANTIC_DELTA_STARVATION_THRESHOLD:]
    if len(recent) < SEMANTIC_DELTA_STARVATION_THRESHOLD:
        return None
    if any(
        event.get("event") != "user-input-observed"
        or event.get("intent_delta_status", "not-provided") != "not-provided"
        for event in recent
    ):
        return None
    digest_only_count = sum(
        1 for event in events
        if event.get("event") == "user-input-observed"
        and event.get("intent_delta_status", "not-provided") == "not-provided"
    )
    latest = recent[-1]
    return {
        "schema_version": "autopilot-event.v1",
        "event": "semantic_delta_starvation",
        "platform": current_intent.get("platform"),
        "session_id": current_intent.get("session_id") or intent_state.get("session_id"),
        "state_path": str(current_intent.get("state_path") or ""),
        "events_path": str(events_path_value),
        "digest_only_count": digest_only_count,
        "latest_event_id": latest.get("event_id") or latest.get("input_digest") or "",
        "reason": "current session has repeated digest-only user inputs and no semantic intent delta",
    }

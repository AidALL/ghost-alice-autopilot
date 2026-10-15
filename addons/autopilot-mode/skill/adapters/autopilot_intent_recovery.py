#!/usr/bin/env python3
"""Session-intent admission and recovery predicates for Ghost-ALICE Autopilot.

Dependencies: Python 3.11+, sibling adapters and Core's native completion API.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Mapping


MET_CRITERION_STATUSES = frozenset({"met", "done", "satisfied", "pass", "passed", "complete", "completed", "resolved", "closed"})


def _core_native_assessor(core):
    """Load Core's authority boundary, without assembling Core policy inputs."""
    from autopilot_work_items import _load_core_shared_module
    module = _load_core_shared_module(core, "native_completion.py", "ap_core_native_completion")
    if module is None:
        raise ImportError("Core native completion assessment unavailable")
    return module.assess_native_completion


def _input_anchor(value):
    """Core None and an empty ledger string both mean no admitted input."""
    return None if value is None or (type(value) is str and value == '') else value


def _continuation_feedback_shape_valid(route):
    """Check the authenticated optional Core projection, without judging necessity."""
    if 'continuation_feedback' not in route:
        return True
    feedback = route['continuation_feedback']
    if (not isinstance(feedback, dict)
            or set(feedback) != {'schema_version', 'control', 'required_unprocessed'}
            or feedback['schema_version'] != 'continuation-feedback.v1'
            or not isinstance(feedback['control'], str) or feedback['control'] not in {'active', 'human-stop'}
            or not isinstance(feedback['required_unprocessed'], list)):
        return False
    selected = {(c['work_unit_id'], c['contract_revision'], c['criterion_id'])
        for c in route['selected_criteria']}
    seen = set()
    for row in feedback['required_unprocessed']:
        if (not isinstance(row, dict)
                or set(row) != {'criterion_refs', 'reason', 'next_information', 'source_refs', 'prerequisite'}
                or not isinstance(row['prerequisite'], str) or row['prerequisite'] not in {'ready', 'missing'}
                or not all(isinstance(row[k], str) and row[k].strip() for k in ('reason', 'next_information'))
                or not isinstance(row['criterion_refs'], list) or not row['criterion_refs']
                or not isinstance(row['source_refs'], list)):
            return False
        for ref in row['criterion_refs']:
            if (not isinstance(ref, dict)
                    or set(ref) != {'work_unit_id', 'contract_revision', 'criterion_id'}
                    or type(ref['contract_revision']) is not int or ref['contract_revision'] < 1
                    or not all(isinstance(ref[k], str) and ref[k].strip() for k in ('work_unit_id', 'criterion_id'))):
                return False
            key = (ref['work_unit_id'], ref['contract_revision'], ref['criterion_id'])
            if key not in selected or key in seen:
                return False
            seen.add(key)
        source_selectors = set()
        for ref in row['source_refs']:
            if (not isinstance(ref, dict) or set(ref) != {'candidate_reference', 'source_id', 'source'}
                    or not all(isinstance(ref[k], str) and ref[k].strip() for k in ('candidate_reference', 'source_id'))
                    or (ref['candidate_reference'], ref['source_id']) in source_selectors or not isinstance(ref['source'], dict)):
                return False
            source_selectors.add((ref['candidate_reference'], ref['source_id']))
            original = ref['source']
            storage = original.get('storage')
            fields = ({'storage', 'locator', 'digest', 'byte_length', 'appendable', 'source_id'}
                if storage == 'source-reference.v1' else
                {'storage', 'intent_root', 'owner', 'candidate_reference', 'source_id', 'locator', 'digest'}
                if storage == 'core-source-reference.v1' else None)
            if (fields is None or set(original) != fields or original.get('source_id') != ref['source_id']
                    or not all(isinstance(original.get(k), str) and original[k].strip()
                        for k in ('source_id', 'locator', 'digest'))):
                return False
            if storage == 'source-reference.v1':
                if (not Path(original['locator']).is_absolute() or type(original['byte_length']) is not int
                        or original['byte_length'] < 0 or type(original['appendable']) is not bool):
                    return False
            elif (original['owner'] != {'platform': route['platform'], 'session_id': route['session_id']}
                    or not isinstance(original['intent_root'], str) or not Path(original['intent_root']).is_absolute()
                    or not isinstance(original['candidate_reference'], str) or not original['candidate_reference'].strip()):
                return False
    return True


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
            or ("verification_complexity" in surface
                and surface["verification_complexity"] not in ("level-1", "level-2", "level-3"))
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
                or _input_anchor(assessment.get("input_event_id")) != _input_anchor(state.get("latest_input_event_id"))
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
    if _input_anchor(assessment.get('input_event_id')) is None:
        if assessment.get('context_status') != 'available':
            return dict(result, status='recording-unavailable', reason='core-authority-unavailable',
                detail=assessment.get('context_issue'))
        # This is a read-only Core authority result, not actor inference or a
        # failed recording attempt. No selected execution can be admitted yet.
        return dict(result, status='recording-unavailable', reason='input-not-admitted',
            intake_status=state.get('intake_status'))
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

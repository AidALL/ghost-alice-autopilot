#!/usr/bin/env python3
"""Work-item validation and transition helpers for Ghost-ALICE Autopilot.

Dependencies: Python 3.11+, sibling storage helpers and Core's proof/ledger APIs.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping


ALLOWED_STATUSES = frozenset({
    "ready",
    "running",
    "completed",
    "reopened",
    "blocked",
    "stopped",
    "not_applicable",
})
TERMINAL_STATUSES = frozenset({"completed", "not_applicable"})
DEPENDENCY_SATISFIED_STATUSES = TERMINAL_STATUSES
READY_QUEUE_STATUSES = frozenset({"ready", "reopened"})
ALLOWED_DECISIONS = frozenset({
    "continue_next",
    "retry_same_unit",
    "reopen_micro",
    "reopen_meso",
    "reopen_macro",
    "reopen_meta",
    "park",
    "ask_user_meta",
    "stop",
})
CONDUCT_PLAN_SCHEMA = "autopilot-conduct-plan.v2"
COMPLETION_CHECK_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
APPROVAL_DECISIONS = frozenset({"go", "approve", "approved"})
_CORE_POLICY_SOURCE = ContextVar('autopilot_core_policy_source', default=None)


class AutopilotStateError(ValueError):
    """Raised when Ghost-ALICE Autopilot work-item state is invalid."""


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutopilotStateError(f"{field} must be a non-empty string")
    return value


def _validate_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise AutopilotStateError(f"{field} must be a string array")
    return list(value)


@contextmanager
def _bind_core_policy_source(source):
    """Keep delegated Core policy on the caller's trusted source for this call."""
    if source is None:
        yield  # Legacy standalone calls retain ambient lookup; nested calls inherit.
        return
    token = _CORE_POLICY_SOURCE.set(dict(source))
    try:
        yield
    finally:
        _CORE_POLICY_SOURCE.reset(token)


def _with_core_policy_source(function):
    """Bind a source-taking service entrypoint without changing its signature."""
    signature = inspect.signature(function)
    source_parameter = 'source' if 'source' in signature.parameters else 'env'

    @wraps(function)
    def bound(*args, **kwargs):
        source = signature.bind(*args, **kwargs).arguments.get(source_parameter)
        with _bind_core_policy_source(source):
            return function(*args, **kwargs)
    return bound


def _load_core_shared_module(core, filename: str, module_name: str):
    """Load policy and its imports from the selected ledger's installation."""
    shared = str(Path(core.__file__).resolve().parents[2] / '_shared')
    path = Path(shared) / filename
    sys.path.insert(0, shared)
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (OSError, ImportError):
        return None
    finally:
        sys.path.remove(shared)


def _load_core_policy_module(filename: str, module_name: str):
    """Resolve Core policy through the same provider as the selected ledger."""
    source = _CORE_POLICY_SOURCE.get()
    if source is not None and 'GHOST_ALICE_RUNTIME_SHARED_DIR' not in source:
        root = source.get('GHOST_ALICE_CORE_ROOT') or source.get('GHOST_ALICE_SKILLS_DIR')
        if not isinstance(root, str) or not Path(root).is_absolute():
            return None
        source = dict(source, GHOST_ALICE_CORE_ROOT=root)
    core = _load_core_ledger_module(Path(__file__), source)
    return _load_core_shared_module(core, filename, module_name) if core is not None else None


def _parse_completion_evidence(evidence_text: str, *, allow_partial: bool = False) -> dict[str, Any]:
    """Delegate supported proof coverage to the selected managed Core owner."""
    parser = _load_core_policy_module('completion_check_validator.py', 'ghost_alice_original_completion_parser')
    if parser is None:
        raise AutopilotStateError("Core original-evidence parser is unavailable")
    try:
        return parser.parse_original_completion_evidence(evidence_text, allow_partial=allow_partial)
    except (AttributeError, ValueError) as exc:
        raise AutopilotStateError(str(exc)) from exc


def _extract_top_level_section(text: str, field_name: str) -> str:
    """Keep existing archive callers on Core's canonical field extraction."""
    parser = _load_core_policy_module('completion_check_validator.py', 'ghost_alice_original_completion_parser')
    if parser is None:
        raise AutopilotStateError("Core original-evidence parser is unavailable")
    return parser.extract_top_level_field_section(text, field_name)


def _validate_completion(value: Any, item_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AutopilotStateError(f"work item {item_id!r} completion must be an object")
    completion = dict(value)
    completion.setdefault("state", "not_started")
    completion.setdefault("verdict", None)
    completion.setdefault("evidence", [])
    completion.setdefault("completion_check_digest", None)
    completion.setdefault("reopen_target", None)
    if not isinstance(completion["state"], str) or not completion["state"]:
        raise AutopilotStateError(f"work item {item_id!r} completion.state must be a non-empty string")
    if completion["verdict"] is not None and completion["verdict"] not in {"pass", "fail"}:
        raise AutopilotStateError(f"work item {item_id!r} completion.verdict must be pass, fail, or null")
    _validate_string_list(completion["evidence"], f"work item {item_id!r} completion.evidence")
    if completion["completion_check_digest"] is not None:
        _require_string(completion["completion_check_digest"], f"work item {item_id!r} completion_check_digest")
    if completion["reopen_target"] is not None:
        _require_string(completion["reopen_target"], f"work item {item_id!r} reopen_target")
    return completion


def _validate_continue_next_evidence(
    completion_check_digest: str | None,
    evidence: list[str] | None,
    *, allow_partial: bool = False,
) -> list[str]:
    if not isinstance(completion_check_digest, str) or not COMPLETION_CHECK_DIGEST_PATTERN.fullmatch(
        completion_check_digest
    ):
        raise AutopilotStateError("continue_next requires a sha256 completion-check digest")
    validated = _validate_string_list(evidence, "continue_next evidence")
    evidence_text = "\n".join(validated)
    marker = "[partial-check]" if allow_partial and "[partial-check]" in evidence_text else "[completion-check]"
    if marker not in evidence_text or "claim-evidence-map" not in evidence_text:
        raise AutopilotStateError("continue_next evidence must include a [completion-check] claim-evidence-map")
    _parse_completion_evidence(evidence_text, allow_partial=allow_partial)
    return validated


def _validate_non_continuation_evidence(decision: str, evidence: list[str] | None) -> list[str]:
    validated = _validate_string_list(evidence, f"{decision} evidence")
    if not validated:
        raise AutopilotStateError("non-continuation consistency decisions require evidence")
    return validated


def validate_work_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise AutopilotStateError("work item must be an object")
        item = dict(raw)
        item_id = _require_string(item.get("id"), "work item id")
        if item_id in seen:
            raise AutopilotStateError(f"duplicate work item id {item_id!r}")
        seen.add(item_id)
        status = _require_string(item.get("status"), f"work item {item_id!r} status")
        if status not in ALLOWED_STATUSES:
            raise AutopilotStateError(f"work item {item_id!r} status {status!r} is not allowed")
        item["depends_on"] = _validate_string_list(item.get("depends_on", []), f"work item {item_id!r} depends_on")
        item["acceptance_criteria"] = _validate_string_list(
            item.get("acceptance_criteria", []),
            f"work item {item_id!r} acceptance_criteria",
        )
        item["allowed_surface"] = _validate_string_list(
            item.get("allowed_surface", []),
            f"work item {item_id!r} allowed_surface",
        )
        item["focus_layer"] = _require_string(item.get("focus_layer"), f"work item {item_id!r} focus_layer")
        item["prompt"] = _require_string(item.get("prompt"), f"work item {item_id!r} prompt")
        attempt = item.get("attempt", 0)
        if not isinstance(attempt, int) or attempt < 0:
            raise AutopilotStateError(f"work item {item_id!r} attempt must be a non-negative integer")
        item["attempt"] = attempt
        item["completion"] = _validate_completion(item.get("completion", {}), item_id)
        validated.append(item)

    known = {item["id"] for item in validated}
    for item in validated:
        unknown = [dep for dep in item["depends_on"] if dep not in known]
        if unknown:
            raise AutopilotStateError(f"work item {item['id']!r} depends on unknown items {unknown}")
    return validated


def read_work_items(path: str | Path) -> list[dict[str, Any]]:
    from autopilot_storage import read
    return validate_work_items(read(Path(path)))


def write_work_items(path: str | Path, items: Iterable[dict[str, Any]]) -> None:
    state_path = Path(path)
    validated = validate_work_items(items)
    from autopilot_storage import write
    if write(state_path, validated):
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.",
        suffix=".tmp",
        dir=state_path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for item in validated:
                out.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(tmp_path, state_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def derive_ready_queue(items: Iterable[dict[str, Any]]) -> list[str]:
    validated = validate_work_items(items)
    by_id = {item["id"]: item for item in validated}
    ready: list[str] = []
    for item in validated:
        if item["status"] not in READY_QUEUE_STATUSES:
            continue
        if all(by_id[dep]["status"] in DEPENDENCY_SATISFIED_STATUSES for dep in item["depends_on"]):
            ready.append(item["id"])
    return ready


def _find_item(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    for item in items:
        if item["id"] == item_id:
            return item
    raise AutopilotStateError(f"unknown work item id {item_id!r}")


def allowed_source_statuses_for_consistency_decision(decision: str) -> frozenset[str]:
    """Return work-item statuses from which ``decision`` may be applied."""

    if decision not in ALLOWED_DECISIONS:
        return frozenset()
    if decision == "continue_next":
        return frozenset({"running", "ready", "reopened"})
    return frozenset({"running"})


def apply_consistency_decision(
    items: Iterable[dict[str, Any]],
    item_id: str,
    decision: str,
    *,
    completion_check_digest: str | None = None,
    verdict: str | None = None,
    evidence: list[str] | None = None,
) -> list[dict[str, Any]]:
    if decision not in ALLOWED_DECISIONS:
        raise AutopilotStateError(f"unknown consistency decision {decision!r}")
    updated = validate_work_items(copy.deepcopy(list(items)))
    item = _find_item(updated, item_id)
    allowed_source_statuses = allowed_source_statuses_for_consistency_decision(decision)
    if item["status"] not in allowed_source_statuses:
        raise AutopilotStateError(
            f"consistency decision state transition {decision!r} requires "
            f"{', '.join(sorted(allowed_source_statuses))} work item {item_id!r}; "
            f"found {item['status']!r}"
        )

    if decision == "continue_next":
        if verdict != "pass":
            raise AutopilotStateError("continue_next requires passing completion-check evidence")
        validated_evidence = _validate_continue_next_evidence(completion_check_digest, evidence)
        item["status"] = "completed"
        item["completion"].update({
            "state": "completed",
            "verdict": verdict,
            "evidence": validated_evidence,
            "completion_check_digest": completion_check_digest,
            "reopen_target": None,
        })
    elif decision == "retry_same_unit":
        validated_evidence = _validate_non_continuation_evidence(decision, evidence)
        item["status"] = "ready"
        item["attempt"] += 1
        item["completion"].update({
            "state": "retry",
            "verdict": None,
            "evidence": validated_evidence,
            "completion_check_digest": completion_check_digest,
            "reopen_target": None,
        })
    elif decision.startswith("reopen_"):
        validated_evidence = _validate_non_continuation_evidence(decision, evidence)
        target = decision.removeprefix("reopen_")
        item["status"] = "reopened"
        item["completion"].update({
            "state": "reopened",
            "verdict": None,
            "evidence": validated_evidence,
            "completion_check_digest": completion_check_digest,
            "reopen_target": target,
        })
    elif decision == "park":
        validated_evidence = _validate_non_continuation_evidence(decision, evidence)
        item["status"] = "blocked"
        # Suspension changes eligibility, not the prior business observation.
        item["completion"].update({
            "state": "parked",
            "evidence": list(dict.fromkeys([*item["completion"]["evidence"], *validated_evidence])),
        })
    elif decision in {"ask_user_meta", "stop"}:
        validated_evidence = _validate_non_continuation_evidence(decision, evidence)
        item["status"] = "stopped"
        item["completion"].update({
            "state": decision,
            "verdict": None,
            "evidence": validated_evidence,
            "completion_check_digest": completion_check_digest,
            "reopen_target": "meta" if decision == "ask_user_meta" else None,
        })
    return validate_work_items(updated)


def _load_core_ledger_module(state_path: Path, source: Mapping[str, str] | None = None):
    """Resolve the Core ledger provider from the selected source.

    A native runtime selects its installed ledger. Without a runtime, an explicit
    source or inherited environment root selects the provider. Discovery applies
    only when neither is supplied; an unavailable selection is not replaced.
    """
    candidates: list[Path] = []
    if source is None:
        source = os.environ
    runtime = source.get("GHOST_ALICE_RUNTIME_SHARED_DIR")
    selected = str(source.get("GHOST_ALICE_CORE_ROOT") or os.environ.get("GHOST_ALICE_CORE_ROOT") or "")
    explicit = selected.strip()
    if "GHOST_ALICE_RUNTIME_SHARED_DIR" in source:
        if not isinstance(runtime, str) or not Path(runtime).is_absolute() or not Path(runtime).is_dir():
            return None
        candidates.append(Path(runtime).parent / "session-intent-analyzer/scripts/session_intent_ledger.py")
    elif selected:
        if not explicit or not Path(explicit).is_absolute():
            return None
        candidates.append(Path(explicit) / "session-intent-analyzer" / "scripts" / "session_intent_ledger.py")
    else:
        candidates.append(Path(__file__).resolve().parents[2] / "session-intent-analyzer" / "scripts" / "session_intent_ledger.py")
        for parent in Path(state_path).resolve().parents:
            candidates.append(parent / "session-intent-analyzer" / "scripts" / "session_intent_ledger.py")
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            spec = importlib.util.spec_from_file_location(
                "ghost_alice_session_intent_ledger", candidate
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception:
            continue
    return None


def materialize_met_criteria_from_continue_next(
    run: Mapping[str, Any],
    applied_decision: Mapping[str, Any],
    source: Mapping[str, str] | None = None,
    *, require_success: bool = False,
) -> list[str]:
    """Flip the satisfied admitted criteria to "met" after a validated continue_next.

    The Core ledger owns the write-only "met" invariant; the adapter
    only calls it, using the same validated completion-check digest required by
    continue_next. A session-bound adapter uses require_success
    before committing task completion. Diagnostic callers may request a skipped
    result; neither path rebinds old proof to a new input or criterion.
    """
    def reject(reason: str) -> list[str]:
        if require_success:
            raise AutopilotStateError(f"completion proof was not recorded by core: {reason}")
        return []

    approval = run.get("approval_evidence")
    if not isinstance(approval, Mapping):
        return reject("missing approval")
    session_intent = approval.get("session_intent")
    if not isinstance(session_intent, Mapping):
        return reject("missing session binding")
    material_path = Path(__file__).resolve().parents[1] / "scripts" / "autopilot_session_material.py"
    spec = importlib.util.spec_from_file_location("autopilot_session_material", material_path)
    material = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(material)
    try:
        material.validate_approval_generation(run, applied_decision)
    except ValueError as exc:
        return reject(str(exc))
    approved_criteria = session_intent.get("acceptance_criteria")
    if not isinstance(approved_criteria, list):
        return reject("missing approved criterion snapshots; inspect current input and reapprove work")
    snapshots = {row["id"]: row for row in approved_criteria
                 if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
    approved_input = session_intent.get("latest_input_event")
    expected_input = approved_input.get("event_id") if isinstance(approved_input, Mapping) else None
    state_path_raw = session_intent.get("state_path")
    if not isinstance(state_path_raw, str) or not state_path_raw:
        return reject("missing session state path")
    raw_digest = str(applied_decision.get("completion_check_digest") or "").strip()
    if not COMPLETION_CHECK_DIGEST_PATTERN.fullmatch(raw_digest):
        return reject("invalid completion digest")
    core_digest = raw_digest[len("sha256:"):] if raw_digest.startswith("sha256:") else raw_digest
    evidence = applied_decision.get("evidence")
    if not isinstance(evidence, list):
        return reject("missing completion evidence")
    try:
        validated_evidence = _validate_string_list(evidence, "continue_next evidence")
        parsed = _parse_completion_evidence("\n".join(validated_evidence))
    except AutopilotStateError as exc:
        return reject(str(exc))
    criterion_ids = parsed["criterion_ids"]
    state_path = Path(state_path_raw)
    if (session_intent.get("platform") != state_path.parent.parent.name
            or session_intent.get("session_id") != state_path.parent.name):
        return reject("session identity differs from target state path")
    ledger = _load_core_ledger_module(state_path, source)
    if ledger is None or not hasattr(ledger, "mark_acceptance_criterion_met"):
        return reject("compatible core met writer is unavailable")
    intent_root = state_path.parent.parent.parent
    platform = state_path.parent.parent.name
    session_id = state_path.parent.name
    from autopilot_storage import core_transaction
    connection = core_transaction(intent_root)
    if require_success and connection is None:
        return reject("bound completion requires the shared Ghost-ALICE Autopilot transaction")
    flipped: list[str] = []
    for criterion_id in criterion_ids:
        if criterion_id not in snapshots:
            if require_success:
                return reject(f"criterion {criterion_id!r} was not part of the approval snapshot")
            continue
        try:
            ledger.mark_acceptance_criterion_met(
                root=intent_root,
                platform=platform,
                session_id=session_id,
                criterion_id=criterion_id,
                completion_check_digest=core_digest,
                expected_input_event_id=expected_input,
                expected_criterion=dict(snapshots[criterion_id]),
                transaction=connection,
            )
            flipped.append(criterion_id)
        except Exception as exc:
            if require_success:
                return reject(f"criterion {criterion_id!r}: {exc}")
            continue
    return flipped


def rewrite_open_work_items(
    items: Iterable[dict[str, Any]],
    replacement_items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    current = validate_work_items(copy.deepcopy(list(items)))
    terminal = [item for item in current if item["status"] in TERMINAL_STATUSES]
    replacements = copy.deepcopy(list(replacement_items))
    return validate_work_items([*terminal, *replacements])


def _has_explicit_approval_evidence(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    decision = str(value.get("decision") or "").strip().lower()
    source = value.get("source")
    return decision in APPROVAL_DECISIONS and isinstance(source, str) and bool(source.strip())


def apply_conduct_plan_proposals(
    items: Iterable[dict[str, Any]],
    plan: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if plan.get("schema_version") != CONDUCT_PLAN_SCHEMA:
        raise AutopilotStateError(f"conduct plan schema_version must be {CONDUCT_PLAN_SCHEMA!r}")
    if plan.get("promotion_state") != "approved":
        raise AutopilotStateError("conduct plan promotion_state must be 'approved'")
    _require_string(plan.get("source_candidate_id"), "conduct plan source_candidate_id")
    evidence_digest = _require_string(plan.get("evidence_digest"), "conduct plan evidence_digest")
    if not COMPLETION_CHECK_DIGEST_PATTERN.fullmatch(evidence_digest):
        raise AutopilotStateError("conduct plan evidence_digest must be a sha256 digest")
    if not _has_explicit_approval_evidence(plan.get("approval_evidence")):
        raise AutopilotStateError("conduct plan approval_evidence must include an explicit approval decision and source")
    proposed = plan.get("proposed_queue_items")
    if not isinstance(proposed, list):
        raise AutopilotStateError("conduct plan proposed_queue_items must be an array")

    updated = validate_work_items(copy.deepcopy(list(items)))
    known_ids = {item["id"] for item in updated}
    new_items: list[dict[str, Any]] = []

    for index, raw_proposal in enumerate(proposed):
        if not isinstance(raw_proposal, dict):
            raise AutopilotStateError(f"conduct plan proposed_queue_items[{index}] must be an object")
        proposal = raw_proposal
        proposal_id = _require_string(proposal.get("id"), f"conduct proposal {index} id")
        if proposal.get("proposal_status") != "proposed":
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} proposal_status must be 'proposed'")
        if proposal.get("approval_required") is not True:
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} approval_required must be true")
        transition = proposal.get("approval_transition")
        if not isinstance(transition, dict):
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} approval_transition must be an object")
        if transition.get("copy_task_template") is not True:
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} must copy task_template on approval")
        if transition.get("status_on_approval") != "ready":
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} status_on_approval must be 'ready'")
        template = proposal.get("task_template")
        if not isinstance(template, dict):
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} task_template must be an object")

        item_id = _require_string(template.get("id"), f"conduct proposal {proposal_id!r} task_template id")
        if item_id in known_ids:
            continue
        item = copy.deepcopy(template)
        item["status"] = "ready"
        item.setdefault("depends_on", [])
        item.setdefault("attempt", 0)
        item.setdefault("completion", {})
        item["source_plan_schema"] = CONDUCT_PLAN_SCHEMA
        item["source_plan_source"] = plan.get("source")
        item["source_plan_candidate_id"] = plan.get("source_candidate_id")
        item["source_plan_evidence_digest"] = plan.get("evidence_digest")
        item["source_plan_approval_evidence"] = copy.deepcopy(plan.get("approval_evidence"))
        item["source_proposal_id"] = proposal_id
        item["source_recommendation_id"] = proposal.get("source_recommendation_id")
        if proposal.get("observer_agent_required") is True:
            item["observer_agent_required"] = True
            observer_contract = proposal.get("observer_contract")
            if observer_contract is not None and not isinstance(observer_contract, dict):
                raise AutopilotStateError(
                    f"conduct proposal {proposal_id!r} observer_contract must be an object when present"
                )
            if isinstance(observer_contract, dict):
                item["observer_contract"] = copy.deepcopy(observer_contract)
        new_items.append(item)
        known_ids.add(item_id)

    return validate_work_items([*updated, *new_items])


def materialize_met_criteria_v2(request, records, source, connection):
    """Apply only exact current-owner snapshots after v2 publication validation."""
    from autopilot_completion_contract import criterion_key
    binding = request['binding']
    root = Path(binding['intent_root'])
    if connection is None or any(source.get('GHOST_ALICE_'+k.upper()) != binding[k] for k in ('platform','session_id')):
        raise AutopilotStateError('v2 met writer requires current owner and shared authority transaction')
    authority = source.get('GHOST_ALICE_SESSION_INTENT_ROOT')
    from autopilot_storage import active
    store = active(request['target']['path'])
    if (not isinstance(authority,str) or not Path(authority).is_absolute() or Path(authority).resolve()!=root.resolve()
            or store is None or store.connection is not connection or store.root!=root.resolve()):
        raise AutopilotStateError('v2 met writer requires the exact current Ghost-ALICE Autopilot/Core authority transaction')
    ledger = _load_core_ledger_module(root / binding['platform'] / binding['session_id'] / 'intent-state.json',source)
    if ledger is None or not callable(getattr(ledger,'mark_acceptance_criterion_met',None)):
        raise AutopilotStateError('compatible Core met writer is unavailable')
    live = ledger.read_session_state(root=root,platform=binding['platform'],session_id=binding['session_id'],transaction=connection)
    snapshots = {criterion_key(c):c for c in request['contract']['criteria']}
    # The session retains other work and legacy rows; only this contract can be applied here.
    met = {criterion_key(c) for c in live['acceptance_criteria'] if c.get('status')=='met'
           and (c.get('work_unit_id'), c.get('contract_revision'), c.get('id')) in snapshots}
    for selected in request['selected_criteria']:
        key = (selected['work_unit_id'],selected['contract_revision'],selected['criterion_id'])
        snapshot = snapshots.get(key)
        if snapshot is None:
            raise AutopilotStateError('exact owned approved criterion snapshot is missing')
        if key in met:
            continue
        record = records[selected['evidence_reference']]
        ledger.mark_acceptance_criterion_met(root=root,platform=binding['platform'],session_id=binding['session_id'],
            criterion_id=selected['criterion_id'],completion_check_digest=record['proof_digest'].removeprefix('sha256:'),
            expected_input_event_id=binding['input_event_id'],expected_criterion=dict(snapshot),transaction=connection)

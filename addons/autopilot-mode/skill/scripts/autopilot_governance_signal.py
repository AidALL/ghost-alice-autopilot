#!/usr/bin/env python3
"""Build candidate-first autopilot governance signals.

This script intentionally separates diagnostic/candidate outputs from
adapter-consumable action files. The adapter still consumes only promoted
`consistency-decision.json` and approved `conduct-plan.json` files.

Dependencies: Python 3.11+ standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DECISION_CANDIDATE_SCHEMA = "autopilot-consistency-decision-candidate.v1"
CONDUCT_PLAN_CANDIDATE_SCHEMA = "autopilot-conduct-plan-candidate.v1"
CONDUCT_PLAN_SCHEMA = "autopilot-conduct-plan.v2"
ACTION_DECISION_SCHEMA = "autopilot-consistency-decision.v1"
OBSERVATION_SIGNAL_SCHEMA = "autopilot-observation-signal.v1"
DEFAULT_RETRY_ATTEMPT_CAP = 2
NON_PROMOTABLE_CANDIDATE_SOURCES = {"observation_signal"}
DEFAULT_PROMOTION_EVIDENCE = {
    "decision": "promote",
    "source": "autopilot_governance_signal.promote_candidate_to_action",
}
REOPEN_DECISION_BY_FOCUS = {
    "micro": "reopen_micro",
    "meso": "reopen_meso",
    "macro": "reopen_macro",
    "meta": "ask_user_meta",
}
REOPEN_VERDICT_MARKERS = {
    "reopen_focus",
    "request_verification",
}
STALE_CONFLICT_MARKERS = (
    "stale",
    "conflict",
    "conflicting",
    "outdated",
    "contradict",
)
VERIFICATION_MARKERS = (
    "verification",
    "verify",
    "evidence",
    "source-backed",
    "source backed",
    "unverified",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _short_digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()[:16]


def _read_json_object(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def _read_jsonl_objects(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        return []
    values: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            values.append(parsed)
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


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _non_empty_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _compact_signal_value(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return None


def _normalized_signal_value(value: Any) -> str:
    text = _compact_signal_value(value)
    if text is None:
        return ""
    return text.strip().lower()


def _verdict_requires_reopen(value: Any) -> bool:
    normalized = _normalized_signal_value(value).replace("-", "_").replace(" ", "_")
    return any(marker in normalized for marker in REOPEN_VERDICT_MARKERS)


def _wording_requires_reopen(governance_signal: Mapping[str, Any]) -> bool:
    haystack = " ".join(
        _normalized_signal_value(governance_signal.get(key))
        for key in ("verdict", "next_action", "loop_guard")
    )
    has_stale_or_conflict = any(marker in haystack for marker in STALE_CONFLICT_MARKERS)
    has_verification = any(marker in haystack for marker in VERIFICATION_MARKERS)
    return has_stale_or_conflict and has_verification


def _semantic_observation_requires_reopen(governance_signal: Mapping[str, Any]) -> bool:
    return (
        governance_signal.get("mismatch_detected") is True
        or _verdict_requires_reopen(governance_signal.get("verdict"))
        or _wording_requires_reopen(governance_signal)
    )


def _focus_decision_from_part(value: str) -> str | None:
    if value in REOPEN_DECISION_BY_FOCUS:
        return REOPEN_DECISION_BY_FOCUS[value]
    for focus, decision in REOPEN_DECISION_BY_FOCUS.items():
        if value.startswith(f"{focus}_") or value.startswith(f"{focus}-") or value.startswith(f"{focus} "):
            return decision
    return None


def _reopen_decision_for_focus(value: Any, default: str) -> str:
    if isinstance(value, str):
        value = value.strip().lower()
        if ":" in value:
            value = value.split(":", 1)[0].strip()
        value = value.replace("=>", "->").replace("→", "->").replace("⇒", "->")
        parts = [value]
        if "->" in value:
            parts = value.split("->")
        elif "_to_" in value:
            parts = value.split("_to_")
        elif "/" in value:
            parts = value.split("/")
        for part in reversed([part.strip() for part in parts]):
            decision = _focus_decision_from_part(part)
            if decision is not None:
                return decision
        if "boundary" in value or "verification" in value:
            return "reopen_macro"
        return REOPEN_DECISION_BY_FOCUS.get(value, default)
    return default


def _candidate(
    *,
    work_item_id: str,
    decision: str,
    source: str,
    evidence: Sequence[str],
    state_payload: Mapping[str, Any],
    source_signal_id: str | None = None,
    max_retry_attempts: int = DEFAULT_RETRY_ATTEMPT_CAP,
) -> dict[str, Any] | None:
    evidence_items = [item for item in evidence if isinstance(item, str) and item.strip()]
    if not evidence_items:
        return None
    state_hash = _digest(state_payload)
    evidence_digest = _digest(evidence_items)
    decision_key = _digest({
        "work_item_id": work_item_id,
        "decision": decision,
        "source": source,
        "source_signal_id": source_signal_id,
        "evidence_digest": evidence_digest,
    })
    loop_key = _digest({
        "work_item_id": work_item_id,
        "decision": decision,
        "state_hash": state_hash,
    })
    candidate_id = f"candidate-{_short_digest([work_item_id, decision, evidence_digest, state_hash])}"
    return {
        "schema_version": DECISION_CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "work_item_id": work_item_id,
        "decision": decision,
        "source": source,
        "source_signal_id": source_signal_id,
        "evidence": evidence_items,
        "evidence_digest": evidence_digest,
        "state_hash": state_hash,
        "decision_key": decision_key,
        "loop_guard": {
            "loop_key": loop_key,
            "max_retry_attempts": max_retry_attempts,
        },
        "promotion_state": "candidate",
        "action_file_allowed": False,
        "created_at": _utc_now(),
    }


def _completion_decision(
    *,
    work_item_id: str,
    completion_validation: Mapping[str, Any],
    state_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if completion_validation.get("valid") is not False:
        return None
    errors = _non_empty_strings(completion_validation.get("errors"))
    evidence = [f"completion_validation:{error}" for error in errors]
    if not evidence:
        evidence = ["completion_validation:invalid"]
    return _candidate(
        work_item_id=work_item_id,
        decision="retry_same_unit",
        source="completion_validation",
        evidence=evidence,
        state_payload=state_payload,
        source_signal_id="completion-validation",
    )


def _routing_decision(
    *,
    work_item_id: str,
    routing_surface: Mapping[str, Any],
    state_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    intent_relation = routing_surface.get("intent_relation")
    if intent_relation not in {"correction", "changed", "ambiguous"}:
        return None
    focus_layer = routing_surface.get("focus_layer")
    decision = _reopen_decision_for_focus(focus_layer, "reopen_macro")
    reason = routing_surface.get("reason")
    evidence = [f"routing_surface:{intent_relation}"]
    if isinstance(reason, str) and reason.strip():
        evidence.append(f"routing_surface_reason:{reason.strip()}")
    return _candidate(
        work_item_id=work_item_id,
        decision=decision,
        source="routing_surface",
        evidence=evidence,
        state_payload=state_payload,
        source_signal_id=str(intent_relation),
    )


def _observation_decision(
    *,
    work_item_id: str,
    governance_signal: Mapping[str, Any],
    state_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if governance_signal.get("schema_version") != OBSERVATION_SIGNAL_SCHEMA:
        return None
    classification = governance_signal.get("classification")
    inference_status = governance_signal.get("inference_status")
    if inference_status == "auth-failed" or classification == "readiness-blocker":
        return None
    focus_layer = governance_signal.get("focus_layer")
    if classification == "tool-loop-timeout":
        decision = _reopen_decision_for_focus(focus_layer, "reopen_meso")
    elif classification in {"hook-incomplete", "timeout", "inference-failed"}:
        decision = _reopen_decision_for_focus(focus_layer, "reopen_meso")
    elif classification in {"schema-mismatch", "semantic-invalid"}:
        decision = _reopen_decision_for_focus(focus_layer, "reopen_micro")
    elif classification == "semantic-observation" and _semantic_observation_requires_reopen(governance_signal):
        decision = _reopen_decision_for_focus(focus_layer, "reopen_meso")
    else:
        return None

    evidence = [f"observation_signal:{classification}"]
    if isinstance(focus_layer, str) and focus_layer.strip():
        evidence.append(f"observation_focus_layer:{focus_layer.strip()}")
    runtime = governance_signal.get("runtime")
    if isinstance(runtime, str) and runtime.strip():
        evidence.append(f"observation_runtime:{runtime.strip()}")
    scenario_id = governance_signal.get("scenario_id")
    if isinstance(scenario_id, str) and scenario_id.strip():
        evidence.append(f"observation_scenario:{scenario_id.strip()}")
    verdict = _compact_signal_value(governance_signal.get("verdict"))
    if verdict is not None:
        evidence.append(f"observation_verdict:{verdict}")
    semantic_status = governance_signal.get("semantic_status")
    if isinstance(semantic_status, str) and semantic_status.strip():
        evidence.append(f"observation_semantic_status:{semantic_status.strip()}")
    hook_status = governance_signal.get("hook_status")
    if isinstance(hook_status, str) and hook_status.strip():
        evidence.append(f"observation_hook_status:{hook_status.strip()}")
    agent_activity = governance_signal.get("agent_activity")
    if isinstance(agent_activity, str) and agent_activity.strip():
        evidence.append(f"observation_agent_activity:{agent_activity.strip()}")
    next_action = _compact_signal_value(governance_signal.get("next_action"))
    if next_action is not None:
        evidence.append(f"observation_next_action:{next_action}")
    loop_guard = _compact_signal_value(governance_signal.get("loop_guard"))
    if loop_guard is not None:
        evidence.append(f"observation_loop_guard:{loop_guard}")
    return _candidate(
        work_item_id=work_item_id,
        decision=decision,
        source="observation_signal",
        evidence=evidence,
        state_payload=state_payload,
        source_signal_id=str(governance_signal.get("signal_id") or classification),
    )


def _conduct_decision(
    *,
    work_item_id: str,
    intent_state: Mapping[str, Any],
    state_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    for entry in _as_list(intent_state.get("conduct_feedback")):
        feedback = _as_mapping(entry)
        if feedback.get("status") not in {None, "open", "active"}:
            continue
        occurrence_count = feedback.get("occurrence_count", 1)
        if not isinstance(occurrence_count, int) or isinstance(occurrence_count, bool):
            occurrence_count = 1
        feedback_id = feedback.get("id") if isinstance(feedback.get("id"), str) else "conduct-feedback"
        summary = feedback.get("summary") or feedback.get("corrective_rule") or "conduct feedback is open"
        evidence = [f"conduct_feedback:{feedback_id}"]
        if isinstance(summary, str) and summary.strip():
            evidence.append(f"conduct_feedback_summary:{summary.strip()}")
        if occurrence_count > 1:
            evidence.append(f"conduct_feedback_occurrence_count:{occurrence_count}")
        return _candidate(
            work_item_id=work_item_id,
            decision="reopen_macro",
            source="session_intent.conduct_feedback",
            evidence=evidence,
            state_payload=state_payload,
            source_signal_id=feedback_id,
        )
    return None


def decision_candidate_from_governance(
    *,
    work_item_id: str,
    intent_state: Mapping[str, Any] | None = None,
    routing_surface: Mapping[str, Any] | None = None,
    completion_validation: Mapping[str, Any] | None = None,
    governance_signal: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a diagnostic decision candidate, never an adapter action file."""

    state_payload = {
        "intent_state": dict(intent_state or {}),
        "routing_surface": dict(routing_surface or {}),
        "completion_validation": dict(completion_validation or {}),
        "governance_signal": dict(governance_signal or {}),
    }
    if completion_validation:
        completion_candidate = _completion_decision(
            work_item_id=work_item_id,
            completion_validation=completion_validation,
            state_payload=state_payload,
        )
        if completion_candidate is not None:
            return completion_candidate
    if routing_surface:
        routing_candidate = _routing_decision(
            work_item_id=work_item_id,
            routing_surface=routing_surface,
            state_payload=state_payload,
        )
        if routing_candidate is not None:
            return routing_candidate
    if governance_signal:
        observation_candidate = _observation_decision(
            work_item_id=work_item_id,
            governance_signal=governance_signal,
            state_payload=state_payload,
        )
        if observation_candidate is not None:
            return observation_candidate
    if intent_state:
        conduct_candidate = _conduct_decision(
            work_item_id=work_item_id,
            intent_state=intent_state,
            state_payload=state_payload,
        )
        if conduct_candidate is not None:
            return conduct_candidate
    return None


def _is_promotable_candidate(candidate: Mapping[str, Any]) -> bool:
    return (
        candidate.get("schema_version") == DECISION_CANDIDATE_SCHEMA
        and candidate.get("promotion_state") == "candidate"
        and candidate.get("action_file_allowed") is False
        and candidate.get("source") not in NON_PROMOTABLE_CANDIDATE_SOURCES
        and isinstance(candidate.get("work_item_id"), str)
        and isinstance(candidate.get("decision"), str)
        and bool(_non_empty_strings(candidate.get("evidence")))
    )


def _action_from_candidate(
    candidate: Mapping[str, Any],
    *,
    decision: str,
    evidence: Sequence[str],
    promotion_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": ACTION_DECISION_SCHEMA,
        "decision_id": f"decision-{_short_digest([candidate.get('candidate_id'), decision, evidence])}",
        "work_item_id": candidate["work_item_id"],
        "decision": decision,
        "promotion_state": "promoted",
        "promotion_evidence": dict(promotion_evidence or DEFAULT_PROMOTION_EVIDENCE),
        "evidence": list(evidence),
        "source": "autopilot_governance_signal.promoted_candidate",
        "candidate_id": candidate.get("candidate_id"),
        "governance_signal_digest": candidate.get("evidence_digest"),
        "decision_key": candidate.get("decision_key"),
        "state_hash": candidate.get("state_hash"),
        "loop_key": _as_mapping(candidate.get("loop_guard")).get("loop_key"),
    }
    return {key: value for key, value in payload.items() if value is not None}


def promote_candidate_to_action(
    candidate: Mapping[str, Any] | None,
    *,
    prior_loop_keys: Iterable[str] | None = None,
    current_attempt: int = 0,
    promotion_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Promote an evidence-backed candidate into an adapter-consumable action."""

    if candidate is None or not _is_promotable_candidate(candidate):
        return None
    evidence = _non_empty_strings(candidate.get("evidence"))
    loop_guard = _as_mapping(candidate.get("loop_guard"))
    loop_key = loop_guard.get("loop_key")
    prior = set(prior_loop_keys or [])
    if isinstance(loop_key, str) and loop_key in prior:
        return _action_from_candidate(
            candidate,
            decision="ask_user_meta",
            evidence=[*evidence, "loop-guard: repeated decision/state"],
            promotion_evidence=promotion_evidence,
        )

    decision = candidate["decision"]
    max_retry_attempts = loop_guard.get("max_retry_attempts", DEFAULT_RETRY_ATTEMPT_CAP)
    if not isinstance(max_retry_attempts, int) or isinstance(max_retry_attempts, bool):
        max_retry_attempts = DEFAULT_RETRY_ATTEMPT_CAP
    if decision == "retry_same_unit" and current_attempt >= max_retry_attempts:
        return _action_from_candidate(
            candidate,
            decision="ask_user_meta",
            evidence=[*evidence, f"loop-guard: retry attempt cap {max_retry_attempts} reached"],
            promotion_evidence=promotion_evidence,
        )

    return _action_from_candidate(candidate, decision=decision, evidence=evidence, promotion_evidence=promotion_evidence)


def _promotion_context_from_run_dir(run_dir: str | Path, work_item_id: str) -> dict[str, Any]:
    root = Path(run_dir)
    current_attempt = 0
    for item in _read_jsonl_objects(root / "tasks.jsonl"):
        if item.get("id") == work_item_id and isinstance(item.get("attempt"), int):
            current_attempt = item["attempt"]
            break
    prior_loop_keys: list[str] = []
    for event in _read_jsonl_objects(root / "events.jsonl"):
        if event.get("event") != "consistency_decision_applied":
            continue
        if event.get("work_item_id") != work_item_id:
            continue
        loop_key = event.get("loop_key")
        if isinstance(loop_key, str) and loop_key:
            prior_loop_keys.append(loop_key)
    return {"current_attempt": current_attempt, "prior_loop_keys": prior_loop_keys}


def _safe_id(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "conduct-feedback"


def conduct_plan_candidate_from_governance(
    *,
    intent_state: Mapping[str, Any],
    current_work_item_id: str,
    plan_path: str,
) -> dict[str, Any] | None:
    """Return a conduct-plan candidate; it is not an approved conduct plan."""

    selected_feedback: dict[str, Any] | None = None
    for entry in _as_list(intent_state.get("conduct_feedback")):
        feedback = _as_mapping(entry)
        occurrence_count = feedback.get("occurrence_count", 1)
        if not isinstance(occurrence_count, int) or isinstance(occurrence_count, bool):
            occurrence_count = 1
        if feedback.get("status") in {None, "open", "active"} and occurrence_count >= 2:
            selected_feedback = feedback
            break
    if selected_feedback is None:
        return None

    feedback_id = _safe_id(str(selected_feedback.get("id") or "conduct-feedback"))
    source_recommendation_id = f"conduct-feedback-{feedback_id}"
    evidence = [
        f"conduct_feedback:{feedback_id}",
        f"current_work_item:{current_work_item_id}",
        f"plan_path:{plan_path}",
    ]
    plan = {
        "schema_version": CONDUCT_PLAN_SCHEMA,
        "source": "autopilot_governance_signal.conduct_plan_candidate",
        "proposed_queue_items": [
            {
                "id": f"proposal-{source_recommendation_id}",
                "proposal_status": "proposed",
                "approval_required": True,
                "approval_transition": {
                    "status_on_approval": "ready",
                    "copy_task_template": True,
                },
                "task_template": {
                    "id": f"conduct-{feedback_id}",
                    "depends_on": [],
                    "focus_layer": "meta",
                    "prompt": "Investigate the repeated conduct mismatch, propose a bounded fix, and preserve the candidate/action-file boundary.",
                    "acceptance_criteria": [
                        "Bind the conduct mismatch to session-intent or governance evidence.",
                        "Keep governance outputs as candidates until promotion or approval.",
                        "Attach a read-only observer before implementation.",
                    ],
                    "allowed_surface": [plan_path],
                },
                "observer_agent_required": True,
                "observer_contract": {
                    "mode": "read_only",
                    "purpose": "watch main-process logical consistency, focus shifts, and loop-risk signals",
                    "prohibited_actions": [
                        "modify files",
                        "write adapter-consumable action files",
                        "mark proposed tasks ready",
                    ],
                },
                "source": "conduct_feedback",
                "source_recommendation_id": source_recommendation_id,
            },
        ],
    }
    return {
        "schema_version": CONDUCT_PLAN_CANDIDATE_SCHEMA,
        "candidate_id": f"conduct-plan-candidate-{_short_digest([source_recommendation_id, plan_path])}",
        "promotion_state": "candidate",
        "action_file_allowed": False,
        "current_work_item_id": current_work_item_id,
        "evidence": evidence,
        "evidence_digest": _digest(evidence),
        "state_hash": _digest({"intent_state": intent_state, "current_work_item_id": current_work_item_id}),
        "conduct_plan": plan,
        "created_at": _utc_now(),
    }


def promote_conduct_plan_candidate(
    candidate: Mapping[str, Any] | None,
    *,
    approval_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(candidate, Mapping):
        return None
    if candidate.get("schema_version") != CONDUCT_PLAN_CANDIDATE_SCHEMA:
        return None
    if candidate.get("promotion_state") != "candidate" or candidate.get("action_file_allowed") is not False:
        return None
    if not isinstance(approval_evidence, Mapping) or not approval_evidence:
        return None
    plan = _as_mapping(candidate.get("conduct_plan"))
    if plan.get("schema_version") != CONDUCT_PLAN_SCHEMA:
        return None
    approved = dict(plan)
    approved["promotion_state"] = "approved"
    approved["source_candidate_id"] = candidate.get("candidate_id")
    approved["evidence_digest"] = candidate.get("evidence_digest")
    approved["approval_evidence"] = dict(approval_evidence)
    approved["approved_at"] = _utc_now()
    return approved


def _cmd_decision_candidate(args: argparse.Namespace) -> int:
    candidate = decision_candidate_from_governance(
        work_item_id=args.work_item_id,
        intent_state=_read_json_object(args.intent_state),
        routing_surface=_read_json_object(args.routing_surface),
        completion_validation=_read_json_object(args.completion_validation),
        governance_signal=_read_json_object(args.governance_signal),
    )
    if candidate is None:
        return 3
    _write_json_atomic(args.out, candidate)
    return 0


def _cmd_promote_decision(args: argparse.Namespace) -> int:
    candidate = _read_json_object(args.candidate)
    context = {"current_attempt": 0, "prior_loop_keys": []}
    if args.run_dir:
        work_item_id = candidate.get("work_item_id")
        if isinstance(work_item_id, str):
            context = _promotion_context_from_run_dir(args.run_dir, work_item_id)
    current_attempt = args.current_attempt
    if current_attempt is None:
        current_attempt = int(context["current_attempt"])
    prior_loop_keys = [*context["prior_loop_keys"], *args.prior_loop_key]
    action = promote_candidate_to_action(
        candidate,
        prior_loop_keys=prior_loop_keys,
        current_attempt=current_attempt,
    )
    if action is None:
        return 3
    _write_json_atomic(args.out, action)
    return 0


def _cmd_conduct_plan_candidate(args: argparse.Namespace) -> int:
    candidate = conduct_plan_candidate_from_governance(
        intent_state=_read_json_object(args.intent_state),
        current_work_item_id=args.current_work_item_id,
        plan_path=args.plan_path,
    )
    if candidate is None:
        return 3
    _write_json_atomic(args.out, candidate)
    return 0


def _cmd_promote_conduct_plan(args: argparse.Namespace) -> int:
    approval_evidence = json.loads(args.approval_evidence_json)
    if not isinstance(approval_evidence, dict):
        raise ValueError("--approval-evidence-json must be a JSON object")
    approved = promote_conduct_plan_candidate(
        _read_json_object(args.candidate),
        approval_evidence=approval_evidence,
    )
    if approved is None:
        return 3
    _write_json_atomic(args.out, approved)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    decision_candidate = subcommands.add_parser("decision-candidate")
    decision_candidate.add_argument("--work-item-id", required=True)
    decision_candidate.add_argument("--intent-state")
    decision_candidate.add_argument("--routing-surface")
    decision_candidate.add_argument("--completion-validation")
    decision_candidate.add_argument("--governance-signal")
    decision_candidate.add_argument("--out", required=True)
    decision_candidate.set_defaults(func=_cmd_decision_candidate)

    promote_decision = subcommands.add_parser("promote-decision")
    promote_decision.add_argument("--candidate", required=True)
    promote_decision.add_argument("--out", required=True)
    promote_decision.add_argument("--prior-loop-key", action="append", default=[])
    promote_decision.add_argument("--current-attempt", type=int)
    promote_decision.add_argument("--run-dir")
    promote_decision.set_defaults(func=_cmd_promote_decision)

    conduct_plan_candidate = subcommands.add_parser("conduct-plan-candidate")
    conduct_plan_candidate.add_argument("--intent-state", required=True)
    conduct_plan_candidate.add_argument("--current-work-item-id", required=True)
    conduct_plan_candidate.add_argument("--plan-path", required=True)
    conduct_plan_candidate.add_argument("--out", required=True)
    conduct_plan_candidate.set_defaults(func=_cmd_conduct_plan_candidate)

    promote_conduct_plan = subcommands.add_parser("promote-conduct-plan")
    promote_conduct_plan.add_argument("--candidate", required=True)
    promote_conduct_plan.add_argument("--approval-evidence-json", required=True)
    promote_conduct_plan.add_argument("--out", required=True)
    promote_conduct_plan.set_defaults(func=_cmd_promote_conduct_plan)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Session-intent admission and recovery predicates for autopilot mode.

Dependencies: Python 3.11+ standard library only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


ADMITTED_CRITERION_SOURCES = frozenset({"user-explicit", "previous-tool", "system-doc"})
MET_CRITERION_STATUSES = frozenset({"met", "done", "satisfied", "pass", "passed", "complete", "completed", "resolved", "closed"})
HIGH_IMPACT_CONDUCT_MARKERS = (
    "report-instead-of-execute",
    "plan instead of executing",
    "talk instead of execute",
    "talk-only",
    "no-op",
    "noop",
    "stale intent",
    "stale goal",
    "wrong objective",
    "goal drift",
    "scope drift",
    "unverified",
    "verification failure",
)
SEMANTIC_DELTA_STARVATION_THRESHOLD = 3


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _criterion_is_admitted(criterion: Mapping[str, Any]) -> bool:
    admitted = criterion.get("admitted")
    if admitted is True:
        return True
    if admitted is False:
        return False
    return str(criterion.get("source") or "").strip().lower() in ADMITTED_CRITERION_SOURCES


def _criterion_is_unmet(criterion: Mapping[str, Any]) -> bool:
    status = str(criterion.get("status") or "unmet").strip().lower()
    return status not in MET_CRITERION_STATUSES


def unmet_admitted_criteria_evidence(intent_state: Mapping[str, Any]) -> dict[str, Any] | None:
    criteria = intent_state.get("acceptance_criteria")
    if not isinstance(criteria, list):
        return None
    open_ids = [
        str(criterion.get("id")).strip()
        for criterion in criteria
        if isinstance(criterion, Mapping)
        and _criterion_is_admitted(criterion)
        and _criterion_is_unmet(criterion)
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


def _conduct_feedback_text(feedback: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("id", "summary", "corrective_rule", "failure_mode", "category", "evidence"):
        value = feedback.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple, dict)):
            try:
                parts.append(json.dumps(value, ensure_ascii=True, sort_keys=True))
            except TypeError:
                continue
    return "\n".join(parts).lower()


def _conduct_feedback_requires_followup(feedback: Mapping[str, Any]) -> bool:
    occurrence_count = feedback.get("occurrence_count", 1)
    if not isinstance(occurrence_count, int) or isinstance(occurrence_count, bool):
        occurrence_count = 1
    return occurrence_count >= 2 or any(
        marker in _conduct_feedback_text(feedback)
        for marker in HIGH_IMPACT_CONDUCT_MARKERS
    )


def open_conduct_feedback_evidence(intent_state: Mapping[str, Any]) -> dict[str, Any] | None:
    feedback = intent_state.get("conduct_feedback")
    if not isinstance(feedback, list):
        return None
    open_ids = [
        str(raw.get("id")).strip()
        for raw in feedback
        if isinstance(raw, Mapping)
        and str(raw.get("status") or "open").strip().lower() in {"open", "active"}
        and str(raw.get("id") or "").strip()
        and _conduct_feedback_requires_followup(raw)
    ]
    if not open_ids:
        return None
    return {
        "decision": "AUTO",
        "source": "open-conduct-feedback",
        "reason": "session intent has open conduct feedback requiring follow-up work",
        "open_feedback": open_ids,
    }


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
    events = _read_jsonl_objects(Path(events_path_value))
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

#!/usr/bin/env python3
"""Objective-lineage checks for autopilot continuation.

Dependencies: Python 3.11+ standard library only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


APPROVAL_DECISIONS = frozenset({"go", "approve", "approved", "auto"})
STOPWORDS = frozenset({
    "about",
    "after",
    "again",
    "and",
    "before",
    "current",
    "deeper",
    "from",
    "into",
    "local",
    "more",
    "same",
    "that",
    "the",
    "this",
    "with",
    "work",
})


def _summary_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        summary = value.get("summary")
        if isinstance(summary, str):
            return summary.strip()
    return ""


def _run_session_id(run: Mapping[str, Any]) -> str:
    approval = run.get("approval_evidence")
    if isinstance(approval, Mapping):
        session_intent = approval.get("session_intent")
        if isinstance(session_intent, Mapping):
            session_id = session_intent.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()
    return ""


def _run_summary(run: Mapping[str, Any]) -> str:
    scope_summary = _summary_text(run.get("scope"))
    if scope_summary:
        return scope_summary
    approval = run.get("approval_evidence")
    if isinstance(approval, Mapping):
        session_intent = approval.get("session_intent")
        if isinstance(session_intent, Mapping):
            for key in ("current_goal", "user_intent_summary"):
                value = session_intent.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _current_summary(intent_state: Mapping[str, Any]) -> str:
    for key in ("current_goal", "user_intent_summary"):
        value = intent_state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tokens(text: str) -> set[str]:
    normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return {part for part in normalized.split() if len(part) >= 3 and part not in STOPWORDS}


def _same_objective(left: str, right: str) -> bool:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = left_tokens & right_tokens
    return len(overlap) >= 3 or len(overlap) / min(len(left_tokens), len(right_tokens)) >= 0.5


def _current_has_autopilot_approval(intent_state: Mapping[str, Any]) -> bool:
    decisions = intent_state.get("decisions")
    if not isinstance(decisions, list):
        return False
    for raw in decisions:
        if not isinstance(raw, Mapping) or raw.get("superseded") is True:
            continue
        decision_id = str(raw.get("id") or "").strip()
        kind = str(raw.get("kind") or raw.get("type") or "").strip()
        if decision_id not in {"autopilot-run-approval", "autopilot-approval"} and kind != "autopilot_run_approval":
            continue
        decision = str(raw.get("decision") or "").strip().lower()
        source = raw.get("source")
        if decision in APPROVAL_DECISIONS and isinstance(source, str) and source.strip():
            return True
    return False


def _first_open_item_id(items: list[dict[str, Any]]) -> str:
    for status in ("running", "ready", "reopened"):
        for item in items:
            if item.get("status") == status and isinstance(item.get("id"), str):
                return str(item["id"])
    return "unknown"


def stale_continuation_event(
    run: Mapping[str, Any],
    items: list[dict[str, Any]],
    current_intent: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not current_intent:
        return None
    intent_state = current_intent.get("intent_state")
    if not isinstance(intent_state, Mapping):
        return None
    run_session = _run_session_id(run)
    current_session = str(current_intent.get("session_id") or intent_state.get("session_id") or "").strip()
    if run_session and current_session and run_session == current_session:
        return None
    if _current_has_autopilot_approval(intent_state):
        return None
    run_summary = _run_summary(run)
    current_summary = _current_summary(intent_state)
    if not run_summary or not current_summary or _same_objective(run_summary, current_summary):
        return None
    state_path = current_intent.get("state_path")
    return {
        "schema_version": "autopilot-event.v1",
        "event": "stale_continuation_parked",
        "run_id": run.get("run_id"),
        "work_item_id": _first_open_item_id(items),
        "run_session_id": run_session,
        "current_session_id": current_session,
        "run_summary": run_summary,
        "current_summary": current_summary,
        "current_state_path": str(Path(state_path)) if state_path else "",
        "reason": "current session intent is outside the approved autopilot objective lineage",
    }

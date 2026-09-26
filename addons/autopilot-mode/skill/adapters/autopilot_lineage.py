#!/usr/bin/env python3
"""Objective-lineage checks for autopilot continuation.

Dependencies: Python 3.11+ standard library only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def agent_runtime_context_is_explicit(source: Mapping[str, str]) -> bool:
    """Generic hosts must supply their current identity and authoritative root."""
    root = str(source.get("GHOST_ALICE_SESSION_INTENT_ROOT") or "").strip()
    return (
        str(source.get("GHOST_ALICE_PLATFORM") or "").strip().lower() == "agent-runtime"
        and bool(str(source.get("GHOST_ALICE_SESSION_ID") or "").strip())
        and bool(root)
        and Path(root).is_absolute()
    )


def intent_identity_matches(state: Mapping[str, Any], platform: str, session_id: str) -> bool:
    """Validate candidate identity before either bootstrap or continuation."""
    return (
        state.get("schema_version") == "session-intent-ledger.v1"
        and bool(session_id)
        and state.get("session_id") == session_id
        and state.get("platform") == platform
    )


def current_session_context(
    source: Mapping[str, str] | None,
    *,
    run_platform: str | None = None,
    hook_input: Mapping[str, Any] | None = None,
) -> dict[str, str] | None:
    """Resolve firing identity without copying the approved session identity."""
    if source is None and hook_input is None:
        return None
    context = dict(source or {})
    payload = hook_input or {}
    payload_session = next((
        value.strip() for key in ("session_id", "sessionId", "conversation_id", "thread_id")
        if isinstance(value := payload.get(key), str) and value.strip()
    ), "")
    platform = str(context.get("GHOST_ALICE_PLATFORM") or "").strip().lower() or run_platform
    native_session = str(context.get("CODEX_THREAD_ID") or "").strip() if platform == "codex" else ""
    if session_id := payload_session or native_session:
        context["GHOST_ALICE_SESSION_ID"] = session_id
    return context


def continuation_context_error(binding: Any, source: Mapping[str, str]) -> str:
    """Stored identity is canonical; only firing environment values normalize."""
    if binding is not None and (
        not isinstance(binding, Mapping)
        or binding.get("platform") not in ("codex", "claude", "agent-runtime")
    ):
        return "stored platform binding is not canonical; current-session approval is required"
    run_platform = binding.get("platform") if isinstance(binding, Mapping) else None
    firing_platform = str(source.get("GHOST_ALICE_PLATFORM") or "").strip().lower()
    if "agent-runtime" in (run_platform, firing_platform) and not agent_runtime_context_is_explicit(source):
        return "agent-runtime requires an explicit platform, session id, and absolute current intent root"
    return ""


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

META_AUDIT_MARKERS = (
    "accountability",
    "answer whether",
    "assess whether",
    "determine whether",
    "did you",
    "explain why",
    "over-execut",
    "overexecut",
    "previous autopilot",
    "prior autopilot",
    "side effect",
    "unnecessary",
    "오토파일럿 때문에",
    "필요없는",
    "불필요",
)


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


def _is_meta_audit_intent(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return any(marker in normalized for marker in META_AUDIT_MARKERS)


def _first_open_item_id(items: list[dict[str, Any]]) -> str:
    for status in ("running", "ready", "reopened"):
        for item in items:
            if item.get("status") == status and isinstance(item.get("id"), str):
                return str(item["id"])
    return "unknown"


def _stale_continuation_event(
    run: Mapping[str, Any],
    items: list[dict[str, Any]],
    *,
    current_session: str,
    current_summary: str,
    current_state_path: Any = "",
    reason: str,
) -> dict[str, Any]:
    state_path = Path(current_state_path) if current_state_path else ""
    return {
        "schema_version": "autopilot-event.v1",
        "event": "stale_continuation_parked",
        "run_id": run.get("run_id"),
        "work_item_id": _first_open_item_id(items),
        "run_session_id": _run_session_id(run),
        "current_session_id": current_session,
        "run_summary": _run_summary(run),
        "current_summary": current_summary,
        "current_state_path": str(state_path) if state_path else "",
        "reason": reason,
    }


def stale_continuation_missing_intent_event(
    run: Mapping[str, Any],
    items: list[dict[str, Any]],
    current_session_id: str | None,
    *,
    require_current_intent: bool = False,
) -> dict[str, Any] | None:
    current_session = str(current_session_id or "").strip()
    run_session = _run_session_id(run)
    if not run_session or (not current_session and not require_current_intent):
        return None
    return _stale_continuation_event(
        run,
        items,
        current_session=current_session,
        current_summary="",
        reason="selected current session context has no lineage-compatible intent state",
    )


def _run_source_state_paths(run: Mapping[str, Any]) -> set[str]:
    paths: set[str] = set()
    intent_source = run.get("intent_source")
    if isinstance(intent_source, Mapping):
        state_path = intent_source.get("state_path")
        if isinstance(state_path, str) and state_path.strip():
            paths.add(str(Path(state_path).expanduser()))
    approval = run.get("approval_evidence")
    if isinstance(approval, Mapping):
        session_intent = approval.get("session_intent")
        if isinstance(session_intent, Mapping):
            state_path = session_intent.get("state_path")
            if isinstance(state_path, str) and state_path.strip():
                paths.add(str(Path(state_path).expanduser()))
    return paths


def session_binding_mismatch_event(
    run: Mapping[str, Any],
    items: list[dict[str, Any]],
    current_intent: Mapping[str, Any] | None,
    source: Mapping[str, str] | None,
) -> dict[str, Any] | None:
    """Topic similarity cannot transfer a run's authority across identities."""
    source = source or {}
    current = current_intent or {}
    state = current.get("intent_state")
    state = state if isinstance(state, Mapping) else {}
    approval = run.get("approval_evidence")
    approval = approval if isinstance(approval, Mapping) else {}
    binding = approval.get("session_intent")
    binding = binding if isinstance(binding, Mapping) else {}
    run_session = _run_session_id(run)
    run_platform = str(binding.get("platform") or "").strip()
    current_session = str(source.get("GHOST_ALICE_SESSION_ID") or current.get("session_id") or state.get("session_id") or "").strip()
    current_platform = str(source.get("GHOST_ALICE_PLATFORM") or "").strip().lower()
    if not current_platform:
        current_platform = str(current.get("platform") or state.get("platform") or "")
    reason = ""
    if run_session and current_session and run_session != current_session:
        reason = "current session binding differs from the approved run; current-session approval is required"
    elif run_platform and current_platform and run_platform != current_platform:
        reason = "current platform binding differs from the approved run; current-session approval is required"
    elif current_intent and (
        any(str(value).strip() != current_session for value in (current.get("session_id"), state.get("session_id")) if value)
        or any(str(value).strip() != current_platform for value in (current.get("platform"), state.get("platform")) if value)
    ):
        reason = "current intent ledger identity disagrees with the firing session binding"
    if not reason:
        return None
    event = _stale_continuation_event(
        run, items, current_session=current_session,
        current_summary=_current_summary(state),
        current_state_path=current.get("state_path"), reason=reason,
    )
    event.update({"run_platform": run_platform, "current_platform": current_platform})
    return event


def stale_continuation_source_intent_event(
    run: Mapping[str, Any],
    items: list[dict[str, Any]],
    current_intent: Mapping[str, Any] | None,
    current_session_id: str | None,
) -> dict[str, Any] | None:
    if not current_intent:
        return None
    explicit_session = str(current_session_id or "").strip()
    run_session = _run_session_id(run)
    if not explicit_session or not run_session or explicit_session != run_session:
        return None
    intent_state = current_intent.get("intent_state")
    if not isinstance(intent_state, Mapping):
        return None
    state_path = current_intent.get("state_path")
    if not state_path:
        return None
    current_state_path = str(Path(state_path).expanduser())
    if current_state_path not in _run_source_state_paths(run):
        return None
    return _stale_continuation_event(
        run,
        items,
        current_session=explicit_session,
        current_summary=_current_summary(intent_state),
        current_state_path=state_path,
        reason="explicit current session only exposes the approved run source intent, not current-turn lineage evidence",
    )


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
    run_summary = _run_summary(run)
    current_summary = _current_summary(intent_state)
    state_path = current_intent.get("state_path")
    if not current_summary:
        if run_session and current_session:
            return _stale_continuation_event(
                run,
                items,
                current_session=current_session,
                current_summary="",
                current_state_path=state_path,
                reason="current session has no semantic intent summary that proves continuation remains inside the approved autopilot objective",
            )
        return None
    if not run_summary:
        return None
    if not _is_meta_audit_intent(current_summary) and _same_objective(run_summary, current_summary):
        return None
    return _stale_continuation_event(
        run,
        items,
        current_session=current_session,
        current_summary=current_summary,
        current_state_path=state_path,
        reason="current session intent is outside the approved autopilot objective lineage",
    )

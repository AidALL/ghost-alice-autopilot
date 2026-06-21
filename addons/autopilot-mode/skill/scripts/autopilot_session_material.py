#!/usr/bin/env python3
"""Shared session-intent material helpers for autopilot bridge and adapter.

Dependencies: Python 3.11+ standard library only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


RUN_SCHEMA_VERSION = "autopilot-run.v1"
DEFAULT_STOP_CONDITIONS = ["budget_exhausted", "user_stop"]
APPROVAL_DECISIONS = {"go", "approve", "approved", "auto"}
SAFE_EVENT_KEYS = (
    "event",
    "event_id",
    "platform",
    "session_id",
    "source",
    "delta_keys",
    "intent_delta_digest",
    "input_digest",
    "input_char_count",
    "intake_status",
    "intent_delta_status",
    "correlation_id",
    "tool_stage",
    "metadata",
    "observed_at",
    "created_at",
    "ts",
)


class SessionMaterialError(ValueError):
    """Raised when session-intent material cannot be read or normalized."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SessionMaterialError(f"{source}: expected JSON object")
    return value


def read_jsonl_objects(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SessionMaterialError(f"{source}:{lineno}: invalid JSON: {exc}") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def write_json_atomic(path: str | Path, value: Mapping[str, Any]) -> None:
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


def write_jsonl_atomic(path: str | Path, rows: list[Mapping[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for row in rows:
                out.write(json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
                out.write("\n")
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def safe_id(value: str, fallback: str = "session-intent") -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or fallback


def compact_event(event: Mapping[str, Any] | None) -> dict[str, Any]:
    if not event:
        return {}
    return {key: event[key] for key in SAFE_EVENT_KEYS if key in event}


def latest_event_of(events: list[dict[str, Any]], event_name: str) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("event") == event_name:
            return compact_event(event)
    return {}


def safe_recent_events(events: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    return [compact_event(event) for event in events[-limit:]]


def run_summary(intent_state: Mapping[str, Any]) -> str:
    for key in ("current_goal", "user_intent_summary"):
        value = intent_state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Autopilot run bootstrapped from session-intent ledger."


def acceptance_criteria_from_intent(intent_state: Mapping[str, Any]) -> list[str]:
    criteria: list[str] = []
    raw = intent_state.get("acceptance_criteria")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                criteria.append(item.strip())
            elif isinstance(item, Mapping):
                summary = item.get("summary")
                criterion_id = item.get("id")
                if isinstance(summary, str) and summary.strip():
                    if isinstance(criterion_id, str) and criterion_id.strip():
                        criteria.append(f"{criterion_id.strip()}: {summary.strip()}")
                    else:
                        criteria.append(summary.strip())
    return criteria or ["Satisfy the approved session-intent scope with fresh verification evidence."]


def _labeled_summary_items(value: Any, *, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for raw in value:
        if isinstance(raw, str) and raw.strip():
            items.append(raw.strip())
        elif isinstance(raw, Mapping):
            summary = raw.get("summary") or raw.get("text")
            item_id = raw.get("id")
            if isinstance(summary, str) and summary.strip():
                if isinstance(item_id, str) and item_id.strip():
                    items.append(f"{item_id.strip()}: {summary.strip()}")
                else:
                    items.append(summary.strip())
            elif isinstance(item_id, str) and item_id.strip():
                items.append(item_id.strip())
        if len(items) >= limit:
            break
    return items


def decision_context_from_intent(intent_state: Mapping[str, Any]) -> list[str]:
    active = []
    for raw in intent_state.get("decisions", []) if isinstance(intent_state.get("decisions"), list) else []:
        if isinstance(raw, Mapping) and raw.get("superseded") is True:
            continue
        active.append(raw)
    return _labeled_summary_items(active)


def open_questions_from_intent(intent_state: Mapping[str, Any]) -> list[str]:
    return _labeled_summary_items(intent_state.get("open_questions"))


def session_intent_task(
    *,
    intent_state: Mapping[str, Any],
    session_id: str,
    allowed_surfaces: list[str],
    source_locator: str | None = None,
) -> dict[str, Any]:
    task_id = f"session-intent-{safe_id(session_id)}"
    task = {
        "id": task_id,
        "status": "ready",
        "focus_layer": "macro",
        "depends_on": [],
        "prompt": run_summary(intent_state),
        "acceptance_criteria": acceptance_criteria_from_intent(intent_state),
        "allowed_surface": allowed_surfaces,
        "completion": {
            "state": "not_started",
            "verdict": None,
            "evidence": [],
            "completion_check_digest": None,
            "reopen_target": None,
        },
        "attempt": 0,
    }
    if isinstance(source_locator, str) and source_locator.strip():
        task["source_locator"] = source_locator.strip()
    decisions = decision_context_from_intent(intent_state)
    if decisions:
        task["decision_context"] = decisions
    open_questions = open_questions_from_intent(intent_state)
    if open_questions:
        task["open_questions"] = open_questions
    return task


def build_approved_run(
    *,
    intent_state: Mapping[str, Any],
    approval_evidence: Mapping[str, Any],
    run_id: str,
    remaining_steps: int,
    allowed_surfaces: list[str],
    stop_conditions: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "approved": True,
        "status": "running",
        "scope": {"summary": run_summary(intent_state)},
        "budget": {"remaining_steps": remaining_steps},
        "allowed_surfaces": allowed_surfaces,
        "stop_conditions": stop_conditions,
        "approval_evidence": dict(approval_evidence),
        "created_at": utc_now(),
    }


def load_governance_signal_module(*, required: bool = False):
    path = Path(__file__).resolve().parent / "autopilot_governance_signal.py"
    spec = importlib.util.spec_from_file_location("autopilot_governance_signal", path)
    if spec is None or spec.loader is None:
        if required:
            raise SessionMaterialError(f"cannot load governance signal module: {path}")
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

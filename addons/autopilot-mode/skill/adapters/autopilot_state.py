#!/usr/bin/env python3
"""Durable work-item state helpers for Ghost-ALICE autopilot mode."""

from __future__ import annotations

import copy
import json
import os
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
ALLOWED_DECISIONS = frozenset({
    "continue_next",
    "retry_same_unit",
    "reopen_micro",
    "reopen_meso",
    "reopen_macro",
    "ask_user_meta",
    "stop",
})
APPROVED_RUN_FILE = "approved-run.json"
TASKS_FILE = "tasks.jsonl"
DECISION_FILE = "consistency-decision.json"
APPLIED_DECISION_FILE = "consistency-decision.applied.json"
EVENTS_FILE = "events.jsonl"
OFF_FILE = "OFF"
NOOP_PAYLOAD = {"continue": True, "systemMessage": ""}


class AutopilotStateError(ValueError):
    """Raised when autopilot work-item state is invalid."""


class AutopilotActiveRunError(AutopilotStateError):
    """Raised when a start request would overwrite an active run."""


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutopilotStateError(f"{field} must be a non-empty string")
    return value


def _validate_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise AutopilotStateError(f"{field} must be a string array")
    return list(value)


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
    state_path = Path(path)
    items: list[dict[str, Any]] = []
    for lineno, line in enumerate(state_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AutopilotStateError(f"{state_path}:{lineno}: invalid JSON: {exc}") from exc
        items.append(value)
    return validate_work_items(items)


def write_work_items(path: str | Path, items: Iterable[dict[str, Any]]) -> None:
    state_path = Path(path)
    validated = validate_work_items(items)
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
        if item["status"] != "ready":
            continue
        if all(by_id[dep]["status"] in DEPENDENCY_SATISFIED_STATUSES for dep in item["depends_on"]):
            ready.append(item["id"])
    return ready


def _find_item(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    for item in items:
        if item["id"] == item_id:
            return item
    raise AutopilotStateError(f"unknown work item id {item_id!r}")


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
    if item["status"] != "running":
        raise AutopilotStateError(
            f"consistency decision state transition {decision!r} requires running work item {item_id!r}; "
            f"found {item['status']!r}"
        )

    if decision == "continue_next":
        if verdict != "pass" or not completion_check_digest or not evidence:
            raise AutopilotStateError("continue_next requires passing completion-check evidence")
        item["status"] = "completed"
        item["completion"].update({
            "state": "completed",
            "verdict": verdict,
            "evidence": list(evidence),
            "completion_check_digest": completion_check_digest,
            "reopen_target": None,
        })
    elif decision == "retry_same_unit":
        item["status"] = "ready"
        item["attempt"] += 1
        item["completion"].update({
            "state": "retry",
            "verdict": None,
            "evidence": list(evidence or []),
            "completion_check_digest": completion_check_digest,
            "reopen_target": None,
        })
    elif decision.startswith("reopen_"):
        target = decision.removeprefix("reopen_")
        item["status"] = "reopened"
        item["completion"].update({
            "state": "reopened",
            "verdict": None,
            "evidence": list(evidence or []),
            "completion_check_digest": completion_check_digest,
            "reopen_target": target,
        })
    elif decision in {"ask_user_meta", "stop"}:
        item["status"] = "stopped"
        item["completion"].update({
            "state": decision,
            "verdict": None,
            "evidence": list(evidence or []),
            "completion_check_digest": completion_check_digest,
            "reopen_target": "meta" if decision == "ask_user_meta" else None,
        })
    return validate_work_items(updated)


def rewrite_open_work_items(
    items: Iterable[dict[str, Any]],
    replacement_items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    current = validate_work_items(copy.deepcopy(list(items)))
    terminal = [item for item in current if item["status"] in TERMINAL_STATUSES]
    replacements = copy.deepcopy(list(replacement_items))
    return validate_work_items([*terminal, *replacements])


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
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AutopilotStateError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AutopilotStateError(f"{path}: expected JSON object")
    return value


def _write_json_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _approved_run_is_active(run: dict[str, Any]) -> bool:
    return run.get("approved") is True and run.get("status") == "running"


def _normalise_start_task(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AutopilotStateError("start spec tasks entries must be objects")
    item = dict(raw)
    item.setdefault("status", "ready")
    item.setdefault("depends_on", [])
    item.setdefault("completion", {
        "state": "not_started",
        "verdict": None,
        "evidence": [],
        "completion_check_digest": None,
        "reopen_target": None,
    })
    item.setdefault("attempt", 0)
    return item


def _start_run_record_from_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _require_string(spec.get("run_id"), "run_id")
    scope = spec.get("scope")
    if not _has_non_empty_scope(scope):
        raise AutopilotStateError("scope must be a non-empty string or object")
    budget = spec.get("budget")
    if not isinstance(budget, dict):
        raise AutopilotStateError("budget must be an object")
    remaining_steps = budget.get("remaining_steps")
    if not isinstance(remaining_steps, int) or isinstance(remaining_steps, bool) or remaining_steps <= 0:
        raise AutopilotStateError("budget.remaining_steps must be a positive integer")
    allowed_surfaces = spec.get("allowed_surfaces")
    if not _is_non_empty_string_array(allowed_surfaces):
        raise AutopilotStateError("allowed_surfaces must be a non-empty string array")
    stop_conditions = spec.get("stop_conditions")
    if not _is_non_empty_string_array(stop_conditions):
        raise AutopilotStateError("stop_conditions must be a non-empty string array")
    approval_evidence = spec.get("approval_evidence")
    if not isinstance(approval_evidence, dict) or not approval_evidence:
        raise AutopilotStateError("approval_evidence must be a non-empty object")
    if approval_evidence.get("decision") != "GO":
        raise AutopilotStateError("approval_evidence.decision must be GO")

    return {
        "schema_version": "autopilot-run.v1",
        "run_id": run_id,
        "approved": True,
        "status": "running",
        "scope": scope,
        "budget": {"remaining_steps": remaining_steps},
        "allowed_surfaces": list(allowed_surfaces),
        "stop_conditions": list(stop_conditions),
        "approval_evidence": dict(approval_evidence),
    }


def start_approved_run(
    run_dir: str | Path,
    spec: Mapping[str, Any],
    *,
    replace_active: bool = False,
) -> dict[str, Any]:
    root = Path(run_dir)
    if not isinstance(spec, Mapping):
        raise AutopilotStateError("start spec must be a JSON object")

    approved_run_path = root / APPROVED_RUN_FILE
    tasks_path = root / TASKS_FILE
    if approved_run_path.exists() and tasks_path.exists() and not replace_active:
        try:
            existing = _read_json_object(approved_run_path)
        except AutopilotStateError:
            existing = {}
        if _approved_run_is_active(existing):
            raise AutopilotActiveRunError("active autopilot run already exists")

    run = _start_run_record_from_spec(spec)
    raw_tasks = spec.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise AutopilotStateError("tasks must be a non-empty array")
    items = validate_work_items(_normalise_start_task(task) for task in raw_tasks)
    for item in items:
        if not _work_item_within_run_surfaces(run, item):
            raise AutopilotStateError(f"work item {item['id']!r} is outside run allowed_surfaces")

    root.mkdir(parents=True, exist_ok=True)
    for residue in (DECISION_FILE, APPLIED_DECISION_FILE, EVENTS_FILE, OFF_FILE):
        try:
            (root / residue).unlink()
        except FileNotFoundError:
            pass
    _write_json_object(approved_run_path, run)
    write_work_items(tasks_path, items)
    _append_event(
        root,
        {
            "schema_version": "autopilot-event.v1",
            "event": "approved_run_started",
            "run_id": run["run_id"],
            "task_count": len(items),
        },
    )
    return {
        "schema_version": "autopilot-start-summary.v1",
        "run_id": run["run_id"],
        "run_dir": str(root),
        "task_count": len(items),
        "remaining_steps": run["budget"]["remaining_steps"],
    }


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
    if not _has_non_empty_approval_evidence(run.get("approval_evidence")):
        return False
    return True


def _append_event(run_dir: Path, event: dict[str, Any]) -> None:
    event_path = run_dir / EVENTS_FILE
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")


def _apply_pending_decision(run_dir: Path, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decision_path = run_dir / DECISION_FILE
    if not decision_path.is_file():
        return items
    decision = _read_json_object(decision_path)
    item_id = _require_string(decision.get("work_item_id"), "consistency decision work_item_id")
    decision_value = _require_string(decision.get("decision"), "consistency decision decision")
    evidence = decision.get("evidence")
    if evidence is not None:
        evidence = _validate_string_list(evidence, "consistency decision evidence")
    updated = apply_consistency_decision(
        items,
        item_id,
        decision_value,
        completion_check_digest=decision.get("completion_check_digest"),
        verdict=decision.get("verdict"),
        evidence=evidence,
    )
    write_work_items(run_dir / TASKS_FILE, updated)
    os.replace(decision_path, run_dir / APPLIED_DECISION_FILE)
    _append_event(
        run_dir,
        {
            "schema_version": "autopilot-event.v1",
            "event": "consistency_decision_applied",
            "decision": decision_value,
            "decision_id": decision.get("decision_id"),
            "work_item_id": item_id,
        },
    )
    return updated


def _build_continuation_message(run: dict[str, Any], item: dict[str, Any]) -> str:
    lines = [
        "[autopilot]",
        f"run: {run.get('run_id', 'unknown')}",
        f"work-item: {item['id']}",
        f"focus-layer: {item['focus_layer']}",
        "allowed-surface:",
    ]
    lines.extend(f"- {surface}" for surface in item["allowed_surface"])
    lines.append("acceptance-criteria:")
    lines.extend(f"- {criterion}" for criterion in item["acceptance_criteria"])
    lines.extend(["prompt:", item["prompt"]])
    return "\n".join(lines)


def _select_ready_item(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    updated = validate_work_items(copy.deepcopy(items))
    item = _find_item(updated, item_id)
    item["status"] = "running"
    return {"item": item, "items": updated}


def _consume_budget_step(run: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(run)
    updated["budget"]["remaining_steps"] -= 1
    return updated


def advance_approved_run(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    approved_run_path = root / APPROVED_RUN_FILE
    tasks_path = root / TASKS_FILE
    if (root / OFF_FILE).exists():
        return _noop_payload()
    if not approved_run_path.is_file() or not tasks_path.is_file():
        return _noop_payload()

    run = _read_json_object(approved_run_path)
    if not _approved_run_allows_continue(run):
        return _noop_payload()

    items = read_work_items(tasks_path)
    items = _apply_pending_decision(root, items)
    ready_queue = derive_ready_queue(items)
    if not ready_queue:
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
    updated_run = _consume_budget_step(run)
    write_work_items(tasks_path, selected["items"])
    _write_json_object(approved_run_path, updated_run)
    _append_event(
        root,
        {
            "schema_version": "autopilot-event.v1",
            "event": "continue_next_item",
            "run_id": run.get("run_id"),
            "work_item_id": selected_item["id"],
            "focus_layer": selected_item["focus_layer"],
            "remaining_steps": updated_run["budget"]["remaining_steps"],
        },
    )
    return {
        "continue": True,
        "systemMessage": _build_continuation_message(run, selected_item),
    }


def adapter_payload_from_env(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    run_dir = source.get("GHOST_ALICE_AUTOPILOT_RUN_DIR")
    if run_dir:
        return advance_approved_run(run_dir)
    explicit_cwd = source.get("GHOST_ALICE_AUTOPILOT_CWD")
    if explicit_cwd:
        return advance_approved_run(Path(explicit_cwd).expanduser() / ".autopilot")
    cwd_run_dir = Path.cwd() / ".autopilot"
    if (cwd_run_dir / APPROVED_RUN_FILE).is_file() or (cwd_run_dir / TASKS_FILE).is_file():
        return advance_approved_run(cwd_run_dir)
    pwd = source.get("PWD")
    if pwd:
        return advance_approved_run(Path(pwd).expanduser() / ".autopilot")
    return advance_approved_run(cwd_run_dir)

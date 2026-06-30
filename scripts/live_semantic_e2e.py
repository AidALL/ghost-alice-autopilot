#!/usr/bin/env python3
"""Run or summarize live semantic E2E probes for Ghost-ALICE autopilot.

The harness keeps authentication state separate from inference state. A CLI can
report an authenticated account while the actual model call still fails, so
callers must not treat auth status as semantic execution evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HOOK_LINE = re.compile(r"^hook: ([A-Za-z0-9_-]+)$")
REQUIRED_LIVE_HOOKS = ("SessionStart", "UserPromptSubmit", "Stop")
OBSERVATION_SIGNAL_SCHEMA = "autopilot-observation-signal.v1"
DEFAULT_INTENT_SCENARIO_LIMIT = 12
RAW_INTENT_FIELDS = {"raw_prompt", "transcript", "message_log", "conversation"}


@dataclass(frozen=True)
class LiveScenario:
    id: str
    prompt: str
    expected_keys: tuple[str, ...] = (
        "verdict",
        "mismatch_detected",
        "focus_layer",
        "next_action",
        "loop_guard",
    )
    source_kind: str = "static"
    source_locator: str | None = None


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fresh_install_module() -> Any:
    path = repo_root() / "scripts" / "fresh_install_e2e.py"
    spec = importlib.util.spec_from_file_location("fresh_install_e2e", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def default_intent_roots() -> list[Path]:
    return [
        repo_root().parent / "ghost-alice" / ".tmp" / "session-intent",
        Path.home() / ".ghost-alice" / "session-intent",
    ]


def _safe_id(value: Any) -> str:
    text = str(value or "unknown").lower()
    safe = "".join(ch if ch.isalnum() else "-" for ch in text).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "unknown"


def _string_list(value: Any, *, limit: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            items.append(item.strip())
        elif isinstance(item, dict):
            text = item.get("summary") or item.get("text") or item.get("id")
            if isinstance(text, str) and text.strip():
                items.append(text.strip())
        if len(items) >= limit:
            break
    return items


def _criteria_list(value: Any, *, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    criteria: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            criteria.append(item.strip())
        elif isinstance(item, dict):
            summary = item.get("summary")
            criterion_id = item.get("id")
            if isinstance(summary, str) and summary.strip():
                prefix = f"{criterion_id}: " if isinstance(criterion_id, str) and criterion_id.strip() else ""
                criteria.append(f"{prefix}{summary.strip()}")
        if len(criteria) >= limit:
            break
    return criteria


def _decision_list(value: Any, *, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    decisions: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            decisions.append(item.strip())
        elif isinstance(item, dict) and item.get("superseded") is not True:
            text = item.get("summary") or item.get("text") or item.get("id")
            if isinstance(text, str) and text.strip():
                decisions.append(text.strip())
        if len(decisions) >= limit:
            break
    return decisions


def _open_question_list(value: Any, *, limit: int = 6) -> list[str]:
    return _string_list(value, limit=limit)


def _format_bullets(title: str, items: Sequence[str]) -> list[str]:
    if not items:
        return []
    return [f"{title}:"] + [f"- {item}" for item in items]


def _intent_state_paths(intent_roots: Sequence[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in intent_roots:
        if not root.is_dir():
            continue
        paths.extend(path for path in root.glob("*/*/intent-state.json") if path.is_file())
    return sorted(paths, key=lambda path: str(path))


def _intent_path_parts(path: Path) -> tuple[str, str]:
    try:
        return path.parts[-3], path.parts[-2]
    except IndexError:
        return "unknown", path.parent.name or "unknown"


def _state_relevance_score(state: Mapping[str, Any], feedback: Mapping[str, Any] | None = None) -> int:
    haystack = " ".join(
        str(item)
        for item in [
            state.get("current_goal"),
            state.get("user_intent_summary"),
            state.get("constraints"),
            state.get("acceptance_criteria"),
            state.get("decisions"),
            state.get("open_questions"),
            feedback,
        ]
    ).lower()
    terms = (
        "autopilot",
        "skill",
        "conduct",
        "completion",
        "verification",
        "claude",
        "codex",
        "install",
        "hook",
        "plan",
        "focus",
        "evidence",
    )
    score = sum(1 for term in terms if term in haystack)
    if feedback:
        occurrence_count = feedback.get("occurrence_count")
        if isinstance(occurrence_count, int) and not isinstance(occurrence_count, bool):
            score += min(occurrence_count, 5)
    return score


def _scenario_prompt_from_intent(
    *,
    platform: str,
    session_id: str,
    state: Mapping[str, Any],
    feedback: Mapping[str, Any] | None = None,
) -> str:
    lines = [
        "You are evaluating a compressed Ghost-ALICE intent artifact.",
        "Do not use tools. Do not browse. Do not execute shell commands. Do not read or write files.",
        "Judge only what the autopilot controller should do from the compressed artifact below.",
        "",
        "Compressed intent source:",
        f"Source platform: {platform}",
        f"Session id: {session_id}",
    ]
    current_goal = state.get("current_goal")
    if isinstance(current_goal, str) and current_goal.strip():
        lines.append(f"Current goal: {current_goal.strip()}")
    user_intent_summary = state.get("user_intent_summary")
    if isinstance(user_intent_summary, str) and user_intent_summary.strip():
        lines.append(f"User intent summary: {user_intent_summary.strip()}")
    if feedback:
        feedback_id = feedback.get("id")
        if isinstance(feedback_id, str) and feedback_id.strip():
            lines.append(f"Conduct feedback id: {feedback_id.strip()}")
        summary = feedback.get("summary")
        if isinstance(summary, str) and summary.strip():
            lines.append(f"Conduct feedback summary: {summary.strip()}")
        corrective_rule = feedback.get("corrective_rule")
        if isinstance(corrective_rule, str) and corrective_rule.strip():
            lines.append(f"Corrective rule: {corrective_rule.strip()}")
        failure_pattern = feedback.get("failure_pattern")
        if isinstance(failure_pattern, str) and failure_pattern.strip():
            lines.append(f"Failure pattern: {failure_pattern.strip()}")
    lines.extend(_format_bullets("Constraints", _string_list(state.get("constraints"))))
    lines.extend(_format_bullets("Acceptance criteria", _criteria_list(state.get("acceptance_criteria"))))
    lines.extend(_format_bullets("Decisions", _decision_list(state.get("decisions"))))
    lines.extend(_format_bullets("Open questions", _open_question_list(state.get("open_questions"))))
    lines.extend(
        [
            "",
            "Evaluate how an autopilot controller should respond to this compressed intent artifact.",
            "Classify whether it should continue, reopen focus, update a plan or skill, request verification, or ask the user.",
            "Respond in compact JSON with keys: verdict, mismatch_detected, focus_layer, next_action, loop_guard.",
        ]
    )
    return "\n".join(lines)


def _scenario_from_intent_feedback(path: Path, state: Mapping[str, Any], feedback: Mapping[str, Any]) -> LiveScenario:
    platform, session_id = _intent_path_parts(path)
    feedback_id = feedback.get("id") if isinstance(feedback.get("id"), str) else "conduct-feedback"
    return LiveScenario(
        id=f"intent-{_safe_id(platform)}-{_safe_id(session_id)}-{_safe_id(feedback_id)}",
        prompt=_scenario_prompt_from_intent(
            platform=platform,
            session_id=session_id,
            state=state,
            feedback=feedback,
        ),
        source_kind="intent-state",
        source_locator=f"{path}#conduct_feedback:{feedback_id}",
    )


def _scenario_from_intent_criteria(path: Path, state: Mapping[str, Any]) -> LiveScenario:
    platform, session_id = _intent_path_parts(path)
    return LiveScenario(
        id=f"intent-{_safe_id(platform)}-{_safe_id(session_id)}-acceptance-criteria",
        prompt=_scenario_prompt_from_intent(platform=platform, session_id=session_id, state=state),
        source_kind="intent-state",
        source_locator=f"{path}#acceptance_criteria",
    )


def _scenario_from_intent_decisions(path: Path, state: Mapping[str, Any]) -> LiveScenario:
    platform, session_id = _intent_path_parts(path)
    return LiveScenario(
        id=f"intent-{_safe_id(platform)}-{_safe_id(session_id)}-decisions",
        prompt=_scenario_prompt_from_intent(platform=platform, session_id=session_id, state=state),
        source_kind="intent-state",
        source_locator=f"{path}#decisions",
    )


def _scenario_from_intent_open_questions(path: Path, state: Mapping[str, Any]) -> LiveScenario:
    platform, session_id = _intent_path_parts(path)
    return LiveScenario(
        id=f"intent-{_safe_id(platform)}-{_safe_id(session_id)}-open-questions",
        prompt=_scenario_prompt_from_intent(platform=platform, session_id=session_id, state=state),
        source_kind="intent-state",
        source_locator=f"{path}#open_questions",
    )


def intent_sourced_live_scenarios(
    *,
    intent_roots: Sequence[Path] | None = None,
    limit: int | None = DEFAULT_INTENT_SCENARIO_LIMIT,
) -> list[LiveScenario]:
    roots = list(intent_roots) if intent_roots is not None else default_intent_roots()
    candidates: list[tuple[int, str, LiveScenario]] = []
    for path in _intent_state_paths(roots):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        feedback_items = [item for item in state.get("conduct_feedback", []) if isinstance(item, dict)]
        for feedback in feedback_items:
            if RAW_INTENT_FIELDS.intersection(feedback):
                continue
            if feedback.get("status") not in {None, "open", "active"}:
                continue
            scenario = _scenario_from_intent_feedback(path, state, feedback)
            candidates.append((_state_relevance_score(state, feedback), scenario.id, scenario))
        if _criteria_list(state.get("acceptance_criteria")):
            scenario = _scenario_from_intent_criteria(path, state)
            candidates.append((_state_relevance_score(state), scenario.id, scenario))
        if _decision_list(state.get("decisions")):
            scenario = _scenario_from_intent_decisions(path, state)
            candidates.append((_state_relevance_score(state), scenario.id, scenario))
        if _open_question_list(state.get("open_questions")):
            scenario = _scenario_from_intent_open_questions(path, state)
            candidates.append((_state_relevance_score(state), scenario.id, scenario))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    selected = candidates if limit is None else candidates[:limit]
    return [scenario for _, _, scenario in selected]


def _static_live_scenarios() -> list[LiveScenario]:
    module = _fresh_install_module()
    scenarios = []
    for item in module.semantic_scenarios():
        source_prompt = item["prompt"]
        scenarios.append(
            LiveScenario(
                id=item["id"],
                prompt=(
                    "You are evaluating a hypothetical Ghost-ALICE autopilot behavior scenario. "
                    "Do not execute repository, install, auth, shell, or file actions described in the scenario. "
                    "Evaluate the scenario text and classify the intended agent behavior only.\n\n"
                    f"Scenario text:\n{source_prompt}\n\n"
                    "Respond in compact JSON with keys: verdict, mismatch_detected, "
                    "focus_layer, next_action, loop_guard."
                ),
                source_kind="static",
                source_locator=f"fresh_install_e2e.semantic_scenarios:{item['id']}",
            )
        )
    return scenarios


def live_scenarios(
    *,
    scenario_source: str = "auto",
    intent_roots: Sequence[Path] | None = None,
    limit: int | None = DEFAULT_INTENT_SCENARIO_LIMIT,
) -> list[LiveScenario]:
    if scenario_source not in {"auto", "intent", "static"}:
        raise ValueError(f"unsupported scenario source: {scenario_source}")
    if scenario_source in {"auto", "intent"}:
        intent_scenarios = intent_sourced_live_scenarios(intent_roots=intent_roots, limit=limit)
        if intent_scenarios or scenario_source == "intent":
            return intent_scenarios
    return _static_live_scenarios()


def _parse_json_text(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    try:
        payload, _ = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    return payload


def _missing_expected_keys(payload: Any, expected_keys: Sequence[str]) -> list[str]:
    if not expected_keys:
        return []
    if not isinstance(payload, dict):
        return list(expected_keys)
    return [key for key in expected_keys if key not in payload]


def _invalid_semantic_fields(payload: Any, expected_keys: Sequence[str]) -> list[str]:
    if not isinstance(payload, dict) or not expected_keys:
        return []
    invalid: list[str] = []
    for key in expected_keys:
        value = payload.get(key)
        if key == "mismatch_detected":
            if _semantic_bool_value(value) is None:
                invalid.append(key)
        elif key in {"next_action", "loop_guard"} and isinstance(value, (dict, list)):
            if not value:
                invalid.append(key)
        elif not isinstance(value, str) or not value.strip():
            invalid.append(key)
    return invalid


def _semantic_status(
    payload: Any,
    *,
    schema_checked: bool,
    missing_keys: Sequence[str] = (),
    invalid_fields: Sequence[str] = (),
) -> str:
    if payload is None:
        return "unparsed"
    if not schema_checked:
        return "schema-unchecked"
    if missing_keys:
        return "schema-mismatch"
    if invalid_fields:
        return "semantic-invalid"
    if isinstance(payload, list):
        return "parsed" if payload else "unparsed"
    if isinstance(payload, dict):
        return "parsed"
    return "unparsed"


def _missing_required_hooks(hook_events: dict[str, int]) -> list[str]:
    return [hook for hook in REQUIRED_LIVE_HOOKS if hook_events.get(hook, 0) <= 0]


def _apply_hook_completeness(
    semantic_status: str,
    hook_events: dict[str, int],
    *,
    inference_status: str,
) -> tuple[str, str, list[str]]:
    if inference_status != "ok" or semantic_status != "parsed":
        return semantic_status, "not-applicable", []
    missing_hooks = _missing_required_hooks(hook_events)
    if missing_hooks:
        return "hook-incomplete", "missing-required", missing_hooks
    return semantic_status, "complete", []


def _safe_signal_component(value: Any) -> str:
    text = str(value or "unknown").lower()
    safe = "".join(ch if ch.isalnum() else "-" for ch in text).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "unknown"


def _observation_classification(summary: Mapping[str, Any]) -> str:
    inference_status = summary.get("inference_status")
    semantic_status = summary.get("semantic_status")
    agent_activity = summary.get("agent_activity")
    if inference_status in {"auth-failed", "config-failed"}:
        return "readiness-blocker"
    if agent_activity == "tool-loop-timeout":
        return "tool-loop-timeout"
    if semantic_status in {"hook-incomplete", "schema-mismatch", "semantic-invalid"}:
        return str(semantic_status)
    if inference_status == "timeout":
        return "timeout"
    if inference_status == "failed":
        return "inference-failed"
    if semantic_status == "parsed" and summary.get("hook_status") == "complete":
        return "semantic-observation"
    return "diagnostic"


def _default_focus_layer(classification: str) -> str | None:
    if classification in {"tool-loop-timeout", "timeout", "hook-incomplete"}:
        return "meso"
    if classification in {"schema-mismatch", "semantic-invalid"}:
        return "micro"
    return None


def _default_mismatch_detected(classification: str) -> bool:
    return classification in {
        "tool-loop-timeout",
        "timeout",
        "hook-incomplete",
        "schema-mismatch",
        "semantic-invalid",
        "inference-failed",
    }


def _semantic_bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and "value" in value:
        return _semantic_bool_value(value.get("value"))
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return None
        false_markers = ("no", "false", "none")
        if normalized in false_markers or any(normalized.startswith(f"{marker}:") for marker in false_markers):
            return False
        true_markers = (
            "yes",
            "true",
            "surface",
            "gap",
            "mismatch",
            "blocker",
            "drift",
            "stale",
            "conflict",
        )
        if any(marker in normalized for marker in true_markers):
            return True
    return None


def _compact_evidence(summary: Mapping[str, Any]) -> dict[str, Any]:
    evidence = {
        "output_file": summary.get("output_file"),
        "last_message_file": summary.get("last_message_file"),
        "returncode": summary.get("returncode"),
        "api_error_status": summary.get("api_error_status"),
        "missing_keys": summary.get("missing_keys"),
        "invalid_fields": summary.get("invalid_fields"),
    }
    return {
        key: value
        for key, value in evidence.items()
        if value is not None and value != [] and value != {}
    }


def observation_signal_from_summary(
    summary: Mapping[str, Any],
    *,
    scenario: LiveScenario | None = None,
) -> dict[str, Any]:
    scenario_result = summary.get("scenario_result")
    payload = scenario_result if isinstance(scenario_result, dict) else {}
    classification = _observation_classification(summary)
    focus_layer = payload.get("focus_layer")
    if not isinstance(focus_layer, str) or not focus_layer.strip():
        focus_layer = _default_focus_layer(classification)
    mismatch_detected = _semantic_bool_value(payload.get("mismatch_detected"))
    if mismatch_detected is None:
        mismatch_detected = _default_mismatch_detected(classification)
    verdict = payload.get("verdict")
    if isinstance(verdict, str):
        verdict = verdict.strip() or None
    else:
        verdict = None
    next_action = payload.get("next_action")
    if isinstance(next_action, str) and not next_action.strip():
        next_action = None
    elif not isinstance(next_action, (str, dict, list)):
        next_action = None
    loop_guard = payload.get("loop_guard")
    if isinstance(loop_guard, str) and not loop_guard.strip():
        loop_guard = classification if classification == "tool-loop-timeout" else None
    elif not isinstance(loop_guard, (str, dict, list)):
        loop_guard = classification if classification == "tool-loop-timeout" else None
    runtime = summary.get("runtime")
    scenario_id = summary.get("scenario_id") or (scenario.id if scenario else None)
    return {
        "schema_version": OBSERVATION_SIGNAL_SCHEMA,
        "source": "live_semantic_e2e",
        "signal_id": (
            "observation-"
            f"{_safe_signal_component(runtime)}-"
            f"{_safe_signal_component(scenario_id)}-"
            f"{_safe_signal_component(classification)}"
        ),
        "runtime": runtime,
        "scenario_id": scenario_id,
        "observation_mode": "live-semantic-e2e",
        "classification": classification,
        "inference_status": summary.get("inference_status"),
        "semantic_status": summary.get("semantic_status"),
        "hook_status": summary.get("hook_status"),
        "missing_hooks": summary.get("missing_hooks", []),
        "agent_activity": summary.get("agent_activity", "unknown"),
        "verdict": verdict,
        "mismatch_detected": mismatch_detected,
        "focus_layer": focus_layer,
        "next_action": next_action,
        "loop_guard": loop_guard,
        "evidence": _compact_evidence(summary),
    }


def _count_hook_event(events: dict[str, int], name: str | None) -> None:
    if not name:
        return
    events[name] = events.get(name, 0) + 1


def parse_claude_stream_json(path: Path, *, scenario: LiveScenario | None = None) -> dict[str, Any]:
    hook_events: dict[str, int] = {}
    result: dict[str, Any] | None = None
    assistant_text: str | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") in {"hook_started", "hook_response"}:
            _count_hook_event(hook_events, event.get("hook_event"))
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            text_items = [item.get("text", "") for item in content if item.get("type") == "text"]
            assistant_text = "\n".join(text_items).strip() or assistant_text
        if event.get("type") == "result":
            result = event

    api_error_status = result.get("api_error_status") if result else None
    is_error = bool(result and result.get("is_error"))
    if api_error_status in {401, 403}:
        inference_status = "auth-failed"
        semantic_status = "not-run"
        scenario_result = None
    elif is_error:
        inference_status = "failed"
        semantic_status = "not-run"
        scenario_result = None
    else:
        scenario_result = _parse_json_text(assistant_text or (result or {}).get("result", ""))
        expected_keys = scenario.expected_keys if scenario else ()
        schema_checked = scenario is not None
        missing_keys = _missing_expected_keys(scenario_result, expected_keys)
        invalid_fields = _invalid_semantic_fields(scenario_result, expected_keys)
        inference_status = "ok" if result else "unknown"
        semantic_status = _semantic_status(
            scenario_result,
            schema_checked=schema_checked,
            missing_keys=missing_keys,
            invalid_fields=invalid_fields,
        )
    semantic_status, hook_status, missing_hooks = _apply_hook_completeness(
        semantic_status,
        hook_events,
        inference_status=inference_status,
    )

    return {
        "runtime": "claude",
        "inference_status": inference_status,
        "semantic_status": semantic_status,
        "api_error_status": api_error_status,
        "hook_events": hook_events,
        "scenario_result": scenario_result,
        "missing_keys": missing_keys if "missing_keys" in locals() else [],
        "invalid_fields": invalid_fields if "invalid_fields" in locals() else [],
        "hook_status": hook_status,
        "missing_hooks": missing_hooks,
    }


def _looks_like_auth_failure(text: str, returncode: int) -> bool:
    if returncode == 0:
        return False
    lowered = text.lower()
    auth_markers = (
        "not logged in",
        "run codex login",
        "authentication",
        "authenticate",
        "unauthorized",
        "invalid api key",
        "401",
        "403",
    )
    return any(marker in lowered for marker in auth_markers)


def _looks_like_config_failure(text: str, returncode: int) -> bool:
    if returncode == 0:
        return False
    lowered = text.lower()
    return "error loading config.toml" in lowered or (
        "config.toml" in lowered and "unknown variant" in lowered
    )


def _codex_agent_activity(hook_events: dict[str, int], returncode: int, has_result: bool) -> str:
    tool_events = hook_events.get("PreToolUse", 0) + hook_events.get("PostToolUse", 0)
    if returncode == 124 and tool_events:
        return "tool-loop-timeout"
    if tool_events:
        return "tool-using"
    if has_result and hook_events.get("Stop", 0):
        return "direct-answer"
    if returncode == 124:
        return "timeout-no-tool"
    return "unknown"


def parse_codex_outputs(
    log_path: Path,
    last_message_path: Path,
    *,
    returncode: int,
    scenario: LiveScenario | None = None,
) -> dict[str, Any]:
    hook_events: dict[str, int] = {}
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = HOOK_LINE.match(line.strip())
            if match:
                _count_hook_event(hook_events, match.group(1))
    last_message = (
        last_message_path.read_text(encoding="utf-8", errors="replace")
        if last_message_path.exists()
        else ""
    )
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    scenario_result = _parse_json_text(last_message)
    has_result = scenario_result is not None
    agent_activity = _codex_agent_activity(hook_events, returncode, has_result)
    expected_keys = scenario.expected_keys if scenario else ()
    schema_checked = scenario is not None
    missing_keys = _missing_expected_keys(scenario_result, expected_keys)
    invalid_fields = _invalid_semantic_fields(scenario_result, expected_keys)
    if returncode == 124:
        inference_status = "timeout"
        semantic_status = "not-run"
        missing_keys = []
        invalid_fields = []
    elif _looks_like_auth_failure(log_text + "\n" + last_message, returncode):
        inference_status = "auth-failed"
        semantic_status = "not-run"
        missing_keys = []
        invalid_fields = []
    elif _looks_like_config_failure(log_text + "\n" + last_message, returncode):
        inference_status = "config-failed"
        semantic_status = "not-run"
        missing_keys = []
        invalid_fields = []
    else:
        inference_status = "ok" if returncode == 0 and bool(last_message.strip()) else "failed"
        semantic_status = _semantic_status(
            scenario_result,
            schema_checked=schema_checked,
            missing_keys=missing_keys,
            invalid_fields=invalid_fields,
        )
    semantic_status, hook_status, missing_hooks = _apply_hook_completeness(
        semantic_status,
        hook_events,
        inference_status=inference_status,
    )
    return {
        "runtime": "codex",
        "inference_status": inference_status,
        "semantic_status": semantic_status,
        "returncode": returncode,
        "hook_events": hook_events,
        "scenario_result": scenario_result,
        "missing_keys": missing_keys,
        "invalid_fields": invalid_fields,
        "agent_activity": agent_activity,
        "hook_status": hook_status,
        "missing_hooks": missing_hooks,
    }


def build_claude_command(
    scenario: LiveScenario,
    output_path: Path,
    *,
    max_budget_usd: float = 0.20,
) -> list[str]:
    return [
        "claude",
        "-p",
        scenario.prompt,
        "--verbose",
        "--output-format",
        "stream-json",
        "--include-hook-events",
        "--max-budget-usd",
        f"{max_budget_usd:.2f}",
        "--no-session-persistence",
    ]


def resolve_codex_command(
    codex_bin: str,
    *,
    which=shutil.which,
    platform: str = os.name,
) -> list[str]:
    requested = Path(codex_bin)
    explicit_path = requested.is_absolute() or requested.parent != Path(".")
    if explicit_path:
        if requested.suffix.lower() == ".ps1":
            pwsh = which("pwsh.exe") or which("pwsh")
            if not pwsh:
                return []
            return [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(requested)]
        return [str(requested)]

    if platform == "nt" and codex_bin.lower() == "codex":
        cmd = which("codex.cmd")
        if cmd:
            return [cmd]
        ps1 = which("codex.ps1")
        if ps1:
            pwsh = which("pwsh.exe") or which("pwsh")
            if pwsh:
                return [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1]
        exe = which("codex.exe")
        if exe:
            return [exe]

    resolved = which(codex_bin)
    return [resolved] if resolved else [codex_bin]


def codex_supports_hook_trust(
    codex_command: Sequence[str],
    *,
    cwd: Path,
    timeout_sec: float = 15,
) -> bool:
    try:
        completed = subprocess.run(
            [*codex_command, "exec", "--help"],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--dangerously-bypass-hook-trust" in (completed.stdout or "")


def build_codex_command(
    scenario: LiveScenario,
    log_path: Path,
    last_message_path: Path,
    *,
    codex_bin: str = "codex",
    codex_command: Sequence[str] | None = None,
    hook_trust_supported: bool = False,
    which=shutil.which,
    platform: str = os.name,
) -> list[str]:
    resolved = list(codex_command) if codex_command is not None else resolve_codex_command(
        codex_bin,
        which=which,
        platform=platform,
    )
    command = [*resolved, "exec"]
    if hook_trust_supported:
        command.append("--dangerously-bypass-hook-trust")
    command.extend([
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--output-last-message",
        str(last_message_path),
        "-",
    ])
    return command


def run_command_to_file(
    command: Sequence[str],
    output_path: Path,
    *,
    timeout_sec: float | None = None,
    input_text: str | None = None,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        try:
            process = subprocess.run(
                command,
                input=input_text,
                stdin=subprocess.DEVNULL if input_text is None else None,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            handle.write(f"\n[live-semantic-e2e] command timed out after {timeout_sec} seconds\n")
            return 124
    return int(process.returncode)


def _mark_timeout(summary: dict[str, Any], returncode: int) -> dict[str, Any]:
    if returncode == 124:
        summary["inference_status"] = "timeout"
        summary["semantic_status"] = "not-run"
        summary["missing_keys"] = []
        summary["invalid_fields"] = []
        summary["hook_status"] = "not-applicable"
        summary["missing_hooks"] = []
    return summary


def run_scenario(
    runtime: str,
    scenario: LiveScenario,
    out_dir: Path,
    *,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if runtime == "claude":
        stream = out_dir / f"claude-{scenario.id}.jsonl"
        command = build_claude_command(scenario, stream)
        returncode = run_command_to_file(command, stream, timeout_sec=timeout_sec)
        summary = _mark_timeout(parse_claude_stream_json(stream, scenario=scenario), returncode)
        summary["returncode"] = returncode
        summary["scenario_id"] = scenario.id
        summary["output_file"] = str(stream)
        summary["observation_signal"] = observation_signal_from_summary(summary, scenario=scenario)
        return summary
    if runtime == "codex":
        log = out_dir / f"codex-{scenario.id}.log"
        last = out_dir / f"codex-{scenario.id}.txt"
        codex_command = resolve_codex_command("codex")
        command = build_codex_command(
            scenario,
            log,
            last,
            codex_command=codex_command,
            hook_trust_supported=codex_supports_hook_trust(codex_command, cwd=repo_root()),
        )
        returncode = run_command_to_file(
            command,
            log,
            timeout_sec=timeout_sec,
            input_text=scenario.prompt,
        )
        summary = parse_codex_outputs(log, last, returncode=returncode, scenario=scenario)
        summary["scenario_id"] = scenario.id
        summary["output_file"] = str(log)
        summary["last_message_file"] = str(last)
        summary["observation_signal"] = observation_signal_from_summary(summary, scenario=scenario)
        return summary
    raise ValueError(f"unsupported runtime: {runtime}")


def select_scenarios(ids: Iterable[str], *, scenario_source: str = "auto") -> list[LiveScenario]:
    wanted = set(ids)
    scenarios = live_scenarios(scenario_source=scenario_source, limit=None if wanted else DEFAULT_INTENT_SCENARIO_LIMIT)
    if not wanted:
        return scenarios
    selected = [scenario for scenario in scenarios if scenario.id in wanted]
    missing = wanted - {scenario.id for scenario in selected}
    if missing:
        raise SystemExit(f"unknown scenario ids: {sorted(missing)}")
    return selected


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("claude", "codex", "both"), default="codex")
    parser.add_argument("--scenario-source", choices=("auto", "intent", "static"), default="auto")
    parser.add_argument("--scenario-id", action="append", default=[])
    parser.add_argument("--out-dir", type=Path, default=repo_root() / ".tmp" / "live-cli-e2e")
    parser.add_argument("--execute", action="store_true", help="run live model calls")
    parser.add_argument("--jsonl", action="store_true", help="emit one JSON object per scenario result")
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=None,
        help="optional per-scenario live command timeout; omit for long-running jobs",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    scenarios = select_scenarios(args.scenario_id, scenario_source=args.scenario_source)
    runtimes = ("claude", "codex") if args.runtime == "both" else (args.runtime,)
    if not args.execute:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "runtimes": list(runtimes),
                    "scenario_ids": [scenario.id for scenario in scenarios],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    results = []
    for runtime in runtimes:
        for scenario in scenarios:
            result = run_scenario(runtime, scenario, args.out_dir, timeout_sec=args.timeout_sec)
            results.append(result)
            if args.jsonl:
                print(json.dumps({"live_semantic_e2e_result": result}, ensure_ascii=False, sort_keys=True), flush=True)
    if args.jsonl:
        return 0
    print(json.dumps({"live_semantic_e2e": results}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

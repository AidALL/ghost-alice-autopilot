#!/usr/bin/env python3
"""Continuation message formatting for autopilot mode.

Dependencies: Python 3.11+ standard library only.
"""

import re

from typing import Any, Mapping


def _portable_path(text: str, base_path: str | None = None, home_path: str | None = None) -> str:
    # Platform-neutral rendering of a path/command for the continuation signal:
    # backslashes -> forward slashes, then strip the run's project root ("." ) and
    # the home dir ("~") so no drive-absolute or machine-specific prefix leaks
    # across a cross-platform handoff. The audit log keeps the raw value.
    if not text:
        return text
    result = text.replace("\\", "/")
    replacements = []
    if base_path:
        replacements.append((base_path.replace("\\", "/").rstrip("/"), "."))
    if home_path:
        replacements.append((home_path.replace("\\", "/").rstrip("/"), "~"))
    for prefix, token in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
        if prefix:
            result = re.sub(r"(?i)" + re.escape(prefix) + r"(?=/|$)", lambda _m, t=token: t, result)
    return result


def format_io_trace_rows(
    rows: list[dict[str, Any]],
    *,
    base_path: str | None = None,
    home_path: str | None = None,
) -> list[str]:
    lines: list[str] = []
    for row in rows:
        tool = str(row.get("tool") or "unknown")
        path = _portable_path(str(row.get("path") or "n/a"), base_path, home_path)
        op = str(row.get("op") or "")
        raw_pattern = " ".join(str(row.get("pattern") or "").split())
        pattern = _portable_path(raw_pattern, base_path, home_path) if tool == "Bash" else raw_pattern
        if len(pattern) > 180:
            pattern = pattern[:177] + "..."
        if tool == "Bash":
            # Neutral: prefer the structured op+path; never emit the raw shell
            # command (per-runtime tool surface) when it was structured. Fall back
            # to the path-stripped command only when no op could be extracted.
            if op:
                summary = f"{op} {path}" if path and path != "n/a" else op
            else:
                summary = f"{tool} {pattern}".strip() if pattern else tool
        else:
            summary = f"{tool} {path}"
            if pattern:
                summary = f"{summary} {pattern}"
        lines.append(f"- {summary}")
    return lines


def compact_governance_candidate(candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(candidate, Mapping):
        return None
    evidence = candidate.get("evidence")
    compact = {
        "candidate_id": candidate.get("candidate_id"),
        "decision": candidate.get("decision"),
        "source": candidate.get("source"),
        "source_signal_id": candidate.get("source_signal_id"),
        "evidence": [item for item in evidence if isinstance(item, str)] if isinstance(evidence, list) else [],
    }
    return {key: value for key, value in compact.items() if value not in (None, [], "")}


def build_continuation_message(
    run: dict[str, Any],
    item: dict[str, Any],
    *,
    pending_decision: bool = False,
    io_trace_rows: list[dict[str, Any]] | None = None,
    governance_candidate: Mapping[str, Any] | None = None,
    base_path: str | None = None,
    home_path: str | None = None,
) -> str:
    lines = [
        "[autopilot]",
        f"run: {run.get('run_id', 'unknown')}",
        f"work-item: {item['id']}",
        f"focus-layer: {item['focus_layer']}",
    ]
    if pending_decision:
        lines.append("pending-decision: missing")
    if io_trace_rows:
        lines.append("io-trace:")
        lines.extend(format_io_trace_rows(io_trace_rows, base_path=base_path, home_path=home_path))
    compact_candidate = compact_governance_candidate(governance_candidate)
    if compact_candidate:
        lines.append("governance-signal:")
        if "candidate_id" in compact_candidate:
            lines.append(f"- candidate: {compact_candidate['candidate_id']}")
        if "decision" in compact_candidate:
            lines.append(f"- decision: {compact_candidate['decision']}")
        if "source" in compact_candidate:
            lines.append(f"- source: {compact_candidate['source']}")
        evidence = compact_candidate.get("evidence")
        if isinstance(evidence, list) and evidence:
            lines.append("governance-evidence:")
            lines.extend(f"- {value}" for value in evidence)
    source_locator = item.get("source_locator")
    if isinstance(source_locator, str) and source_locator.strip():
        lines.append(f"source-locator: {_portable_path(source_locator.strip(), base_path, home_path)}")
    decision_context = item.get("decision_context")
    if isinstance(decision_context, list) and any(isinstance(value, str) and value for value in decision_context):
        lines.append("decision-context:")
        lines.extend(f"- {value}" for value in decision_context if isinstance(value, str) and value)
    open_questions = item.get("open_questions")
    if isinstance(open_questions, list) and any(isinstance(value, str) and value for value in open_questions):
        lines.append("open-questions:")
        lines.extend(f"- {value}" for value in open_questions if isinstance(value, str) and value)
    lines.append("allowed-surface:")
    lines.extend(f"- {_portable_path(str(surface), base_path, home_path)}" for surface in item["allowed_surface"])
    lines.append("acceptance-criteria:")
    lines.extend(f"- {criterion}" for criterion in item["acceptance_criteria"])
    reopen_target = item.get("completion", {}).get("reopen_target")
    if isinstance(reopen_target, str) and reopen_target:
        lines.append(f"reopen-target: {reopen_target}")
    if item.get("observer_agent_required") is True:
        observer_contract = item.get("observer_contract")
        mode = "read_only"
        if isinstance(observer_contract, dict) and isinstance(observer_contract.get("mode"), str):
            mode = observer_contract["mode"]
        lines.extend(["observer-agent: required", f"observer-mode: {mode}"])
        if isinstance(observer_contract, dict) and isinstance(observer_contract.get("purpose"), str):
            lines.append(f"observer-purpose: {observer_contract['purpose']}")
        prohibited = observer_contract.get("prohibited_actions") if isinstance(observer_contract, dict) else None
        if isinstance(prohibited, list) and all(isinstance(action, str) and action for action in prohibited):
            lines.append("observer-prohibited-actions:")
            lines.extend(f"- {action}" for action in prohibited)
    lines.extend([
        "before-stop:",
        "- continue from the latest io-trace when no promoted consistency decision exists.",
        "- promote a candidate with scripts/autopilot_governance_signal.py promote-decision when a candidate exists.",
        "- otherwise write .autopilot/consistency-decision.json only with the full promoted schema when a completion/retry/reopen decision is resolved.",
        "- promoted schema requires schema_version, decision_id, work_item_id, decision, promotion_state: promoted, promotion_evidence.decision, promotion_evidence.source, candidate_id, governance_signal_digest, decision_key, state_hash, loop_key, and evidence.",
        "- promotion_evidence.decision must be one of go, approve, approved, promote, promoted, or direct; use direct only for a current-turn before-stop resolution without a candidate.",
        "- evidence must be a JSON array of strings; do not nest verdict, completion_check_digest, or text inside evidence.",
        "- for continue_next, put verdict and completion_check_digest at top level and put the full [completion-check] block in evidence strings.",
        "- use continue_next only after [completion-check] with verdict pass, sha256 completion_check_digest, acceptance-criteria, and criterion-bound claim-evidence-map evidence.",
        "- use retry_same_unit or reopen_micro/reopen_meso/reopen_macro when verification fails or drift remains.",
        "- use ask_user_meta only when neither io-trace nor work state can resolve the next action.",
    ])
    lines.extend(["prompt:", item["prompt"]])
    return "\n".join(lines)


def build_meta_intervention_message(
    run: dict[str, Any],
    *,
    work_item_id: str,
    evidence: list[str],
    pending_decision_state: str | None = None,
) -> str:
    lines = [
        "[autopilot]",
        f"run: {run.get('run_id', 'unknown')}",
        f"work-item: {work_item_id}",
    ]
    if pending_decision_state:
        lines.append(f"pending-decision: {pending_decision_state}")
    lines.extend([
        "decision: ask_user_meta",
        "evidence:",
    ])
    lines.extend(f"- {item}" for item in evidence)
    lines.extend([
        "prompt:",
        "Ask the user for a meta-level decision before continuing this autonomous run.",
    ])
    return "\n".join(lines)

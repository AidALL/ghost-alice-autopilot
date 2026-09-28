#!/usr/bin/env python3
"""Continuation message formatting for autopilot mode.

Dependencies: Python 3.11+ standard library only.
"""

import re
import shlex
import sys
from pathlib import Path

from typing import Any, Mapping


def _portable_path(text: str, base_path: str | None = None, home_path: str | None = None) -> str:
    # Platform-neutral rendering of a path/command for the continuation signal: backslashes -> forward slashes, then strip the run's project root ("." ) and the home dir ("~") so no drive-absolute or machine-specific prefix leaks across a cross-platform handoff. The audit log keeps the raw value.
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
        if tool == "Bash":
            # Neutral: prefer the structured op+path; never emit the raw shell command (per-runtime tool surface) when it was structured. Fall back to the path-stripped command only when no op could be extracted.
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


def completion_recovery_guidance() -> str:
    """Carry Core's standalone-answer recovery contract into AP-only errors."""
    return (
        "Keep internal recovery subordinate to the original user goal; it is not a replacement task. "
        "After recovery, write a complete standalone final answer reporting the requested business result. "
        "If this replacement final asserts business completion, include the supported [completion-check] in the final answer as well as in runtime action evidence; an accepted runtime record does not satisfy the final-answer requirement. "
        "Reuse prior business proof only while its relevant criteria, artifacts, and evidence remain unchanged and valid; retain its original source and verification time, and do not describe an earlier check as freshly run. "
        "Preserve the substantive answer and supported evidence; do not repeat completed business operations merely to repair runtime records or final formatting. "
        "Verify changed runtime records separately for new bookkeeping claims; these checks do not refresh business proof or establish business completion. "
        "Preserve Core's skill-call and io-trace requirements truthfully; do not copy a prior turn's skill-call as a current-turn load. "
        "Do not invent evidence or turn a failed/unverified result into a pass. "
        "If a relevant criterion, artifact or evidence changed, failed, is missing or no longer valid, reopen only the affected verification; until supported, report the partial or failed business state honestly without a finalized [completion-check]. "
        "Attribute these requirements to runtime/tool context, not new user authorization."
    )


def completion_command(contract, run_dir, *arguments):
    """Render current installed coordinates; callers supply original proof values."""
    return shlex.join(["env", *(f"{key}={value}" for key, value in {
        "GHOST_ALICE_SESSION_INTENT_ROOT": contract["intent_root"],
        "GHOST_ALICE_PLATFORM": contract["platform"], "GHOST_ALICE_SESSION_ID": contract["session_id"],
        "GHOST_ALICE_AUTOPILOT_RUN_DIR": run_dir}.items()), sys.executable,
        str(Path(__file__).resolve().parents[1] / "scripts/autopilot_completion.py"), *arguments])


def completion_preflight_guidance(origin):
    """Make the existing publication path actionable before business tools."""
    contract = origin["contract"]
    command = completion_command(contract, contract["run_dir"], "prepare", "--reapprove-current-input",
        "--intent-root", contract["intent_root"], "--platform", contract["platform"],
        "--session-id", contract["session_id"], "--input-event-id", contract["input_event_id"])
    return "\n".join([
        "[autopilot]", "completion-preflight: required",
        f"prospective-origin: {origin['origin_token']}", f"prospective-captured-at: {origin['captured_at']}",
        f"criterion-ids: {contract['criterion_ids']}",
        "For the current admitted work, prepare its completion receipt with the command below. After supported verification, publish the unchanged [completion-check] before the first final response using the exact publish_command returned by prepare, replacing ORIGINAL_ISO_TIME with the original verification time and supplying the proof on stdin. Keep that supported block in the final answer too; publication alone does not satisfy the final-answer requirement.",
        command,
        "On the first execution of each necessary check, retain the returned tool output or its stable locator and original verification time. Already returned successful output remains valid evidence while the relevant criteria, artifacts, and evidence are unchanged; cite it directly or copy its existing bytes. A separate store or proof file is optional; do not rerun a successful inspection merely to populate a store, file, trace, or publication. Do not refresh its time or relabel proof that predates this prospective capture. Reopen only affected verification when its relevant state changed or its evidence is missing or invalid.",
        "This runtime/tool notice records provenance only. It does not admit, verify, or complete work; current authorization and the existing Stop completion checks remain in force.",
    ])


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
    if run.get("completion_origin_gap"):
        return "[autopilot]\nprospective-origin: stale\nThe original provenance does not match the current input or contract. No completion was accepted. Preserve the supported business answer and original evidence; do not backdate or relabel it. Only a changed criterion or scope can justify affected verification after a new pretool origin capture."
    publication = run.get("completion_publication")
    if isinstance(publication, Mapping) and publication.get("status") == "prepared":
        receipt = publication.get("receipt") or {}
        if receipt.get("work_item_id") == item["id"]:
            return "\n".join([
                "[autopilot]", f"run: {run.get('run_id', 'unknown')}",
                f"work-item: {item['id']}", "focus-layer: micro",
                "completion-publication: missing",
                f"receipt-token: {publication['receipt_token']}",
                f"prepared-at: {receipt['prepared_at']}",
                f"criterion-ids: {receipt['criterion_ids']}",
                f"approval-generation: {run.get('approval_generation')}",
                "approval-generation-source: previous-tool",
                "approval-generation-origin: autopilot-runtime",
                "The runtime is waiting for a supported completion publication. A missing publication is not evidence that business verification failed or that business work must be repeated.",
                "If the authorized verification already passed after this receipt was prepared, reuse its exact completion block and original timestamp. Supply that timestamp once through --verified-at in the command below and pass the unchanged proof on stdin; do not insert a timestamp into proof that lacks one. Explicit verification time declarations must agree with the original raw string. Keep the returned criterion IDs unchanged. The helper derives the exact reference from exactly one nonempty top-level evidence section in one supported completion block. It does not select nested claim evidence, invent missing references, or authenticate an external verifier output; missing, duplicate, placeholder, or partial evidence stays rejected.",
                completion_command(receipt["contract"], receipt["run_dir"], "publish", "--receipt-token", publication["receipt_token"], "--verified-at", "ORIGINAL_ISO_TIME", "--completion-file", "-"),
                "If verification has not run, perform the necessary authorized check once before publishing. Do not prepare a new receipt to relabel earlier proof, infer success from this message, or refresh the proof time. The helper rejects stale input, criterion, scope, and receipt bindings.",
                "Keep the current user goal, constraints, and protections in force. Publication writes runtime records only; the Stop adapter applies the existing completion transaction.",
                "recovery-and-final-answer:", completion_recovery_guidance(),
            ])
    lines = [
        "[autopilot]",
        f"run: {run.get('run_id', 'unknown')}",
        f"work-item: {item['id']}",
        f"focus-layer: {item['focus_layer']}",
    ]
    if pending_decision:
        lines.append("pending-decision: missing")
    if run.get("approval_generation"):
        lines.append(f"approval-generation: {run['approval_generation']}")
        lines.append("approval-generation-source: previous-tool")
        lines.append("approval-generation-origin: autopilot-runtime")
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
    for key, label in (("contextual_protections", "contextual-protections"),
                       ("constraints", "accumulated-constraints"), ("non_goals", "accumulated-non-goals")):
        values = item.get(key)
        if isinstance(values, list) and values:
            lines.append(f"{label}:")
            lines.extend(f"- {value}" for value in values if isinstance(value, str) and value)
    if any(item.get(key) for key in ("contextual_protections", "constraints", "non_goals")):
        lines.extend([
            "context-use:",
            "- Preserve contextual protections as boundary checks and auxiliary evidence; do not bind unadmitted IDs as met criteria or change admission to satisfy proof.",
            "- Interpret accumulated context with the current goal and newer explicit decisions; do not revive superseded scope or discard protections outside an authorized exception.",
        ])
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
        "- promote an eligible evidence-backed candidate with scripts/autopilot_governance_signal.py promote-decision; preserve its source and approval generation.",
        "- observation_signal candidates are diagnostic; do not promote or relabel them. Continue from observed work and produce a fresh resolved decision only after verification.",
        "- otherwise write .autopilot/consistency-decision.json only with the full promoted schema when a completion/retry/reopen decision is resolved.",
        "- promoted schema requires schema_version, decision_id, work_item_id, decision, promotion_state: promoted, promotion_evidence.decision, promotion_evidence.source, candidate_id, governance_signal_digest, decision_key, state_hash, loop_key, and evidence.",
        "- session-bound actions must preserve approval_generation from the approval receipt used to produce the evidence; never relabel old evidence with a newer generation.",
        "- promotion_evidence.decision must be one of go, approve, approved, promote, promoted, or direct; use direct only for a current-turn before-stop resolution without a promotable candidate.",
        "- evidence must be a JSON array of strings; do not nest verdict, completion_check_digest, or text inside evidence.",
        "- for continue_next, put verdict and completion_check_digest at top level and put the full [completion-check] block in evidence strings.",
        "- use continue_next only after [completion-check] with verdict pass, sha256 completion_check_digest, acceptance-criteria, and criterion-bound claim-evidence-map evidence.",
        "- use retry_same_unit or reopen_micro/reopen_meso/reopen_macro when verification fails or drift remains.",
        "- use ask_user_meta only when neither io-trace nor work state can resolve the next action.",
    ])
    lines.extend(["recovery-and-final-answer:", completion_recovery_guidance(), "prompt:", item["prompt"]])
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


def build_semantic_delta_starvation_message(event: Mapping[str, Any]) -> str:
    lines = [
        "[autopilot]",
        "decision: reopen_macro",
        "evidence:",
        f"- semantic-delta-starvation: {event.get('digest_only_count', 0)} consecutive digest-only user inputs",
    ]
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        lines.append(f"- session_id: {session_id.strip()}")
    latest_event_id = event.get("latest_event_id")
    if isinstance(latest_event_id, str) and latest_event_id.strip():
        lines.append(f"- latest_event_id: {latest_event_id.strip()}")
    state_path = event.get("state_path")
    if isinstance(state_path, str) and state_path.strip():
        lines.append(f"- state_path: {state_path.strip()}")
    lines.extend([
        "prompt:",
        "Before stopping, run the session-intent-analyzer recovery path for the current turn: record the current goal, constraints, decisions, acceptance criteria, and open conduct feedback as a semantic delta, then continue the actual work from that updated intent state.",
    ])
    return "\n".join(lines)

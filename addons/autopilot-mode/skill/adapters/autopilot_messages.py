#!/usr/bin/env python3
"""Continuation message formatting for Ghost-ALICE Autopilot.

Dependencies: Python 3.11+ standard library only.
"""

import re
import os
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
            # The formatter emits structured op+path when available; otherwise the formatter emits the command with machine-specific path prefixes removed.
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


def completion_recovery_guidance(host_input=None, source=None, *, claims_changed=None) -> str:
    """Return Core delivery guidance for the host observation; preserve original business proof."""
    from autopilot_runtime_context import core_recovery_delivery
    return core_recovery_delivery(host_input, source, claims_changed=claims_changed)['guidance']



def completion_command(contract, run_dir, *arguments, target_receipt=None, source=None):
    """Bind commands to immutable receipts, never a caller-asserted origin flag."""
    coordinates = {
        'GHOST_ALICE_SESSION_INTENT_ROOT': contract['intent_root'],
        'GHOST_ALICE_PLATFORM': contract['platform'], 'GHOST_ALICE_SESSION_ID': contract['session_id'],
        'GHOST_ALICE_AUTOPILOT_RUN_DIR': run_dir}
    current = dict(os.environ if source is None else source)
    if core_root := current.get('GHOST_ALICE_CORE_ROOT'):
        if not Path(core_root).is_absolute():
            raise ValueError('Core source location must be absolute')
        coordinates['GHOST_ALICE_CORE_ROOT'] = core_root
    for key in ('GHOST_ALICE_GHOST_ROOT','GHOST_ALICE_OWNED_ROOT','GHOST_ALICE_PLATFORM_DIR','GHOST_ALICE_SKILLS_DIR','GHOST_ALICE_PROJECT_CWD','GHOST_ALICE_RUNTIME_SHARED_DIR'):
        if current.get(key): coordinates[key] = current[key]
    coordinates['PYTHONDONTWRITEBYTECODE'] = '1'
    args = list(arguments)
    if target_receipt is not None:
        coordinates['GHOST_ALICE_COMPLETION_TARGET_RECEIPT'] = target_receipt
        coordinates['GHOST_ALICE_COMPLETION_SCHEMA'] = 'autopilot-completion-receipt.v2'
        args.extend(['--schema-version', 'autopilot-completion-receipt.v2'])
    return shlex.join(['env', *(f'{key}={value}' for key, value in coordinates.items()), sys.executable,
        str(Path(__file__).resolve().parents[1] / 'scripts/autopilot_completion.py'), *args])


def completion_publication_scope_guidance():
    """Scope this returned publication instruction to the legacy v1 caller; native v2 publishes at Stop."""
    return (
        "publication-protocol: legacy-v1. The current-session assistant follows installed verification-before-completion/references/autopilot-publication.md, section Explicit v1 callers, when the Ghost-ALICE Core completion service requires publication for the candidate response. "
        "The returned command binds this selected receipt; publication status is separate from Core response acceptance."
    )



def completion_preflight_guidance(origin):
    """Return the legacy v1 receipt-preparation command for the admitted execution contract."""
    contract = origin["contract"]
    command = completion_command(contract, contract["run_dir"], "prepare", "--reapprove-current-input",
        "--intent-root", contract["intent_root"], "--platform", contract["platform"],
        "--session-id", contract["session_id"], "--input-event-id", contract["input_event_id"])
    return "\n".join([
        "[autopilot]", "completion-preflight: required", "publication-protocol: legacy-v1",
        f"prospective-origin: {origin['origin_token']}", f"prospective-captured-at: {origin['captured_at']}",
        f"criterion-ids: {contract['criterion_ids']}",
        completion_publication_scope_guidance(),
        "The current-session assistant requests the native host shell tool to run this receipt-preparation command. Its result supplies the publication command; the Core owner procedure specifies original verification time and proof inputs.",
        command,
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
    host_input: Mapping[str, Any] | None = None,
    source: Mapping[str, str] | None = None,
) -> str:
    if run.get("completion_origin_gap"):
        return "[autopilot]\nprospective-origin: stale\nGhost-ALICE Autopilot recording code rejected completion recording because the original provenance does not match the current input or contract. The current-session assistant retains the supported business answer and original proof/source/time. The current-session assistant following verification-before-completion may verify changed criteria or scope after a new pretool capture; a provenance mismatch alone does not justify replaying business work or relabelling earlier proof."
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
                "Ghost-ALICE Autopilot has no accepted publication under this receipt. The current-session assistant follows installed verification-before-completion/references/autopilot-publication.md, section Explicit v1 callers, for a required publication using original proof and time.",
                completion_command(receipt["contract"], receipt["run_dir"], "publish", "--receipt-token", publication["receipt_token"], "--verified-at", "ORIGINAL_ISO_TIME", "--completion-file", "-"),
                "recovery-and-final-answer:", completion_recovery_guidance(host_input, source),
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
            "- The current-session assistant uses contextual protections as boundary checks and auxiliary evidence; only Core-admitted criterion IDs can receive applied proof.",
            "- The current-session assistant applies the current goal and newer explicit decisions to accumulated context; superseded scope remains inactive and protections remain binding unless the user authorized an exception.",
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
        "- The current-session assistant judges the next necessary authorized action from the displayed task and evidence; tool-trace presence supplies no semantic mismatch or focus decision.",
        "- For a resolved queue action, follow the Approved queue API section of installed Ghost-ALICE Autopilot SKILL.md. That procedure owns promotion and the consistency-decision.json in the selected run directory, including original approval_generation and evidence bindings.",
        "- Core verification-before-completion owns claim review; task-router selects judgment focus and verification depth; boundary-contract and systematic-debugging own authority and incident limits.",
    ])
    lines.extend(["recovery-and-final-answer:", completion_recovery_guidance(host_input, source), "prompt:", item["prompt"]])
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
        "The current-session assistant asks the user for the unresolved decision identified by the displayed evidence before resuming the displayed run and work-item.",
    ])
    return "\n".join(lines)

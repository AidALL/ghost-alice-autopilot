#!/usr/bin/env python3
"""Work-item validation and transition helpers for autopilot mode.

Dependencies: Python 3.11+ standard library only.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
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
READY_QUEUE_STATUSES = frozenset({"ready", "reopened"})
ALLOWED_DECISIONS = frozenset({
    "continue_next",
    "retry_same_unit",
    "reopen_micro",
    "reopen_meso",
    "reopen_macro",
    "ask_user_meta",
    "stop",
})
CONDUCT_PLAN_SCHEMA = "autopilot-conduct-plan.v2"
COMPLETION_CHECK_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
TOP_LEVEL_FIELD_RE = re.compile(r"^-\s*[A-Za-z0-9_-]+\s*:")
ACCEPTANCE_ID_RE = re.compile(r"^\s*-\s*([A-Za-z0-9_.=-]+)\s*:")
CLAIM_RE = re.compile(r"^\s*-\s*claim\s*:\s*(.+?)\s*$", re.I)
CRITERION_RE = re.compile(r"^\s*criterion\s*:\s*(.+?)\s*$", re.I)
VERDICT_RE = re.compile(r"^\s*verdict\s*:\s*(.+?)\s*$", re.I)
APPROVAL_DECISIONS = frozenset({"go", "approve", "approved", "auto"})


class AutopilotStateError(ValueError):
    """Raised when autopilot work-item state is invalid."""


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutopilotStateError(f"{field} must be a non-empty string")
    return value


def _validate_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise AutopilotStateError(f"{field} must be a string array")
    return list(value)


def _extract_top_level_section(text: str, field_name: str) -> str:
    lines = text.splitlines()
    field_pattern = re.compile(r"^-\s*" + re.escape(field_name) + r"\s*:", re.I)
    start = -1
    for index, line in enumerate(lines):
        if field_pattern.search(line):
            start = index
            break
    if start < 0:
        return ""

    kept = []
    for index in range(start + 1, len(lines)):
        if TOP_LEVEL_FIELD_RE.search(lines[index]):
            break
        kept.append(lines[index])
    return "\n".join(kept).strip()


def _extract_completion_acceptance_ids(evidence_text: str) -> set[str]:
    section = _extract_top_level_section(evidence_text, "acceptance-criteria")
    ids: set[str] = set()
    for line in section.splitlines():
        match = ACCEPTANCE_ID_RE.match(line)
        if match and "<" not in match.group(1):
            ids.add(match.group(1))
    return ids


def _extract_completion_claim_criteria(evidence_text: str) -> list[str]:
    section = _extract_top_level_section(evidence_text, "claim-evidence-map")
    criteria: list[str] = []
    current_claim = False
    for line in section.splitlines():
        if CLAIM_RE.match(line):
            current_claim = True
            criteria.append("")
            continue
        if current_claim:
            match = CRITERION_RE.match(line)
            if match:
                criteria[-1] = match.group(1).strip()
    return criteria


def _extract_completion_claim_verdicts(evidence_text: str) -> list[str]:
    section = _extract_top_level_section(evidence_text, "claim-evidence-map")
    verdicts: list[str] = []
    current_claim = False
    for line in section.splitlines():
        if CLAIM_RE.match(line):
            current_claim = True
            verdicts.append("")
            continue
        if current_claim:
            match = VERDICT_RE.match(line)
            if match:
                verdicts[-1] = match.group(1).strip().lower()
    return verdicts


def _extract_unverified_items(evidence_text: str) -> list[str]:
    section = _extract_top_level_section(evidence_text, "unverified")
    items: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        item = stripped[1:].strip()
        if item:
            items.append(item)
    return items


def _validate_completion_claim_criteria(evidence_text: str) -> None:
    acceptance_ids = _extract_completion_acceptance_ids(evidence_text)
    claim_criteria = _extract_completion_claim_criteria(evidence_text)
    if not acceptance_ids:
        raise AutopilotStateError(
            "continue_next evidence must include acceptance-criteria criterion ids for completion-check claims"
        )
    if not claim_criteria:
        raise AutopilotStateError("continue_next evidence must include claim-evidence-map entries")
    for criterion in claim_criteria:
        criterion_ids = [token for token in re.split(r"[,\s]+", criterion.strip()) if token]
        if not criterion_ids or any(token not in acceptance_ids for token in criterion_ids):
            raise AutopilotStateError(
                "continue_next evidence claim-evidence-map entries must reference acceptance-criteria criterion ids"
            )


def _validate_completion_claim_outcomes(evidence_text: str) -> None:
    claim_verdicts = _extract_completion_claim_verdicts(evidence_text)
    if not claim_verdicts or any(verdict != "pass" for verdict in claim_verdicts):
        raise AutopilotStateError("continue_next evidence claim-evidence-map verdicts must all be pass")
    unverified_items = _extract_unverified_items(evidence_text)
    if not unverified_items:
        raise AutopilotStateError("continue_next evidence must include unverified: none")
    if any(item.strip().lower() != "none" for item in unverified_items):
        raise AutopilotStateError("continue_next evidence unverified items must be none")


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


def _validate_continue_next_evidence(
    completion_check_digest: str | None,
    evidence: list[str] | None,
) -> list[str]:
    if not isinstance(completion_check_digest, str) or not COMPLETION_CHECK_DIGEST_PATTERN.fullmatch(
        completion_check_digest
    ):
        raise AutopilotStateError("continue_next requires a sha256 completion-check digest")
    validated = _validate_string_list(evidence, "continue_next evidence")
    evidence_text = "\n".join(validated)
    if "[completion-check]" not in evidence_text or "claim-evidence-map" not in evidence_text:
        raise AutopilotStateError("continue_next evidence must include a [completion-check] claim-evidence-map")
    _validate_completion_claim_criteria(evidence_text)
    _validate_completion_claim_outcomes(evidence_text)
    return validated


def _validate_non_continuation_evidence(decision: str, evidence: list[str] | None) -> list[str]:
    validated = _validate_string_list(evidence, f"{decision} evidence")
    if not validated:
        raise AutopilotStateError("non-continuation consistency decisions require evidence")
    return validated


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
        if item["status"] not in READY_QUEUE_STATUSES:
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
        if verdict != "pass":
            raise AutopilotStateError("continue_next requires passing completion-check evidence")
        validated_evidence = _validate_continue_next_evidence(completion_check_digest, evidence)
        item["status"] = "completed"
        item["completion"].update({
            "state": "completed",
            "verdict": verdict,
            "evidence": validated_evidence,
            "completion_check_digest": completion_check_digest,
            "reopen_target": None,
        })
    elif decision == "retry_same_unit":
        validated_evidence = _validate_non_continuation_evidence(decision, evidence)
        item["status"] = "ready"
        item["attempt"] += 1
        item["completion"].update({
            "state": "retry",
            "verdict": None,
            "evidence": validated_evidence,
            "completion_check_digest": completion_check_digest,
            "reopen_target": None,
        })
    elif decision.startswith("reopen_"):
        validated_evidence = _validate_non_continuation_evidence(decision, evidence)
        target = decision.removeprefix("reopen_")
        item["status"] = "reopened"
        item["completion"].update({
            "state": "reopened",
            "verdict": None,
            "evidence": validated_evidence,
            "completion_check_digest": completion_check_digest,
            "reopen_target": target,
        })
    elif decision in {"ask_user_meta", "stop"}:
        validated_evidence = _validate_non_continuation_evidence(decision, evidence)
        item["status"] = "stopped"
        item["completion"].update({
            "state": decision,
            "verdict": None,
            "evidence": validated_evidence,
            "completion_check_digest": completion_check_digest,
            "reopen_target": "meta" if decision == "ask_user_meta" else None,
        })
    return validate_work_items(updated)


def _load_core_ledger_module(state_path: Path, source: Mapping[str, str] | None = None):
    """Resolve and import the core session-intent ledger by path (B.3 hybrid).

    Tries an explicit core root from the environment, then walks up from the
    ledger state file to find a core repo checkout. Returns None when no core
    source is reachable (e.g. a data-only installed layout) so the caller can
    skip the met-flip gracefully instead of crashing.
    """
    candidates: list[Path] = []
    if source is not None:
        core_root = str(source.get("GHOST_ALICE_CORE_ROOT") or "").strip()
        if core_root:
            candidates.append(
                Path(core_root) / "session-intent-analyzer" / "scripts" / "session_intent_ledger.py"
            )
    for parent in Path(state_path).resolve().parents:
        candidates.append(parent / "session-intent-analyzer" / "scripts" / "session_intent_ledger.py")
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            spec = importlib.util.spec_from_file_location(
                "ghost_alice_session_intent_ledger", candidate
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception:
            continue
    return None


def materialize_met_criteria_from_continue_next(
    run: Mapping[str, Any],
    applied_decision: Mapping[str, Any],
    source: Mapping[str, str] | None = None,
) -> list[str]:
    """Flip the satisfied admitted criteria to "met" after a validated continue_next.

    Hybrid B.3: the core ledger owns the write-only "met" invariant; the adapter
    only calls it, using the same validated completion-check digest that
    continue_next already required. Any resolution or flip failure is a graceful
    skip (the criterion stays unmet, so the run keeps going) rather than a crash
    or a premature stop.
    """
    approval = run.get("approval_evidence")
    if not isinstance(approval, Mapping):
        return []
    session_intent = approval.get("session_intent")
    if not isinstance(session_intent, Mapping):
        return []
    state_path_raw = session_intent.get("state_path")
    if not isinstance(state_path_raw, str) or not state_path_raw:
        return []
    raw_digest = str(applied_decision.get("completion_check_digest") or "").strip()
    if not COMPLETION_CHECK_DIGEST_PATTERN.fullmatch(raw_digest):
        return []
    core_digest = raw_digest[len("sha256:"):] if raw_digest.startswith("sha256:") else raw_digest
    evidence = applied_decision.get("evidence")
    if not isinstance(evidence, list):
        return []
    # A single claim may bind several criteria ("criterion: AC1, AC2"); split the
    # same way the continue_next validator does so each real ledger id is flipped.
    raw_criteria = _extract_completion_claim_criteria("\n".join(str(line) for line in evidence))
    criterion_ids: list[str] = []
    for raw in raw_criteria:
        for token in re.split(r"[,\s]+", str(raw).strip()):
            if token and token not in criterion_ids:
                criterion_ids.append(token)
    if not criterion_ids:
        return []
    state_path = Path(state_path_raw)
    ledger = _load_core_ledger_module(state_path, source)
    if ledger is None or not hasattr(ledger, "mark_acceptance_criterion_met"):
        return []
    intent_root = state_path.parent.parent.parent
    platform = state_path.parent.parent.name
    session_id = state_path.parent.name
    flipped: list[str] = []
    for criterion_id in criterion_ids:
        try:
            ledger.mark_acceptance_criterion_met(
                root=intent_root,
                platform=platform,
                session_id=session_id,
                criterion_id=criterion_id,
                completion_check_digest=core_digest,
            )
            flipped.append(criterion_id)
        except Exception:
            continue
    return flipped


def rewrite_open_work_items(
    items: Iterable[dict[str, Any]],
    replacement_items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    current = validate_work_items(copy.deepcopy(list(items)))
    terminal = [item for item in current if item["status"] in TERMINAL_STATUSES]
    replacements = copy.deepcopy(list(replacement_items))
    return validate_work_items([*terminal, *replacements])


def _has_explicit_approval_evidence(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    decision = str(value.get("decision") or "").strip().lower()
    source = value.get("source")
    return decision in APPROVAL_DECISIONS and isinstance(source, str) and bool(source.strip())


def apply_conduct_plan_proposals(
    items: Iterable[dict[str, Any]],
    plan: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if plan.get("schema_version") != CONDUCT_PLAN_SCHEMA:
        raise AutopilotStateError(f"conduct plan schema_version must be {CONDUCT_PLAN_SCHEMA!r}")
    if plan.get("promotion_state") != "approved":
        raise AutopilotStateError("conduct plan promotion_state must be 'approved'")
    _require_string(plan.get("source_candidate_id"), "conduct plan source_candidate_id")
    evidence_digest = _require_string(plan.get("evidence_digest"), "conduct plan evidence_digest")
    if not COMPLETION_CHECK_DIGEST_PATTERN.fullmatch(evidence_digest):
        raise AutopilotStateError("conduct plan evidence_digest must be a sha256 digest")
    if not _has_explicit_approval_evidence(plan.get("approval_evidence")):
        raise AutopilotStateError("conduct plan approval_evidence must include an explicit approval decision and source")
    proposed = plan.get("proposed_queue_items")
    if not isinstance(proposed, list):
        raise AutopilotStateError("conduct plan proposed_queue_items must be an array")

    updated = validate_work_items(copy.deepcopy(list(items)))
    known_ids = {item["id"] for item in updated}
    new_items: list[dict[str, Any]] = []

    for index, raw_proposal in enumerate(proposed):
        if not isinstance(raw_proposal, dict):
            raise AutopilotStateError(f"conduct plan proposed_queue_items[{index}] must be an object")
        proposal = raw_proposal
        proposal_id = _require_string(proposal.get("id"), f"conduct proposal {index} id")
        if proposal.get("proposal_status") != "proposed":
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} proposal_status must be 'proposed'")
        if proposal.get("approval_required") is not True:
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} approval_required must be true")
        transition = proposal.get("approval_transition")
        if not isinstance(transition, dict):
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} approval_transition must be an object")
        if transition.get("copy_task_template") is not True:
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} must copy task_template on approval")
        if transition.get("status_on_approval") != "ready":
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} status_on_approval must be 'ready'")
        template = proposal.get("task_template")
        if not isinstance(template, dict):
            raise AutopilotStateError(f"conduct proposal {proposal_id!r} task_template must be an object")

        item_id = _require_string(template.get("id"), f"conduct proposal {proposal_id!r} task_template id")
        if item_id in known_ids:
            continue
        item = copy.deepcopy(template)
        item["status"] = "ready"
        item.setdefault("depends_on", [])
        item.setdefault("attempt", 0)
        item.setdefault("completion", {})
        item["source_plan_schema"] = CONDUCT_PLAN_SCHEMA
        item["source_plan_source"] = plan.get("source")
        item["source_plan_candidate_id"] = plan.get("source_candidate_id")
        item["source_plan_evidence_digest"] = plan.get("evidence_digest")
        item["source_plan_approval_evidence"] = copy.deepcopy(plan.get("approval_evidence"))
        item["source_proposal_id"] = proposal_id
        item["source_recommendation_id"] = proposal.get("source_recommendation_id")
        if proposal.get("observer_agent_required") is True:
            item["observer_agent_required"] = True
            observer_contract = proposal.get("observer_contract")
            if observer_contract is not None and not isinstance(observer_contract, dict):
                raise AutopilotStateError(
                    f"conduct proposal {proposal_id!r} observer_contract must be an object when present"
                )
            if isinstance(observer_contract, dict):
                item["observer_contract"] = copy.deepcopy(observer_contract)
        new_items.append(item)
        known_ids.add(item_id)

    return validate_work_items([*updated, *new_items])

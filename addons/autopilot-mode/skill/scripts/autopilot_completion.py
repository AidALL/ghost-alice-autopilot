#!/usr/bin/env python3
"""Capture provenance before verification and publish its unchanged proof.

Dependencies: Python 3.11+ standard library, sibling AP adapters, and the bound
Core SQLite storage runtime. This helper never runs a business check or marks a
criterion met. The caller supplies supported proof; Stop validates and commits
the existing continue_next transaction. A receipt is provenance, not truth.

prepare --intent-root ROOT --platform codex --session-id ID --input-event-id ID
publish --receipt-token TOKEN --verified-at ISO_TIME --completion-file -
                                            # exact completion block on stdin

Both operations use the configured Stop run target and current session. Runtime
state stays in the AP authority; no business scratch file is required. Timestamps
and receipt tokens are strings. Prepare must precede the original verification.
"""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any, Mapping

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters"))
import autopilot_state as adapter
import autopilot_storage as storage
from autopilot_work_items import _extract_top_level_section, _parse_completion_evidence, _validate_continue_next_evidence

PUBLICATION_OBJECT = "completion-publication.json"
RECEIPT_SCHEMA = "autopilot-completion-receipt.v1"
PROVENANCE_SCHEMA = "autopilot-completion-provenance.v1"
SUPPORTED_PLATFORMS = ("codex", "claude")


def _require_supported_platform(platform: str) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("completion publication supports only codex and claude; retain the existing workflow on other platforms")


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("verification time must be an original ISO timestamp string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("verification time must be an original ISO timestamp string") from exc
    if result.tzinfo is None:
        raise ValueError("verification time requires an explicit timezone")
    return result


def _definitions(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("missing approved criterion definitions")
    result = [{key: row.get(key) for key in ("id", "summary", "source", "admitted")}
              for row in rows if isinstance(row, Mapping)]
    if len(result) != len(rows) or len({row["id"] for row in result}) != len(result):
        raise ValueError("invalid or ambiguous criterion definitions")
    return sorted(result, key=lambda row: str(row["id"]))


def _source(source: Mapping[str, str] | None) -> dict[str, str]:
    supplied = dict(os.environ if source is None else source)
    if supplied.get("GHOST_ALICE_PLATFORM"):
        _require_supported_platform(supplied["GHOST_ALICE_PLATFORM"])
    if (supplied.get("GHOST_ALICE_PLATFORM") == "codex" and supplied.get("CODEX_THREAD_ID")
            and supplied.get("GHOST_ALICE_SESSION_ID")
            and supplied["CODEX_THREAD_ID"] != supplied["GHOST_ALICE_SESSION_ID"]):
        raise ValueError("explicit completion session disagrees with current native session")
    current = adapter.current_session_context(supplied) or {}
    if (not current.get("GHOST_ALICE_SESSION_INTENT_ROOT")
            and current.get("GHOST_ALICE_PLATFORM") in SUPPORTED_PLATFORMS
            and current.get("GHOST_ALICE_SESSION_ID")):
        # Use the same current-session candidates as Stop, never a discovery
        # pointer or another session. Reads do not bootstrap or migrate state.
        for candidate in adapter._session_intent_root_candidates(current, adapter._project_cwd_from_env(current)):
            if not (candidate / "ghost-state.sqlite3").is_file():
                continue
            try:
                adapter.read_session_material(candidate, current["GHOST_ALICE_PLATFORM"],
                    current["GHOST_ALICE_SESSION_ID"], source=current, require_input=True, recover_audit=False)
            except FileNotFoundError:
                continue
            current["GHOST_ALICE_SESSION_INTENT_ROOT"] = str(candidate.resolve())
            break
    for key in ("GHOST_ALICE_SESSION_INTENT_ROOT", "GHOST_ALICE_PLATFORM", "GHOST_ALICE_SESSION_ID"):
        if not current.get(key):
            raise ValueError(f"completion publication needs explicit current {key}")
    _require_supported_platform(current["GHOST_ALICE_PLATFORM"])
    return current


def _current_contract(run: Mapping[str, Any], source: Mapping[str, str]) -> dict[str, Any]:
    binding = (run.get("approval_evidence") or {}).get("session_intent")
    if not isinstance(binding, Mapping):
        raise ValueError("completion publication requires a session-bound run")
    platform, session = source["GHOST_ALICE_PLATFORM"], source["GHOST_ALICE_SESSION_ID"]
    if platform != binding.get("platform") or session != binding.get("session_id"):
        raise ValueError("completion receipt belongs to a different session")
    intent_root = Path(source["GHOST_ALICE_SESSION_INTENT_ROOT"]).expanduser().resolve()
    if intent_root != Path(binding["state_path"]).parent.parent.parent.resolve():
        raise ValueError("completion receipt belongs to a different intent authority")
    material = adapter.read_session_material(intent_root, platform, session, source=source, require_input=True)
    state = material["intent_state"]
    original_input = binding.get("latest_input_event") or {}
    current_input = material["latest_input_event"]
    if any(original_input.get(key) != current_input.get(key) for key in ("event_id", "input_digest")):
        raise ValueError("completion input changed; reapprove current work before new verification")
    criteria = _definitions(state.get("acceptance_criteria"))
    if criteria != _definitions(binding.get("acceptance_criteria")):
        raise ValueError("completion criterion changed; reapprove current work before new verification")
    if adapter.SESSION_MATERIAL.run_summary(state) != (run.get("scope") or {}).get("summary"):
        raise ValueError("completion scope changed; reapprove current work before new verification")
    return {"platform": platform, "session_id": session, "intent_root": str(intent_root),
        "input_event_id": current_input["event_id"], "input_digest": current_input.get("input_digest"),
        "acceptance_criteria": criteria, "scope": copy.deepcopy(run.get("scope")),
        "allowed_surfaces": copy.deepcopy(run.get("allowed_surfaces")),
        "stop_conditions": copy.deepcopy(run.get("stop_conditions")),
        "constraints": copy.deepcopy(state.get("constraints", [])),
        "non_goals": copy.deepcopy(state.get("non_goals", [])),
        "decisions": copy.deepcopy(state.get("decisions", [])),
        "latest_scope": copy.deepcopy(state.get("latest_scope", {})),
        "approval_generation": adapter.SESSION_MATERIAL.approval_generation(run)}


def _receipt_output(record: Mapping[str, Any]) -> dict[str, Any]:
    receipt = record["receipt"]
    from autopilot_messages import completion_command
    return {"receipt_token": record["receipt_token"], "prepared_at": receipt["prepared_at"],
        "approval_generation": receipt["contract"]["approval_generation"],
        "input_event_id": receipt["contract"]["input_event_id"],
        "criterion_ids": receipt["criterion_ids"], "work_item_id": receipt["work_item_id"],
        "run_dir": receipt["run_dir"], "status": "prepared",
        "publish_command": completion_command(receipt["contract"], receipt["run_dir"], "publish",
            "--receipt-token", record["receipt_token"], "--verified-at", "ORIGINAL_ISO_TIME", "--completion-file", "-")}


def _require_requested_input(contract: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    if (contract.get("input_event_id") != expected.get("event_id")
            or contract.get("input_digest") != expected.get("input_digest")):
        raise ValueError("input changed during completion preparation; original caller receipt is stale")


def prepare_completion(*, intent_root: Path, platform: str, session_id: str,
                       input_event_id: str, source: Mapping[str, str] | None = None,
                       reapprove_current_input: bool = False) -> dict[str, Any]:
    """Capture the receipt once, before the caller performs business verification."""
    _require_supported_platform(platform)
    current = dict(os.environ if source is None else source)
    # Arguments are original intake coordinates, never values copied from an old run.
    supplied = {"GHOST_ALICE_SESSION_INTENT_ROOT": str(Path(intent_root).expanduser().resolve()),
               "GHOST_ALICE_PLATFORM": platform, "GHOST_ALICE_SESSION_ID": session_id}
    for key, value in supplied.items():
        observed = current.get(key)
        if observed and key == "GHOST_ALICE_SESSION_INTENT_ROOT":
            observed = str(Path(observed).expanduser().resolve())
        if observed and observed != value:
            raise ValueError(f"supplied completion coordinate disagrees with current {key}")
        current[key] = value
    current = _source(current)
    if current["GHOST_ALICE_SESSION_ID"] != session_id:
        raise ValueError("supplied completion session differs from the firing session")
    material = adapter.read_session_material(Path(intent_root), platform, session_id, source=current,
                                            require_input=True, expected_input_event_id=input_event_id)
    target = adapter.resolve_run_target(current)
    root = Path(target["run_dir"]).expanduser().resolve()
    if (root / adapter.OFF_FILE).exists():
        raise ValueError("Autopilot is OFF; completion publication was not prepared")
    plan = str(current.get("GHOST_ALICE_AUTOPILOT_PLAN_PATH") or
               adapter._project_cwd_from_env(current) / ".tmp/implementation-plans/autopilot-session-intent.md")
    reapprove = reapprove_current_input
    if reapprove and adapter._run_state_available(root):
        with storage.transaction(root, current) as store:
            try:
                existing = store.read(PUBLICATION_OBJECT)
            except FileNotFoundError:
                existing = None
            try:
                unchanged = _current_contract(store.run(), current)
            except ValueError:
                unchanged = None
            if unchanged is not None:
                _require_requested_input(unchanged, material["latest_input_event"])
                if existing is not None and existing["receipt"]["contract"] == unchanged:
                    return _receipt_output(existing)
                if existing is None:
                    expected_scope = {"summary": unchanged["scope"]["summary"], "completion_contract": {
                        key: unchanged[key] for key in ("constraints", "non_goals", "decisions")}}
                    if unchanged["scope"].get("publication_mode") == "session-intent-task":
                        expected_scope.update(publication_mode="session-intent-task", latest_scope=unchanged["latest_scope"])
                    elif unchanged["latest_scope"]:
                        expected_scope = None  # Legacy scope did not bind this semantic field.
                    if unchanged["scope"] == {"summary": unchanged["scope"]["summary"]}:
                        # A summary-only admission can still be proved unchanged
                        # by its authoritative revision. Never invent its prior
                        # semantic fields after an intervening ledger update.
                        latest = adapter.read_session_material(Path(intent_root), platform, session_id,
                            source=current, require_input=True, recover_audit=False)["intent_state"]
                        revision = store.run()["approval_evidence"]["session_intent"].get("ledger_revision")
                        if isinstance(revision, int) and revision == latest.get("ledger_revision"):
                            expected_scope = unchanged["scope"]
                    try:
                        items = adapter.read_work_items(root / adapter.TASKS_FILE)
                    except FileNotFoundError:
                        items = []
                    active = [item for item in items if item["status"] in {"ready", "running", "reopened"}]
                    # Keep a compatible ordinary admission and its origin. A
                    # conduct-only partial admission has no such business task.
                    if (unchanged["scope"] == expected_scope
                            and unchanged["allowed_surfaces"] == [plan]
                            and unchanged["stop_conditions"] == list(adapter.DEFAULT_STOP_CONDITIONS)
                            and not storage.pending(root / adapter.CONDUCT_PLAN_FILE)
                            and len(active) == 1 and active[0]["id"] == "session-intent-" + adapter.SESSION_MATERIAL.safe_id(session_id)
                            and active[0]["acceptance_criteria"] == adapter.SESSION_MATERIAL.acceptance_criteria_from_intent(
                                {"acceptance_criteria": store.run()["approval_evidence"]["session_intent"]["acceptance_criteria"]})):
                        reapprove = False
    if reapprove:
        if adapter.unmet_admitted_criteria_evidence(material["intent_state"]) is None:
            raise ValueError("reapproval requires current admitted unmet criteria for authorized work")
        # Use the same explicit admission boundary as the supported bridge. Old
        # generations and publications remain immutable in database history.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from autopilot_session_bridge import bridge_session_intent_to_run_state
        bridge_session_intent_to_run_state(intent_root=Path(intent_root), platform=platform,
            session_id=session_id, input_event_id=input_event_id, run_dir=root,
            current_work_item_id="session-intent-" + adapter.SESSION_MATERIAL.safe_id(session_id),
            plan_path=plan, source=current, bind_completion_contract=True,
            approval_evidence={"decision": "approved", "source": "user-authorized-current-input",
                               "input_event_id": input_event_id})
    if not adapter._run_state_available(root):
        adapter._bootstrap_from_session_intent_if_approved(root, current,
            adapter._project_cwd_from_env(current), expected_input_event=material["latest_input_event"])
    if not adapter._run_state_available(root):
        raise ValueError("no admitted current-session work for completion publication")
    with storage.transaction(root, current) as store:
        run = store.run()
        contract = _current_contract(run, current)
        _require_requested_input(contract, material["latest_input_event"])
        try:
            record = store.read(PUBLICATION_OBJECT)
        except FileNotFoundError:
            record = None
        if record is not None:
            if record["receipt"]["contract"] != contract:
                raise ValueError("prepared completion contract changed; old proof cannot be relabeled")
            return _receipt_output(record)
        items = adapter.read_work_items(root / adapter.TASKS_FILE)
        from autopilot_provenance import bind_origin
        promoted = bind_origin(root, run, items, current)
        if promoted.get("completion_publication"):
            return _receipt_output(promoted["completion_publication"])
        if promoted.get("completion_origin_gap"):
            raise ValueError("prospective origin is stale; do not relabel existing proof")
        active = [item for item in items if item["status"] in {"ready", "running", "reopened"}]
        expected_id = "session-intent-" + adapter.SESSION_MATERIAL.safe_id(session_id)
        if len(active) != 1 or active[0]["id"] != expected_id:
            raise ValueError("completion preparation requires the single session-intent task; use explicit work-item decisions for other runs")
        approved_rows = run["approval_evidence"]["session_intent"]["acceptance_criteria"]
        expected_task_criteria = adapter.SESSION_MATERIAL.acceptance_criteria_from_intent(
            {"acceptance_criteria": approved_rows})
        if active[0]["acceptance_criteria"] != expected_task_criteria:
            raise ValueError("active task criteria differ from the admitted session task")
        # The full definition snapshot above binds staleness. Proof coverage is
        # only this task's still-unmet obligations; historical met evidence keeps
        # its original digest/time rather than being relabeled for this input.
        ids = sorted(row["id"] for row in approved_rows
                     if row.get("admitted") is True and row.get("status") != "met")
        if not ids:
            raise ValueError("no admitted criteria to publish")
        receipt = {"schema_version": RECEIPT_SCHEMA, "run_dir": str(root),
            "work_item_id": expected_id, "contract": contract, "criterion_ids": ids,
            "prepared_at": datetime.now(timezone.utc).isoformat()}
        record = {"status": "prepared", "receipt": receipt, "receipt_token": _digest(receipt)}
        store.write(PUBLICATION_OBJECT, record)
        return _receipt_output(record)


def _original_evidence_source(proof: str) -> str:
    """Extract one complete top-level section without rewriting original bytes.

    Claim parsing/all-pass validation remains in the existing shared parser.
    This narrow framing check never selects a nested claim's evidence instead.
    """
    marker = "[completion-check]"
    lines = proof.splitlines(keepends=True)
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if sum(line.strip() == marker for line in lines) != 1 or first is None or lines[first].strip() != marker:
        raise ValueError("evidence derivation requires exactly one original completion-check block")
    headers = []
    offset = sum(len(line) for line in lines[:first + 1])
    for line in lines[first + 1:]:
        body = line.rstrip("\r\n")
        if body.strip() and not body[0].isspace():
            if not re.match(r"^-[ \t]*[A-Za-z0-9_-]+[ \t]*:", body):
                raise ValueError("evidence derivation requires a complete canonical completion-check block")
            match = re.match(r"^-[ \t]*evidence[ \t]*:[ \t]*", body, re.I)
            headers.append((offset, offset + match.end() if match else None))
        offset += len(line)
    evidence = [(index, value) for index, (_, value) in enumerate(headers) if value is not None]
    if len(evidence) != 1:
        raise ValueError("evidence derivation requires exactly one nonempty top-level evidence section")
    index, start = evidence[0]
    end = headers[index + 1][0] if index + 1 < len(headers) else len(proof)
    value = proof[start:end].strip()
    if not value:
        raise ValueError("original top-level evidence must not be empty")
    placeholder_word = r"(?:TODO|TBD|PLACEHOLDER|REPLACE_ME|ORIGINAL_[A-Z_]+)"
    placeholders = r"<[ \t]*(?:" + placeholder_word + r"|\.\.\.|…)[ \t]*>|\$\{[^}]+\}"
    for line in value.splitlines():
        if not line.strip():
            continue
        meaningful = re.sub(r"^[ \t]*-[ \t]*", "", line).strip(" \t`\"'")
        if (not meaningful or re.search(placeholders, meaningful, re.I)
                or re.fullmatch(placeholder_word, meaningful, re.I)
                or re.fullmatch(r"(?:none|unknown|pending|n/?a|not provided|not available|\.\.\.|…)[.!]?", meaningful, re.I)):
            raise ValueError("original top-level evidence must not contain empty items or placeholders")
    return value


def _legacy_time_after(prefix_tail: str) -> str | None:
    """Parse a whole scalar after a verification prefix, never a path prefix.

    Delegate ISO forms to the scalar parser. Longest-first prevents fractional
    comma/offset syntax from being cut at a shorter valid timestamp.
    """
    tail = prefix_tail.lstrip("`'\"([{")
    if not re.match(r"\d{4}", tail):
        return None
    ends = [len(tail)]
    for index, char in enumerate(tail):
        following = tail[index + 1:index + 2]
        if char in "/\\_-" or (char in ",.:" and following.isdigit()):
            continue
        if char in ".:" and following and not following.isspace() and following.isalnum():
            continue  # Filename extension or an unfinished time component.
        if char.isspace() or char == "`" or unicodedata.category(char).startswith("P"):
            ends.append(index)
    for end in sorted(set(ends), reverse=True):
        value = tail[:end]
        try:
            _time(value)
        except ValueError:
            continue
        return value
    # An explicit date-like but invalid declaration must not become "untimed".
    # Clear file locators remain data, including ISO dates in path components.
    parts = tail.split()
    head = parts[0]
    try:
        date.fromisoformat(head)
    except ValueError:
        pass
    else:
        head = " ".join(parts[:2])  # A space-separated ISO date/time candidate.
    if "/" in head or "\\" in head or re.search(r"\.[A-Za-z_]", head):
        return None
    raise ValueError("legacy evidence contains an invalid verification timestamp")


def _validate_proof_time(proof: str, verified_at: str) -> None:
    """Check explicit declarations, without requiring time in original prose.

    Legacy grammar is deliberately limited to evidence's `tool-result:ID at T`
    and `[Original|Fresh] verification [output] at T`, where T is a whole scalar
    accepted by _time, bounded by prose punctuation. Dates elsewhere and paths
    are not declarations.
    This checks caller consistency, not the
    authenticity of a verifier output or the truth of a first supplied time.
    """
    declared = re.findall(r"^-[ \t]*(verified[-_]at)[ \t]*:", proof, re.I | re.M)
    if len(declared) > 1:
        raise ValueError("proof contains ambiguous duplicate verification time declarations")
    if declared:
        value = _extract_top_level_section(proof, declared[0])
        _time(value)
        if value != verified_at:
            raise ValueError("proof verification time conflicts with the original raw timestamp")
    parsed = _parse_completion_evidence(proof)
    evidence = [claim["evidence"] for claim in parsed["claims"]]
    evidence.append(_extract_top_level_section(proof, "evidence"))
    legacy = re.compile(r"(?:^|(?<=[; \t]))(?:tool-result:[^\s;]+|"
        r"(?:(?:original|fresh)\s+)?verification(?:\s+output)?)\s+at\s+", re.I)
    times = [value for text in evidence for match in legacy.finditer(text)
             if (value := _legacy_time_after(text[match.end():])) is not None]
    if times and verified_at not in times:
        raise ValueError("legacy evidence verification time conflicts with the original raw timestamp")


def publish_completion(*, receipt_token: str, completion_check: str, verified_at: str,
                       evidence_source: str | None = None, source: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Enqueue supported original proof; only the adapter applies criterion met writes."""
    current = _source(source)
    root = Path(adapter.resolve_run_target(current)["run_dir"]).expanduser().resolve()
    if not receipt_token or not adapter._run_state_available(root):
        raise ValueError("missing prepared completion receipt; do not relabel earlier proof")
    if (root / adapter.OFF_FILE).exists():
        raise ValueError("Autopilot is OFF; completion publication was not queued")
    with storage.transaction(root, current) as store:
        try:
            record = store.read(PUBLICATION_OBJECT)
        except FileNotFoundError as exc:
            raise ValueError("missing prepared completion receipt; do not relabel earlier proof") from exc
        receipt = record["receipt"]
        if receipt_token != record["receipt_token"] or receipt_token != _digest(receipt):
            raise ValueError("completion receipt token is missing, foreign, or tampered")
        if receipt["run_dir"] != str(root) or receipt["contract"] != _current_contract(store.run(), current):
            raise ValueError("completion receipt is stale; original proof cannot be relabeled")
        if not isinstance(completion_check, str) or not completion_check.strip():
            raise ValueError("original completion proof is required")
        observed = _time(verified_at)
        if observed < _time(receipt["prepared_at"]) or observed > datetime.now(timezone.utc):
            raise ValueError("verification must follow the original prepare receipt and must not be future-dated")
        if evidence_source is None:
            evidence_source = _original_evidence_source(completion_check)
        if not evidence_source or evidence_source not in completion_check:
            raise ValueError("original evidence source must appear unchanged in the proof")
        legacy = record["status"] == "published" and "schema_version" not in record.get("result", {})
        if legacy:
            if verified_at not in completion_check:
                raise ValueError("legacy publication time must remain unchanged in its original proof")
        else:
            _validate_proof_time(completion_check, verified_at)
        digest = "sha256:" + hashlib.sha256(completion_check.encode()).hexdigest()
        _validate_continue_next_evidence(digest, [completion_check])
        parsed = _parse_completion_evidence(completion_check)
        if set(parsed["criterion_ids"]) != set(receipt["criterion_ids"]):
            raise ValueError("proof must cover exactly the prepared admitted criteria")
        metadata = {"receipt_token": receipt_token, "completion_check_digest": digest,
                    "verified_at": verified_at, "evidence_source": evidence_source}
        if not legacy:
            metadata["schema_version"] = PROVENANCE_SCHEMA
        decision_id = "completion-" + _digest(metadata).removeprefix("sha256:")
        result = dict(metadata, decision_id=decision_id, status="pending-adapter")
        if record["status"] == "published":
            validate_publication_decision(record, store.run(), record["action"], current)
            if record.get("result") != result or record.get("completion_check") != completion_check:
                raise ValueError("completion receipt was already published with different proof")
            return result
        if storage.pending(root / adapter.DECISION_FILE):
            raise ValueError("another completion decision is pending; resolve it without overwriting")
        generation = receipt["contract"]["approval_generation"]
        action = {"schema_version": adapter.CONSISTENCY_DECISION_SCHEMA,
            "decision_id": decision_id, "work_item_id": receipt["work_item_id"],
            "decision": "continue_next", "promotion_state": "promoted",
            "promotion_evidence": {"decision": "direct", "source": "prepared-completion-publication"},
            "candidate_id": "prepared-" + receipt_token.removeprefix("sha256:"),
            "governance_signal_digest": digest, "decision_key": _digest(metadata),
            "state_hash": _digest(receipt["contract"]), "loop_key": _digest([receipt_token, digest]),
            "evidence": [completion_check], "verdict": "pass", "completion_check_digest": digest,
            "approval_generation": generation, "completion_origin": metadata}
        adapter._validate_promoted_decision_file(action)
        store.write(adapter.DECISION_FILE, action)
        store.write(PUBLICATION_OBJECT, dict(record, status="published", result=result,
                                            completion_check=completion_check, action=action))
        return result


def validate_publication_decision(record: Mapping[str, Any], run: Mapping[str, Any],
                                  decision: Mapping[str, Any], source: Mapping[str, str]) -> None:
    """Recheck publication identity inside the adapter's Core/task transaction."""
    receipt = record.get("receipt") or {}
    if record.get("status") != "published" or record.get("action") != decision:
        raise ValueError("completion publication is missing or its queued decision was altered")
    if record.get("receipt_token") != _digest(receipt):
        raise ValueError("completion publication receipt was altered")
    if receipt.get("contract") != _current_contract(run, _source(source)):
        raise ValueError("completion publication contract changed before adapter commit")
    proof = record.get("completion_check")
    if not isinstance(proof, str) or decision.get("completion_check_digest") != "sha256:" + hashlib.sha256(proof.encode()).hexdigest():
        raise ValueError("completion publication proof digest differs from original evidence")
    origin = decision.get("completion_origin")
    if not isinstance(origin, Mapping):
        raise ValueError("completion publication provenance is missing")
    fields = {"receipt_token", "completion_check_digest", "verified_at", "evidence_source"}
    if "schema_version" in origin:
        if origin["schema_version"] != PROVENANCE_SCHEMA:
            raise ValueError("unsupported completion publication provenance")
        fields.add("schema_version")
    if set(origin) != fields or any(not isinstance(origin[key], str) or not origin[key] for key in fields):
        raise ValueError("completion publication provenance is malformed")
    if (origin["receipt_token"] != record["receipt_token"]
            or origin["completion_check_digest"] != decision["completion_check_digest"]
            or origin["evidence_source"] not in proof or decision.get("evidence") != [proof]):
        raise ValueError("completion publication provenance differs from original evidence")
    observed = _time(origin["verified_at"])
    if observed < _time(receipt["prepared_at"]) or observed > datetime.now(timezone.utc):
        raise ValueError("completion publication verification time is outside its original receipt")
    if "schema_version" in origin:
        _validate_proof_time(proof, origin["verified_at"])
    elif origin["verified_at"] not in proof:
        raise ValueError("legacy publication time differs from original proof")
    key = _digest(origin)
    decision_id = "completion-" + key.removeprefix("sha256:")
    if (decision.get("decision_key") != key or decision.get("decision_id") != decision_id
            or record.get("result") != dict(origin, decision_id=decision_id, status="pending-adapter")):
        raise ValueError("completion publication result differs from immutable provenance")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="capture original provenance before verification")
    prepare.add_argument("--intent-root", required=True, type=Path)
    prepare.add_argument("--platform", required=True, choices=SUPPORTED_PLATFORMS)
    prepare.add_argument("--session-id", required=True)
    prepare.add_argument("--input-event-id", required=True)
    prepare.add_argument("--reapprove-current-input", action="store_true",
                         help="admit already-authorized current work before new verification; never relabel old proof")
    publish = commands.add_parser("publish", help="publish the unchanged supported proof without rerunning checks")
    publish.add_argument("--receipt-token", required=True)
    publish.add_argument("--verified-at", required=True)
    publish.add_argument("--evidence-source", help="optional checked legacy override; default uses the original top-level evidence section")
    publish.add_argument("--completion-file", required=True, help="original completion block path, or - for stdin")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_completion(intent_root=args.intent_root, platform=args.platform,
                session_id=args.session_id, input_event_id=args.input_event_id,
                reapprove_current_input=args.reapprove_current_input)
        else:
            current = _source(None)
            raw = sys.stdin.buffer.read() if args.completion_file == "-" else Path(args.completion_file).read_bytes()
            proof = raw.decode("utf-8")
            result = publish_completion(receipt_token=args.receipt_token, completion_check=proof,
                verified_at=args.verified_at, evidence_source=args.evidence_source, source=current)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"completion publication rejected: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

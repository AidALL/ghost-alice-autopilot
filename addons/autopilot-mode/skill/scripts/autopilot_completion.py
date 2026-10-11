#!/usr/bin/env python3
"""Explicit manual/compatibility CLI for provenance and supported proof publication.

Dependencies: Python 3.11+ standard library, sibling Ghost-ALICE Autopilot adapters, and the bound
Ghost-ALICE SQLite storage runtime. An authorized manual caller requests this
CLI through the native host shell tool and supplies the current receipt/runtime
coordinates. Native managed v2 uses registered pretool preparation and Stop
publication; the assistant does not issue a separate publish command there.
This helper never runs a business check or marks a criterion met. It returns
JSON preparation/publication status; native Stop authenticates the accepted
publication and requests guarded criterion writes in one authority transaction.
A receipt is provenance, not truth.

prepare --intent-root ROOT --platform codex --session-id ID --input-event-id ID
publish --receipt-token TOKEN --verified-at ISO_TIME --completion-file -
                                            # exact completion block on stdin
verification-time  # optional audit clock; normal v2 ordering uses authenticated capture/review bindings

The prepare result carries receipt_token, selected criteria and publish_command;
publish returns recording status, not task completion or visible delivery.
Both operations use the configured Stop run target and current session. Runtime
state stays in the Ghost-ALICE Autopilot authority; no business scratch file is required. Timestamps
and receipt tokens are strings. Prepare must precede the original verification.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters"))
import autopilot_state as adapter
import autopilot_storage as storage
from autopilot_work_items import _parse_completion_evidence, _validate_continue_next_evidence, _with_core_policy_source

PUBLICATION_OBJECT = "completion-publication.json"
RECEIPT_SCHEMA = "autopilot-completion-receipt.v1"
PROVENANCE_SCHEMA = "autopilot-completion-provenance.v1"
SUPPORTED_PLATFORMS = ("codex", "claude")
from autopilot_completion_service import (
    CompletionRecordingError, _require_supported_platform, _digest, _time, _definitions,
    _approval_selection, _source, _current_contract, _original_evidence_source,
    _validate_proof_time, validate_publication_decision,
    _prepare_completion_v2, _publish_completion_v2, apply_completion_v2,
)


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
        raise ValueError("input changed during completion preparation; the requested input_event_id or input_digest no longer matches current intake")


@_with_core_policy_source
def prepare_completion(*, intent_root: Path, platform: str, session_id: str,
                       input_event_id: str, source: Mapping[str, str] | None = None,
                       reapprove_current_input: bool = False, schema_version: str | None = None,
                       correction_of: str | None = None) -> dict[str, Any]:
    from autopilot_lineage import completion_protocol
    protocol = completion_protocol(dict(os.environ if source is None else source), schema_version)
    if protocol == "autopilot-completion-receipt.v2":
        return _prepare_completion_v2(intent_root=intent_root, platform=platform, session_id=session_id,
            input_event_id=input_event_id, source=source, correction_of=correction_of)
    if correction_of is not None:
        raise ValueError("Corrections require v2 publication")
    try:
        return _prepare_completion(intent_root=intent_root, platform=platform, session_id=session_id,
            input_event_id=input_event_id, source=source, reapprove_current_input=reapprove_current_input)
    except CompletionRecordingError as exc:
        return exc.status()


def _prepare_completion(*, intent_root: Path, platform: str, session_id: str,
                       input_event_id: str, source: Mapping[str, str] | None = None,
                       reapprove_current_input: bool = False) -> dict[str, Any]:
    """Capture the v1 receipt before the current-session assistant performs business verification."""
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
    if adapter.run_is_paused(root):
        raise ValueError("Ghost-ALICE Autopilot is OFF; completion publication was not prepared")
    from autopilot_intent_recovery import project_session_material
    material, selected = project_session_material(material, current, root)
    if selected["status"] != "selected":
        return selected
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
                        expected_scope = None  # This scope has no captured semantic field.
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


@_with_core_policy_source
def publish_completion(*, receipt_token: str, completion_check: str, verified_at: str,
                       evidence_source: str | None = None, source: Mapping[str, str] | None = None,
                       schema_version: str | None = None, evidence_references: list[str] | None = None,
                       dependencies: list[str] | None = None, criterion_dependencies: list[dict[str, Any]] | None = None,
                       origin_receipt_token: str | None = None,
                       original_dependency_snapshots: list[dict[str, str]] | None = None,
                       correction: dict[str, Any] | None = None) -> dict[str, Any]:
    """Enqueue supported original proof; only the adapter applies criterion met writes."""
    from autopilot_lineage import completion_protocol
    protocol = completion_protocol(dict(os.environ if source is None else source), schema_version)
    if protocol == "autopilot-completion-receipt.v2":
        from autopilot_completion_contract import CompletionContractError
        try:
            return _publish_completion_v2(receipt_token=receipt_token, completion_check=completion_check,
                verified_at=verified_at, evidence_source=evidence_source, source=source,
                evidence_references=evidence_references, dependencies=dependencies, criterion_dependencies=criterion_dependencies,
                origin_receipt_token=origin_receipt_token, original_dependency_snapshots=original_dependency_snapshots, correction=correction)
        except CompletionContractError as error:
            if error.reason == "time-order-ambiguous":
                # Publication rolled back. Retain its typed recording gap so
                # Stop cannot demand unchanged proof again as if it were absent.
                from autopilot_completion_service import recording_failure_payload
                recording_failure_payload(dict(os.environ if source is None else source), None, error)
            raise
    if correction is not None or evidence_references is not None or dependencies is not None or criterion_dependencies is not None or origin_receipt_token is not None or original_dependency_snapshots is not None:
        raise ValueError("v1 publication cannot consume v2 references or dependency snapshots")
    current = _source(source)
    root = Path(adapter.resolve_run_target(current)["run_dir"]).expanduser().resolve()
    if not receipt_token or not adapter._run_state_available(root):
        raise ValueError("missing prepared completion receipt; do not relabel earlier proof")
    if adapter.run_is_paused(root):
        raise ValueError("Ghost-ALICE Autopilot is OFF; completion publication was not queued")
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
                raise ValueError("publication time must remain unchanged in its original proof")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verification-time", help="optional audit clock for an actual new verification, not required by normal v2 ordering; "
        "clock output cannot refresh historical proof or establish a business verification result")
    prepare = commands.add_parser("prepare", help="Ghost-ALICE Autopilot captures original provenance before required new verification")
    prepare.add_argument("--intent-root", required=True, type=Path)
    prepare.add_argument("--platform", required=True, choices=SUPPORTED_PLATFORMS)
    prepare.add_argument("--session-id", required=True)
    prepare.add_argument("--input-event-id", required=True)
    prepare.add_argument("--correction-of", help="accepted publication reference to correct with current authority")
    prepare.add_argument("--reapprove-current-input", action="store_true",
                         help="Ghost-ALICE Autopilot admits already-authorized current work before required new verification; earlier proof retains the original binding")
    publish = commands.add_parser("publish", help="Ghost-ALICE Autopilot publishes unchanged supported proof; the current-session assistant reuses completed checks")
    publish.add_argument("--receipt-token", required=True)
    publish.add_argument("--correction-json", help="current-session-assistant-supplied correction JSON with supersedes, affected qualified criteria and reason")
    publish.add_argument("--origin-receipt-token", help="current-session assistant supplies the original v2 capture receipt for retained unrecorded proof; the current receipt authorizes publication")
    publish.add_argument("--original-dependencies-json", help="current-session assistant supplies original verification locator/digest snapshots for retained capture ingestion, including [] when no dependencies existed")
    publish.add_argument("--verified-at")
    publish.add_argument("--evidence-source", help="optional checked override; default uses the original top-level evidence section")
    publish.add_argument("--completion-file", help="original completion block path, or - for stdin")
    publish.add_argument("--evidence-reference", action="append", dest="evidence_references",
                         help="exact accepted v2 evidence reference; repeat for independent original records")
    publish.add_argument("--dependency", action="append", dest="dependencies",
                         help="absolute relevant artifact dependency locator for fresh v2 evidence")
    publish.add_argument("--criterion-dependencies-json", help="v2 qualified criterion-to-locator mapping as a JSON list")
    for command in (prepare, publish):
        command.add_argument("--schema-version", choices=("autopilot-completion-receipt.v1", "autopilot-completion-receipt.v2"),
                             help="protocol capability only; does not grant approval or permission")
    args = parser.parse_args(argv)
    if args.command == "verification-time":
        print(datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        return 0
    try:
        if args.command == "prepare":
            result = prepare_completion(intent_root=args.intent_root, platform=args.platform,
                session_id=args.session_id, input_event_id=args.input_event_id,
                reapprove_current_input=args.reapprove_current_input, schema_version=args.schema_version, correction_of=args.correction_of)
        else:
            current = _source(None)
            if args.evidence_references is not None:
                if args.completion_file is not None or args.verified_at is not None:
                    raise ValueError("Reference reuse cannot relabel original proof or verification time")
                proof = None
            else:
                if not args.completion_file or not args.verified_at:
                    raise ValueError("Fresh proof requires --completion-file and --verified-at")
                raw = sys.stdin.buffer.read() if args.completion_file == "-" else Path(args.completion_file).read_bytes()
                proof = raw.decode("utf-8")
            result = publish_completion(receipt_token=args.receipt_token, completion_check=proof,
                verified_at=args.verified_at, evidence_source=args.evidence_source, source=current,
                schema_version=args.schema_version, evidence_references=args.evidence_references, dependencies=args.dependencies,
                origin_receipt_token=args.origin_receipt_token,
                correction=json.loads(args.correction_json) if args.correction_json is not None else None,
                original_dependency_snapshots=json.loads(args.original_dependencies_json) if args.original_dependencies_json is not None else None,
                criterion_dependencies=json.loads(args.criterion_dependencies_json) if args.criterion_dependencies_json is not None else None)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"completion publication rejected: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

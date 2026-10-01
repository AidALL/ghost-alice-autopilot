"""Prospective proof provenance; capture alone never admits or advances work.

Dependencies: Python standard library, AP helpers and current Core SQLite API.
Origins are immutable rows in the current session's authority. They are not
approval, business evidence, or a successful completion verdict.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from autopilot_runtime_context import run_owner_hint, run_owner_matches


def _helper():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import autopilot_completion
    return autopilot_completion


def _locked_state(core, root, source, connection):
    return core.read_session_state(root=root, platform=source["GHOST_ALICE_PLATFORM"],
        session_id=source["GHOST_ALICE_SESSION_ID"], transaction=connection)


def _blocked(state):
    decision = state.get("model_security_decision") or {}
    return (decision.get("input_event_id") == state.get("latest_input_event_id")
            and decision.get("input_digest") in (None, "", state.get("latest_input_digest"))
            and decision.get("decision") == "block")


def _snapshot(state, source, run_dir):
    helper = _helper()
    return {"platform": source["GHOST_ALICE_PLATFORM"], "session_id": source["GHOST_ALICE_SESSION_ID"],
        "intent_root": str(Path(source["GHOST_ALICE_SESSION_INTENT_ROOT"]).resolve()),
        "run_dir": str(run_dir.resolve()), "input_event_id": state.get("latest_input_event_id"),
        "input_digest": state.get("latest_input_digest"),
        "acceptance_criteria": helper._definitions(state.get("acceptance_criteria", [])),
        "criterion_ids": sorted(row["id"] for row in state.get("acceptance_criteria", [])
            if row.get("admitted") is True and row.get("status") != "met"),
        "scope_summary": helper.adapter.SESSION_MATERIAL.run_summary(state),
        "allowed_surfaces": [str(source.get("GHOST_ALICE_AUTOPILOT_PLAN_PATH") or
            helper.adapter._project_cwd_from_env(source) / ".tmp/implementation-plans/autopilot-session-intent.md")],
        "stop_conditions": list(helper.adapter.DEFAULT_STOP_CONDITIONS),
        **{key: copy.deepcopy(state.get(key, {} if key == "latest_scope" else []))
           for key in ("constraints", "non_goals", "decisions", "latest_scope")}}


def _key(snapshot):
    return _helper()._digest([snapshot[key] for key in ("run_dir", "intent_root", "platform", "session_id")])


def _tables(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS ap_completion_origins (token TEXT PRIMARY KEY, body_json TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS ap_completion_origin_heads (origin_key TEXT PRIMARY KEY, token TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS ap_completion_origin_bindings (token TEXT PRIMARY KEY, generation TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS ap_completion_preflight_notices (origin_token TEXT PRIMARY KEY REFERENCES ap_completion_origins(token))")


def _head(connection, key):
    if not connection.execute("SELECT name FROM sqlite_master WHERE name='ap_completion_origin_heads'").fetchone():
        return None
    row = connection.execute("SELECT o.token,o.body_json FROM ap_completion_origins o JOIN ap_completion_origin_heads h ON o.token=h.token WHERE h.origin_key=?", (key,)).fetchone()
    if row is None:
        return None
    body = json.loads(row[1])
    if _helper()._digest(body) != row[0]:
        raise ValueError("prospective origin was altered")
    return {"origin_token": row[0], **body}


def _bound_generation(connection, origin):
    row = connection.execute("SELECT generation FROM ap_completion_origin_bindings WHERE token=?", (origin["origin_token"],)).fetchone()
    return row[0] if row else origin["existing_generation"]


def _current_generation(connection, snapshot):
    if not connection.execute("SELECT name FROM sqlite_master WHERE name='ap_runs'").fetchone():
        return None
    row = connection.execute("SELECT run_json FROM ap_runs WHERE run_key=?", (snapshot["run_dir"],)).fetchone()
    if row is None:
        return None
    run = json.loads(row[0]); binding = (run.get("approval_evidence") or {}).get("session_intent") or {}
    event = binding.get("latest_input_event") or {}
    helper = _helper()
    if (binding.get("platform") == snapshot["platform"] and binding.get("session_id") == snapshot["session_id"]
            and event.get("event_id") == snapshot["input_event_id"]
            and event.get("input_digest") == snapshot["input_digest"]
            and helper._definitions(binding.get("acceptance_criteria", [])) == snapshot["acceptance_criteria"]
            and run.get("scope") in _compatible_scopes(snapshot)
            and all(run.get(key) == snapshot[key] for key in ("allowed_surfaces", "stop_conditions"))):
        generation = helper.adapter.SESSION_MATERIAL.approval_generation(run)
        if run.get("scope") == {"summary": snapshot["scope_summary"]}:
            # Legacy summary-only approvals lack historical semantic fields.
            # Reuse a checked receipt when one exists; otherwise retain the
            # existing generation conservatively rather than inventing history.
            previous = connection.execute("SELECT body_json FROM ap_objects WHERE run_key=? AND generation=? AND name=?",
                (snapshot["run_dir"], generation, helper.PUBLICATION_OBJECT)).fetchone()
            if previous is not None:
                publication = json.loads(previous[0]); receipt = publication.get("receipt") or {}
                if publication.get("receipt_token") != helper._digest(receipt):
                    raise ValueError("previous completion receipt was altered")
                if any(receipt.get("contract", {}).get(key) != snapshot[key]
                       for key in ("constraints", "non_goals", "decisions", "latest_scope")):
                    return None
        return generation
    return None


def _bound_scope(snapshot):
    return {"summary": snapshot["scope_summary"], "completion_contract": {
        key: snapshot[key] for key in ("constraints", "non_goals", "decisions")}}


def _compatible_scopes(snapshot):
    bound = _bound_scope(snapshot)
    return ({"summary": snapshot["scope_summary"]}, bound,
            dict(bound, publication_mode="session-intent-task", latest_scope=snapshot["latest_scope"]))


def _unbound_conduct_reapproval(connection, origin, run, snapshot):
    """Recover only an unused conduct-only admission of this exact contract."""
    helper = _helper(); generation = origin["existing_generation"]
    if (generation is None or run.get("scope") != _compatible_scopes(snapshot)[2]
            or connection.execute("SELECT 1 FROM ap_completion_origin_bindings WHERE token=?",
                                  (origin["origin_token"],)).fetchone()):
        return False
    row = connection.execute("SELECT run_json FROM ap_generations WHERE run_key=? AND generation=?",
                             (snapshot["run_dir"], generation)).fetchone()
    if row is None:
        return False
    previous = json.loads(row[0])
    if (helper.adapter.SESSION_MATERIAL.approval_generation(previous) != generation
            or previous.get("scope") != _bound_scope(snapshot)):
        return False
    names = {row[0] for row in connection.execute("SELECT name FROM ap_objects WHERE run_key=? AND generation=?",
                                                (snapshot["run_dir"], generation))}
    if ("conduct-plan.json" not in names
            or not names <= {"conduct-plan.json", "conduct-plan.candidate.json", "events.jsonl"}
            or connection.execute("SELECT 1 FROM ap_receipts WHERE run_key=? AND generation=? LIMIT 1",
                                  (snapshot["run_dir"], generation)).fetchone()):
        return False
    # Only the explicit task-route scope may differ. Input, criteria, session,
    # run identity, surfaces and stop conditions remain generation-bound.
    previous["scope"] = run["scope"]
    return helper.adapter.SESSION_MATERIAL.approval_generation(previous) == helper.adapter.SESSION_MATERIAL.approval_generation(run)


def _capture_result(connection, origin, *, preflight, ownership_conflict=False):
    result = {"status": "prospective", "origin_token": origin["origin_token"],
              "captured_at": origin["captured_at"]}
    if preflight:
        # The caller still holds the authority transaction that rechecked the
        # current source and contract. A parallel hook can claim this origin
        # only after this transaction ends; capture/admission remains separate.
        claimed = connection.execute("INSERT INTO ap_completion_preflight_notices VALUES(?) ON CONFLICT(origin_token) DO NOTHING",
                                     (origin["origin_token"],)).rowcount
        if claimed:
            if ownership_conflict:
                result["additional_context"] = "\n".join([
                    "[autopilot]", "completion-preflight: ownership-conflict",
                    "The selected run belongs to a different session or standalone owner. Its approval and state are preserved.",
                    "recovery-action: correct the explicit run-dir override within the authorized scope; default selection uses the current session's own run. No completion command was issued.",
                ])
            else:
                from autopilot_messages import completion_preflight_guidance
                result["additional_context"] = completion_preflight_guidance(origin)
    return result


def capture_origin(source, *, preflight=False):
    helper = _helper(); current = helper._source(source)
    root = Path(current["GHOST_ALICE_SESSION_INTENT_ROOT"]).resolve()
    run_dir = Path(helper.adapter.resolve_run_target(current)["run_dir"]).resolve()
    if helper.adapter.run_is_paused(run_dir) or not (root / "ghost-state.sqlite3").is_file():
        return {"status": "skipped"}
    owner = run_owner_hint(run_dir)
    ownership_conflict = owner is not None and not run_owner_matches(owner, current)
    material = helper.adapter.read_session_material(root, current["GHOST_ALICE_PLATFORM"],
        current["GHOST_ALICE_SESSION_ID"], source=current, require_input=True, recover_audit=False)
    before = material["intent_state"]
    snapshot = _snapshot(before, current, run_dir)
    if _blocked(before) or not snapshot["criterion_ids"]:
        return {"status": "skipped"}
    from autopilot_work_items import _load_core_ledger_module
    core = _load_core_ledger_module(material["state_path"], current)
    with core.storage_transaction(root) as connection:
        state = _locked_state(core, root, current, connection)
        if _snapshot(state, current, run_dir) != snapshot or _blocked(state):
            raise ValueError("current input or contract changed during prospective capture")
        if helper.adapter.run_is_paused(run_dir):
            return {"status": "skipped"}
        _tables(connection)
        existing = _head(connection, _key(snapshot))
        generation = _current_generation(connection, snapshot)
        pinned = _bound_generation(connection, existing) if existing is not None else None
        if (existing is not None and existing["contract"] == snapshot
                and (generation is None or pinned is None or generation == pinned)):
            # Admission after capture does not refresh origin age or manufacture
            # a new preverification generation receipt.
            return _capture_result(connection, existing, preflight=preflight, ownership_conflict=ownership_conflict)
        body = {"schema_version": "autopilot-prospective-origin.v1", "contract": snapshot,
            "captured_at": datetime.now(timezone.utc).isoformat(), "existing_generation": generation}
        token = helper._digest(body)
        connection.execute("INSERT INTO ap_completion_origins VALUES(?,?)", (token, json.dumps(body, sort_keys=True)))
        connection.execute("INSERT INTO ap_completion_origin_heads VALUES(?,?) ON CONFLICT(origin_key) DO UPDATE SET token=excluded.token", (_key(snapshot), token))
        return _capture_result(connection, {"origin_token": token, **body}, preflight=preflight, ownership_conflict=ownership_conflict)


def bind_origin(run_dir, run, items, source):
    """Attach an exact prospective origin only after ordinary admission checks."""
    helper = _helper(); store = helper.storage.active(run_dir)
    if (store is None or store.authority.get("kind") != "session"
            or not store.connection.execute("SELECT name FROM sqlite_master WHERE name='ap_completion_origin_heads'").fetchone()):
        return {}
    try:
        store.read(helper.PUBLICATION_OBJECT)
        return {}
    except FileNotFoundError:
        pass
    current = helper._source(source)
    root = Path(current["GHOST_ALICE_SESSION_INTENT_ROOT"]).resolve()
    from autopilot_work_items import _load_core_ledger_module
    core = _load_core_ledger_module(root / current["GHOST_ALICE_PLATFORM"] / current["GHOST_ALICE_SESSION_ID"] / "intent-state.json", current)
    state = _locked_state(core, root, current, store.connection)
    snapshot = _snapshot(state, current, Path(run_dir))
    origin = _head(store.connection, _key(snapshot))
    if origin is None:
        return {}
    if _blocked(state) or origin["contract"] != snapshot:
        return {"completion_origin_gap": "stale"}
    contract = helper._current_contract(run, current)
    _tables(store.connection)
    generation_matches = (_bound_generation(store.connection, origin) in (None, contract["approval_generation"])
                          or _unbound_conduct_reapproval(store.connection, origin, run, snapshot))
    if (not generation_matches
            or contract["scope"] not in _compatible_scopes(snapshot)
            or any(contract[key] != snapshot[key] for key in ("allowed_surfaces", "stop_conditions"))):
        return {"completion_origin_gap": "stale"}
    expected_id = "session-intent-" + helper.adapter.SESSION_MATERIAL.safe_id(current["GHOST_ALICE_SESSION_ID"])
    active = [item for item in items if item["status"] in {"ready", "running", "reopened"}]
    approved = run["approval_evidence"]["session_intent"]["acceptance_criteria"]
    if (len(active) != 1 or active[0]["id"] != expected_id
            or active[0]["acceptance_criteria"] != helper.adapter.SESSION_MATERIAL.acceptance_criteria_from_intent({"acceptance_criteria": approved})
            or sorted(row["id"] for row in approved if row.get("admitted") is True and row.get("status") != "met") != snapshot["criterion_ids"]):
        return {}
    receipt = {"schema_version": helper.RECEIPT_SCHEMA, "run_dir": str(Path(run_dir).resolve()),
        "work_item_id": expected_id, "contract": contract, "criterion_ids": snapshot["criterion_ids"],
        "prepared_at": origin["captured_at"], "prospective_origin": origin["origin_token"]}
    publication = {"status": "prepared", "receipt": receipt, "receipt_token": helper._digest(receipt)}
    store.connection.execute("INSERT INTO ap_completion_origin_bindings VALUES(?,?) ON CONFLICT(token) DO NOTHING",
        (origin["origin_token"], contract["approval_generation"]))
    store.write(helper.PUBLICATION_OBJECT, publication)
    return {"completion_publication": publication}


def reapproval_payload(run_dir, run, source):
    """Surface current authorized admission without advancing the previous run."""
    helper = _helper(); store = helper.storage.active(run_dir)
    if (store is None or store.authority.get("kind") != "session"
            or not store.connection.execute("SELECT name FROM sqlite_master WHERE name='ap_completion_origin_heads'").fetchone()):
        return None
    current = helper._source(source)
    binding = (run.get("approval_evidence") or {}).get("session_intent") or {}
    if binding.get("platform") != current["GHOST_ALICE_PLATFORM"] or binding.get("session_id") != current["GHOST_ALICE_SESSION_ID"]:
        return None
    try:
        contract = helper._current_contract(run, current)
        try:
            previous = store.read(helper.PUBLICATION_OBJECT)
        except FileNotFoundError:
            return None
        if previous["receipt"]["contract"] == contract:
            return None
    except ValueError:
        pass
    material = helper.adapter.read_session_material(Path(current["GHOST_ALICE_SESSION_INTENT_ROOT"]),
        current["GHOST_ALICE_PLATFORM"], current["GHOST_ALICE_SESSION_ID"], source=current, require_input=True)
    state = material["intent_state"]; snapshot = _snapshot(state, current, Path(run_dir))
    origin = _head(store.connection, _key(snapshot))
    if _blocked(state) or not snapshot["criterion_ids"] or origin is None or origin["contract"] != snapshot:
        return None
    from autopilot_messages import completion_command, completion_recovery_guidance
    command = completion_command(snapshot, str(Path(run_dir).resolve()), "prepare", "--reapprove-current-input",
        "--intent-root", snapshot["intent_root"], "--platform", snapshot["platform"],
        "--session-id", snapshot["session_id"], "--input-event-id", snapshot["input_event_id"])
    return {"continue": True, "systemMessage": "\n".join([
        "[autopilot]", "completion-admission: required", f"prospective-captured-at: {origin['captured_at']}",
        "Current input has separately admitted execution criteria and original prospective provenance. The previous run is parked. For this already-authorized work, use the supported admission command below, then publish its unchanged original proof with the returned receipt. This archives the previous generation; it does not require another user permission round or a repeated business check. A proof older than this capture remains rejected.",
        command, completion_recovery_guidance()])}


def default_intent_root(source, project_cwd):
    """Resolve Core's own installed default without discovering another session."""
    from autopilot_work_items import _load_core_ledger_module
    home = Path(source.get("HOME") or Path.home())
    locator = home / ".ghost-alice/runtime/current/.tmp/session-intent/intent-state.json"
    core = _load_core_ledger_module(locator, source)
    if core is None:
        return None
    return core.default_root(env=dict(source), cwd=project_cwd)

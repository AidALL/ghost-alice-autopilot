"""Native intake and response routes for current-work integration tests."""
from pathlib import Path


def register_route(core, root, platform="codex", session="session-1", *, selected=None,
                   kind="execution", mode="normal", purpose=None, continuation_feedback=None):
    state = core.read_session_state(root=root, platform=platform, session_id=session)
    owned = [c for c in state["acceptance_criteria"] if "work_unit_id" in c]
    latest = {}
    for row in owned:
        latest[row["work_unit_id"]] = max(latest.get(row["work_unit_id"], 0), row["contract_revision"])
    ids = [unit for unit in latest if any(c["work_unit_id"] == unit and c["contract_revision"] == latest[unit] and c["admitted"] for c in owned)] if selected is None else selected
    units = [{"work_unit_id": u, "contract_revision": latest[u], "kind": kind,
              "admitted": True, "owner": {"platform": platform, "session_id": session}}
             for u in ids]
    criteria = [{"work_unit_id": c["work_unit_id"], "contract_revision": c["contract_revision"],
                 "criterion_id": c["id"]} for c in owned if c["work_unit_id"] in ids
                and c["contract_revision"] == latest[c["work_unit_id"]] and c["admitted"]]
    surface = dict(intent_relation="continuation", primary_request="Current authorized work",
        causal_axis="n/a", response_mode=mode,
        response_order="imperative-result-first-after-necessary-work" if mode == "normal" else "resolved-intent-first",
        change_depth="localized", focus_layer="meso", verification_complexity="level-2",
        boundary_contract="required" if mode == "normal" else "n/a", forced_visibility="no", reason="Test selection")
    return core.record_response_route(root=root, platform=platform, session_id=session,
        expected_input_event_id=state["latest_input_event_id"], boundary_ref="test-boundary" if mode == "normal" else None,
        candidate=dict(**({"response_purpose":purpose} if purpose is not None else {}),
                       **({"continuation_feedback":continuation_feedback} if continuation_feedback is not None else {}),
                       schema_version="response-route.v1", routing_surface=surface, work_units=units,
                       selected_work_unit_ids=ids, selected_criteria=criteria))


def native_ledger(root, core, *, platform="codex", session="session-1", criteria=None,
                  selected=None, mode="normal"):
    (Path(root) / platform / session).mkdir(parents=True, exist_ok=True)
    raw = "Create and verify the current deliverable"
    observation = core.build_input_observation(platform=platform, session_id=session, raw_user_input=raw)
    observation["input_event_id"] = "event-1"
    criteria = criteria or [{"id": "bridge-state", "summary": "Current requested deliverable",
        "source": "user-explicit", "admitted": True, "status": "unmet", "work_unit_id": "current", "contract_revision": 1}]
    core.record_turn(root=root, platform=platform, session_id=session, raw_user_input=raw,
        observation=observation, intent_delta={"current_goal": "Write and verify the requested report.",
                                              "acceptance_criteria": criteria})
    register_route(core, root, platform, session, selected=selected, mode=mode)
    return Path(root) / platform / session / "intent-state.json"


def retained_publication_fixture(test, criteria):
    """Select a fresh native authority for an exact retained proof test."""
    test.root = test.root / "retained-case"
    test.root.mkdir()
    test.run_dir = test.root / ".autopilot/sessions/codex/session-1"
    test.source.update(GHOST_ALICE_SESSION_INTENT_ROOT=str(test.root / "intent"),
                       GHOST_ALICE_AUTOPILOT_CWD=str(test.root), PWD=str(test.root))
    rows = [dict(c, work_unit_id="retained-unit", contract_revision=1) for c in criteria]
    test.state_path = native_ledger(test.root / "intent", test.core, criteria=rows)
    test.arguments.update(intent_root=test.root / "intent", source=test.source)

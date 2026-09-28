"""Read-only session provenance and shared Stop target selection.

Dependencies: Python 3.11+ standard library and sibling lineage checks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from autopilot_lineage import intent_identity_matches
from autopilot_work_items import _load_core_ledger_module


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def read_session_material(
    intent_root: Path, platform: str, session_id: str | None,
    *, require_input: bool = False, expected_input_event_id: str | None = None,
    source: Mapping[str, str] | None = None, recover_audit: bool = True,
) -> dict[str, Any]:
    """One reader for bridge admission, automatic bootstrap and continuation.

    Legacy continuation may have no recorded input event. New explicit
    admission requires one; any present event must match the firing identity.
    """
    if platform not in {"codex", "claude", "agent-runtime"}:
        raise ValueError("unsupported session platform")
    from autopilot_storage import core_transaction
    root = Path(intent_root).expanduser().resolve()
    locator = root / platform / (session_id or "_discovery") / "intent-state.json"
    core = _load_core_ledger_module(locator, os.environ if source is None else source)
    if core is None or not hasattr(core, "read_session_events"):
        raise ValueError("compatible Core SQLite reader is required")
    connection = core_transaction(root)
    if not session_id:
        session_id = core.read_current_session_pointer(root, platform, transaction=connection)
    if not session_id:
        raise FileNotFoundError("current session authority is missing or has invalid schema_version")
    if Path(session_id).name != session_id or session_id in {".", ".."} or "\\" in session_id:
        raise ValueError("session id must be a single path component")
    state_path = root / platform / session_id / "intent-state.json"
    state = core.read_session_state(root=root, platform=platform, session_id=session_id,
                                    transaction=connection)
    if not state:
        raise FileNotFoundError(f"{state_path}: selected current-session state is missing")
    if not intent_identity_matches(state, platform, session_id):
        raise ValueError(f"{state_path}: ledger schema, platform, or session identity mismatch")
    if not recover_audit and state.get("pending_audit_event"):
        raise ValueError("ledger audit is pending; migration is required before admission")
    if recover_audit and state.get("pending_audit_event"):
        state = core.migrate_session(root=root, platform=platform, session_id=session_id,
                                     transaction=connection)
    events_path = state_path.parent / "intent-events.jsonl"
    events = core.read_session_events(root=root, platform=platform, session_id=session_id,
                                      transaction=connection)
    if "latest_input_event_id" in state:
        revision = state.get("ledger_revision", 0)
        if type(revision) is not int or revision < 0:
            raise ValueError("ledger revision is invalid")
        event_id, digest = state.get("latest_input_event_id"), state.get("latest_input_digest")
        if not isinstance(event_id, str) or not isinstance(digest, str) or bool(event_id) != bool(digest):
            raise ValueError("ledger input anchor is invalid")
        # Audit rows are only a projection. Never borrow a newer row's input
        # identity to authorize work against an older state snapshot.
        events = [row for row in events if row.get("session_id") == session_id
                  and row.get("platform") == platform
                  and ("ledger_revision" not in row or
                       (type(row["ledger_revision"]) is int and row["ledger_revision"] <= revision))]
        latest_input = {"event": "user-input-observed", "event_id": event_id,
                        "platform": platform, "session_id": session_id,
                        "input_digest": digest,
                        "input_char_count": state.get("latest_input_char_count", 0),
                        "source": "authoritative-state-anchor"} if event_id else {}
    else:
        for event in events:
            if event.get("session_id") != session_id or event.get("platform") != platform:
                raise ValueError(f"{events_path}: event identity differs from current session")
        latest_input = next((row for row in reversed(events) if row.get("event") == "user-input-observed"), {})
    if require_input and (not latest_input.get("event_id") or not latest_input.get("input_digest")):
        raise ValueError(f"{events_path}: current input event and digest are required")
    if expected_input_event_id is not None and latest_input.get("event_id") != expected_input_event_id:
        raise ValueError("input event changed; inspect current input and obtain current approval before retrying")
    return {"platform": platform, "session_id": session_id, "state_path": state_path,
            "events_path": events_path, "intent_state": state, "events": events,
            "latest_input_event": latest_input}


def project_cwd_from_env(source: Mapping[str, str]) -> Path:
    candidate = Path(str(source.get("GHOST_ALICE_AUTOPILOT_CWD") or source.get("PWD") or "").strip())
    return candidate if candidate.is_absolute() else Path.cwd()


def run_state_available(run_dir: Path) -> bool:
    from autopilot_storage import exists
    return exists(run_dir / "approved-run.json") and (
        exists(run_dir / "tasks.jsonl") or exists(run_dir / "conduct-plan.json")
    )


def resolve_run_target(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Inspect existing precedence without creating, redirecting or advancing state."""
    source = os.environ if env is None else env
    prefer_process_cwd = env is None
    project_cwd = project_cwd_from_env(source)

    def target(root: Path, project: Path, reason: str, *, derived: bool = False,
               bootstrap: bool = True) -> dict[str, Any]:
        return {"run_dir": root, "project_cwd": project, "reason": reason,
                "derived_run_dir": derived, "bootstrap": bootstrap}

    if run_dir := source.get("GHOST_ALICE_AUTOPILOT_RUN_DIR"):
        return target(Path(run_dir).expanduser(), project_cwd, "explicit-run-dir")
    if explicit_cwd := source.get("GHOST_ALICE_AUTOPILOT_CWD"):
        if not (explicit_project := Path(explicit_cwd)).is_absolute():
            raise ValueError("GHOST_ALICE_AUTOPILOT_CWD must be an absolute path")
        return target(explicit_project / ".autopilot", explicit_project, "project-cwd", derived=True)
    cwd_run_dir = (Path.cwd() if prefer_process_cwd else project_cwd) / ".autopilot"
    if pwd := source.get("PWD"):
        if prefer_process_cwd and run_state_available(cwd_run_dir):
            return target(cwd_run_dir, project_cwd, "existing-process-cwd", bootstrap=False)
        if (pwd_project := Path(pwd)).is_absolute():
            return target(pwd_project / ".autopilot", pwd_project, "pwd", derived=True)
    if run_state_available(cwd_run_dir):
        return target(cwd_run_dir, project_cwd, "existing-cwd", bootstrap=False)
    return target(cwd_run_dir, project_cwd, "cwd", derived=True)

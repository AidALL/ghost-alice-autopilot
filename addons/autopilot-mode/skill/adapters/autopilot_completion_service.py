"""Shared completion recording/application service. Never imports the CLI or State.

Native State and CLI consumers use the same transactional authority service.
Protocol dispatch does not grant approval. The contract module authenticates
archive records and delegates evidence compatibility to Core's policy APIs.
"""
from __future__ import annotations
import copy
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unicodedata
from typing import Any, Mapping
import autopilot_runtime_context as runtime
import autopilot_storage as storage
from autopilot_lineage import current_session_context
from autopilot_work_items import _extract_top_level_section, _parse_completion_evidence, _validate_continue_next_evidence, _with_core_policy_source, read_work_items

PUBLICATION_OBJECT = "completion-publication.json"
RECEIPT_SCHEMA = "autopilot-completion-receipt.v1"
PROVENANCE_SCHEMA = "autopilot-completion-provenance.v1"
SUPPORTED_PLATFORMS = ("codex", "claude")


def _session_material():
    path=Path(__file__).resolve().parents[1]/'scripts/autopilot_session_material.py'
    spec=importlib.util.spec_from_file_location('ap_completion_session_material',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


# Shared read-only helpers, not the native State/CLI entry point.
adapter=SimpleNamespace(current_session_context=current_session_context,
    _session_intent_root_candidates=runtime.session_intent_root_candidates,
    _project_cwd_from_env=runtime.project_cwd_from_env,read_session_material=runtime.read_session_material,
    resolve_run_target=runtime.resolve_run_target,run_is_paused=runtime.run_is_paused,
    _run_state_available=runtime.run_state_available,read_work_items=read_work_items,
    SESSION_MATERIAL=_session_material(),DEFAULT_STOP_CONDITIONS=['budget_exhausted','user_stop'])


def _noop_payload():
    return {"continue":True,"systemMessage":""}

class CompletionRecordingError(ValueError):
    """A typed administrative failure; it is never a tool permission denial."""
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason

    def status(self):
        return {"status": "recording-unavailable", "reason": self.reason, "detail": str(self)}


@dataclass(frozen=True)
class _RetainedCriterionOutcome:
    key: tuple[str, int, str]
    proof_digest: str
    evidence_reference: str | None
    observations: tuple[dict[str, Any], ...]
    changed: bool = False
    error: ValueError | OSError | None = None


@dataclass(frozen=True)
class _RetainedEvidenceScan:
    outcomes: tuple[_RetainedCriterionOutcome, ...] = ()

    @property
    def changed(self):
        return any(outcome.changed for outcome in self.outcomes)

    @property
    def observations(self):
        return sorted((row for outcome in self.outcomes for row in outcome.observations),
            key=lambda row:(row['evidence_reference'],row['criterion_id'],row['locator']))

    @property
    def errors(self):
        return [dict(work_unit_id=outcome.key[0],contract_revision=outcome.key[1],criterion_id=outcome.key[2],
            proof_digest=outcome.proof_digest,evidence_reference=outcome.evidence_reference,
            reason=getattr(outcome.error,'reason','retained-dependency-unavailable'),
            error_type=type(outcome.error).__name__,errno=getattr(outcome.error,'errno',None),detail=str(outcome.error))
            for outcome in self.outcomes if outcome.error is not None]

    def require_backing(self):
        # Foreign/newer or unsupported proof cannot be adopted by publication.
        for outcome in self.outcomes:
            if outcome.error is not None and not isinstance(outcome.error,OSError):raise outcome.error

    def require_unchanged(self):
        if self.changed:
            raise CompletionRecordingError('dependency-mutated','Retained met proof changed during privileged application')


def _require_supported_platform(platform: str) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("completion publication supports only codex and claude")


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
    result = [dict({key: row.get(key) for key in ("id", "summary", "source", "admitted")},
                   **{key: row[key] for key in ("work_unit_id", "contract_revision") if key in row})
              for row in rows if isinstance(row, Mapping)]
    if len(result) != len(rows) or len({row["id"] for row in result}) != len(result):
        raise ValueError("invalid or ambiguous criterion definitions")
    return sorted(result, key=lambda row: str(row["id"]))


def _approval_selection(binding, snapshot):
    """Read qualified admission without adopting unowned criterion records.

    v1 publication addresses local IDs. Explicit current-input admission
    must produce the selected contract; an accumulated approval snapshot
    cannot supply unbound ownership.
    """
    rows = binding.get("acceptance_criteria")
    if not isinstance(rows, list) or any(not isinstance(c, Mapping) for c in rows):
        raise CompletionRecordingError("approval-projection-invalid", "approved criterion snapshots are incompatible")
    owned = {}
    for row in rows:
        if not {"work_unit_id", "contract_revision"} & row.keys():
            continue  # Unowned aliases are context, never current obligations.
        unit, revision, criterion = row.get("work_unit_id"), row.get("contract_revision"), row.get("id")
        if (not isinstance(unit, str) or not unit or type(revision) is not int or revision < 1
                or not isinstance(criterion, str) or not criterion or type(row.get("admitted")) is not bool
                or any(not isinstance(row.get(k), str) or not row[k] for k in ("summary", "source"))):
            raise CompletionRecordingError("approval-projection-invalid", "approved qualified criterion identity is incompatible")
        key = (unit, revision, criterion)
        if key in owned:
            raise CompletionRecordingError("approval-projection-invalid", "approved qualified criterion identity is ambiguous")
        owned[key] = row
    if not owned:
        raise CompletionRecordingError("unbound-legacy-approval", "prior approval has no reliable owned snapshot; use supported current-input admission without adopting old proof")
    selected = [owned[key] for c in snapshot["acceptance_criteria"]
                if (key := (c.get("work_unit_id"), c.get("contract_revision"), c.get("id"))) in owned]
    definitions = _definitions(selected)
    safe_v1 = (len(selected) == len(rows)
               and binding.get("selected_work_units") == snapshot["selected_work_units"])
    return definitions, safe_v1


def _source(source: Mapping[str, str] | None) -> dict[str, str]:
    supplied = dict(os.environ if source is None else source)
    if supplied.get("GHOST_ALICE_PLATFORM"):
        _require_supported_platform(supplied["GHOST_ALICE_PLATFORM"])
    if (supplied.get("GHOST_ALICE_PLATFORM") == "codex" and str(supplied.get("CODEX_THREAD_ID") or "").strip()
            and supplied.get("GHOST_ALICE_SESSION_ID")
            and str(supplied["CODEX_THREAD_ID"]).strip() != supplied["GHOST_ALICE_SESSION_ID"]):
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
    from autopilot_intent_recovery import project_session_material
    material, selected = project_session_material(material, source, Path(adapter.resolve_run_target(source)["run_dir"]))
    if selected["status"] != "selected" and selected["reason"] != "no-current-unmet-criteria":
        raise ValueError("completion recording unavailable: " + selected["reason"])
    state = material["intent_state"]
    criteria = _definitions(state.get("acceptance_criteria"))
    approved, safe_v1 = _approval_selection(binding, {"acceptance_criteria": criteria,
                                                     "selected_work_units": material["selected_work_units"]})
    if binding.get("selected_work_units") != material["selected_work_units"]:
        raise ValueError("completion work unit is unbound or changed; reapprove current work")
    original_input = binding.get("latest_input_event") or {}
    current_input = material["latest_input_event"]
    if any(original_input.get(key) != current_input.get(key) for key in ("event_id", "input_digest")):
        raise ValueError("completion input changed; reapprove current work before new verification")
    if not safe_v1:
        raise CompletionRecordingError("v1-approval-needs-reapproval", "v1 publication requires an explicitly admitted selected contract; unowned records remain unbound")
    if criteria != approved:
        raise ValueError("completion criterion changed; reapprove current work before new verification")
    if adapter.SESSION_MATERIAL.run_summary(state) != (run.get("scope") or {}).get("summary"):
        raise ValueError("completion scope changed; reapprove current work before new verification")
    return {"platform": platform, "session_id": session, "intent_root": str(intent_root),
        "input_event_id": current_input["event_id"], "input_digest": current_input.get("input_digest"),
        "acceptance_criteria": criteria, "scope": copy.deepcopy(run.get("scope")),
        "selected_work_units": copy.deepcopy(material["selected_work_units"]),
        "allowed_surfaces": copy.deepcopy(run.get("allowed_surfaces")),
        "stop_conditions": copy.deepcopy(run.get("stop_conditions")),
        "constraints": copy.deepcopy(state.get("constraints", [])),
        "non_goals": copy.deepcopy(state.get("non_goals", [])),
        "decisions": copy.deepcopy(state.get("decisions", [])),
        "latest_scope": copy.deepcopy(state.get("latest_scope", {})),
        "approval_generation": adapter.SESSION_MATERIAL.approval_generation(run)}


def _original_evidence_source(proof: str, *, allow_partial: bool = False) -> str:
    """Extract one complete top-level section without rewriting original bytes.

    Claim parsing/all-pass validation remains in the existing shared parser.
    This narrow framing check never selects a nested claim's evidence instead.
    """
    markers = {"[completion-check]", "[partial-check]"}
    allowed = markers if allow_partial else {"[completion-check]"}
    lines = proof.splitlines(keepends=True)
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if sum(line.strip() in markers for line in lines) != 1 or first is None or lines[first].strip() not in allowed:
        raise ValueError("evidence derivation requires exactly one original supported proof block")
    headers = []
    offset = sum(len(line) for line in lines[:first + 1])
    for line in lines[first + 1:]:
        body = line.rstrip("\r\n")
        if body.strip() and not body[0].isspace():
            if not re.match(r"^-[ \t]*[A-Za-z0-9_-]+[ \t]*:", body):
                raise ValueError("evidence derivation requires a complete canonical proof block")
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
    raise ValueError("evidence contains an invalid verification timestamp")


def _validate_proof_time(proof: str, verified_at: str, *, allow_partial: bool = False, claim_evidence=None) -> None:
    """Check explicit declarations, without requiring time in original prose.

    The accepted prose grammar is deliberately limited to evidence's `tool-result:ID at T`
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
    if claim_evidence is None:
        parsed = _parse_completion_evidence(proof, allow_partial=allow_partial)
        claim_evidence = [claim["evidence"] for claim in parsed["claims"]]
    evidence = list(claim_evidence)
    evidence.append(_extract_top_level_section(proof, "evidence"))
    legacy = re.compile(r"(?:^|(?<=[; \t]))(?:tool-result:[^\s;]+|"
        r"(?:(?:original|fresh)\s+)?verification(?:\s+output)?)\s+at\s+", re.I)
    times = [value for text in evidence for match in legacy.finditer(text)
             if (value := _legacy_time_after(text[match.end():])) is not None]
    if times and verified_at not in times:
        raise ValueError("evidence verification time conflicts with the original raw timestamp")


@_with_core_policy_source
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
        raise ValueError("publication time differs from original proof")
    key = _digest(origin)
    decision_id = "completion-" + key.removeprefix("sha256:")
    if (decision.get("decision_key") != key or decision.get("decision_id") != decision_id
            or record.get("result") != dict(origin, decision_id=decision_id, status="pending-adapter")):
        raise ValueError("completion publication result differs from immutable provenance")


def _v2_run_is_paused(source):
    """An inactive resolved target needs no completion authority or recovery."""
    target = adapter.resolve_run_target(dict(os.environ if source is None else source))
    project_pause = target.get('project_pause_root')
    return (adapter.run_is_paused(Path(target['run_dir']).expanduser())
        or bool(project_pause and adapter.run_is_paused(Path(project_pause))))


def _v2_claim_scope(hook_input):
    """Core owns native phase and response selection for both Stop consumers."""
    from autopilot_work_items import _load_core_policy_module
    native = _load_core_policy_module('native_transcript.py', 'autopilot_core_native_transcript')
    if native is None or not hasattr(native, 'completion_scope'):
        raise CompletionRecordingError('provider-capability-missing', 'Core native response selection is unavailable')
    return native.completion_scope(hook_input)


def _v2_material(source, *, expected_input=None, transaction=None, claim_scope=None, require_input=True):
    """Current Core selection is authority; protocol dispatch supplies none."""
    current = _source(source)
    intent_root = Path(current['GHOST_ALICE_SESSION_INTENT_ROOT']).expanduser()
    if not intent_root.is_absolute():
        raise CompletionRecordingError('authority-missing','Current intent authority must be an absolute root')
    current['GHOST_ALICE_SESSION_INTENT_ROOT'] = str(intent_root.resolve())
    current['GHOST_ALICE_COMPLETION_SCHEMA'] = 'autopilot-completion-receipt.v2'
    target = adapter.resolve_run_target(current)
    logical_root = Path(target['run_dir']).expanduser().absolute()
    root = logical_root.resolve()
    target = dict(target, logical_run_dir=str(logical_root))
    material = adapter.read_session_material(Path(current['GHOST_ALICE_SESSION_INTENT_ROOT']),
        current['GHOST_ALICE_PLATFORM'], current['GHOST_ALICE_SESSION_ID'], source=current,
        require_input=require_input, expected_input_event_id=expected_input, recover_audit=False)
    if transaction is not None:
        from autopilot_work_items import _load_core_ledger_module
        core = _load_core_ledger_module(material['state_path'], current)
        state = core.read_session_state(root=Path(current['GHOST_ALICE_SESSION_INTENT_ROOT']),
            platform=current['GHOST_ALICE_PLATFORM'], session_id=current['GHOST_ALICE_SESSION_ID'],
            transaction=transaction)
        material = dict(material, intent_state=state)
        material['latest_input_event'] = dict(material['latest_input_event'],
            event_id=state['latest_input_event_id'], input_digest=state['latest_input_digest'])
        if expected_input is not None and state['latest_input_event_id'] != expected_input:
            raise CompletionRecordingError('input-stale', 'Current input changed during completion recording')
    from autopilot_intent_recovery import project_session_material
    material, selected = project_session_material(material, current, root, transaction=transaction,claim_scope=claim_scope)
    reference_token = current.get('GHOST_ALICE_COMPLETION_TARGET_RECEIPT')
    if reference_token and material.get('selected_work_units') and selected.get('reason')!='response-no-publication':
        from autopilot_work_items import _load_core_ledger_module
        import autopilot_completion_contract as contract
        core = _load_core_ledger_module(material['state_path'], current)
        def check_reference(connection):
            store = storage.RunStore(connection, root, _v2_authority(current), Path(current['GHOST_ALICE_SESSION_INTENT_ROOT']))
            receipt = store.completion_read('receipt', reference_token)
            if receipt.get('schema_version') != contract.RECEIPT_SCHEMA:
                raise CompletionRecordingError('target-invalid', 'Target reference is not a v2 receipt')
            reference = receipt['target']
            contract.validate_target(reference)
            owner = dict(platform=current['GHOST_ALICE_PLATFORM'], session_id=current['GHOST_ALICE_SESSION_ID'])
            state = material['intent_state']
            binding = dict(platform=owner['platform'],session_id=owner['session_id'],
                intent_root=current['GHOST_ALICE_SESSION_INTENT_ROOT'],input_event_id=state['latest_input_event_id'],input_digest=state['latest_input_digest'])
            if (reference['path'] != str(logical_root) or reference['authority'] != binding['intent_root']
                    or reference['owner'] != owner or receipt['binding'] != binding):
                raise CompletionRecordingError('target-stale', 'Target reference differs from current authority/path/binding')
            target.update(reason='explicit-run-dir' if reference['selection_origin']=='explicit' else 'receipt-derived',
                derived_run_dir=reference['selection_origin']=='derived', authoritative_reference=reference)
        if transaction is not None:
            check_reference(transaction)
        else:
            with core.storage_transaction(Path(current['GHOST_ALICE_SESSION_INTENT_ROOT'])) as connection:
                check_reference(connection)
    return current, root, target, material, selected


def _v2_contracts(current, root, target, material):
    import autopilot_completion_contract as contract
    owner = {k:current['GHOST_ALICE_'+k.upper()] for k in ('platform','session_id')}
    reference = dict(path=target.get('logical_run_dir',str(root)), authority=current['GHOST_ALICE_SESSION_INTENT_ROOT'],
        owner=owner, selection_origin='explicit' if target['reason']=='explicit-run-dir' else 'derived')
    contract.validate_target(reference)
    state = material['intent_state']
    binding = dict(platform=owner['platform'], session_id=owner['session_id'],
        intent_root=reference['authority'], input_event_id=state['latest_input_event_id'],
        input_digest=state['latest_input_digest'])
    rows = state['acceptance_criteria']
    contracts = []
    for unit in material['selected_work_units']:
        if unit['owner'] != owner:
            raise CompletionRecordingError('owner-mismatch', 'Selected unit belongs to another owner')
        criteria = [contract.definition(row) for row in rows
            if (row.get('work_unit_id'),row.get('contract_revision')) ==
               (unit['work_unit_id'],unit['contract_revision'])]
        snapshot = dict(work_unit_id=unit['work_unit_id'],contract_revision=unit['contract_revision'],
            owner=copy.deepcopy(owner),criteria=criteria,
            relevant_context={k:copy.deepcopy(state.get(k,{} if k=='latest_scope' else []))
                              for k in ('constraints','non_goals','decisions','latest_scope')})
        contract.validate_contract(snapshot)
        contracts.append(snapshot)
    return binding, reference, contracts


def _v2_authority(current):
    return dict(schema_version='autopilot-authority.v1',kind='session',
        ledger_root=current['GHOST_ALICE_SESSION_INTENT_ROOT'],
        platform=current['GHOST_ALICE_PLATFORM'],session_id=current['GHOST_ALICE_SESSION_ID'])


def _capture_v2_locked(store, binding, target, snapshot, selected_criteria, correction_of=None):
    import autopilot_completion_contract as contract
    previous = store.completion_unit(snapshot['work_unit_id'])
    receipt = None
    if previous is not None:
        receipt = store.completion_read('receipt', previous['receipt_token'])
        if any(receipt.get(k)!=v for k,v in dict(binding=binding,target=target,contract=snapshot,selected_criteria=selected_criteria,correction_of=correction_of).items()):
            receipt = None
    if receipt is not None:
        origin = store.completion_read('origin', receipt['origin_token'])
        contract.validate_origin(origin,receipt['origin_token'])
        if origin['contract']!=snapshot or origin['target']!=target:
            raise CompletionRecordingError('origin-stale','Original capture no longer matches selected contract')
        return receipt, previous
    now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    origin = dict(schema_version=contract.ORIGIN_SCHEMA, contract=copy.deepcopy(snapshot),
        target=copy.deepcopy(target), input_event_id=binding['input_event_id'],
        input_digest=binding['input_digest'], captured_at=now)
    origin_token = store.completion_put('origin',origin)
    receipt = dict(schema_version=contract.RECEIPT_SCHEMA,binding=copy.deepcopy(binding),
        target=copy.deepcopy(target),contract=copy.deepcopy(snapshot),origin_token=origin_token,prepared_at=now,
        selected_criteria=copy.deepcopy(selected_criteria),**(dict(correction_of=correction_of) if correction_of else {}))
    token = store.completion_put('receipt',receipt)
    state = dict(work_unit_id=snapshot['work_unit_id'],contract_revision=snapshot['contract_revision'],
        receipt_token=token,status='prepared',publication_reference=None,
        applied_requests=(previous or {}).get('applied_requests',[]),
        publications=(previous or {}).get('publications',{}),
        application_references=(previous or {}).get('application_references',{}),
        correction_publications=(previous or {}).get('correction_publications',[]),
        correction_heads=(previous or {}).get('correction_heads',{}))
    store.completion_set_unit(state)
    return receipt,state


def _v2_receipt_output(receipt, state, source=None):
    from autopilot_messages import completion_command
    binding = receipt['binding']; root = receipt['target']['path']; token = state['receipt_token']
    result = dict(schema_version=receipt['schema_version'],receipt_token=token,
        prepared_at=receipt['prepared_at'],origin_token=receipt['origin_token'],
        work_unit_id=receipt['contract']['work_unit_id'],contract_revision=receipt['contract']['contract_revision'],
        criterion_ids=sorted(c['criterion_id'] for c in receipt['selected_criteria']),
        input_event_id=binding['input_event_id'],target_reference=receipt['target'],run_dir=root,status=state['status'])
    result['prepare_command'] = completion_command(binding,root,'prepare','--intent-root',binding['intent_root'],
        '--platform',binding['platform'],'--session-id',binding['session_id'],'--input-event-id',binding['input_event_id'],target_receipt=token,source=source)
    result['publish_command'] = completion_command(binding,root,'publish','--receipt-token',token,
        '--verified-at','ORIGINAL_ISO_TIME','--completion-file','-',target_receipt=token,source=source)
    result['verification_time_command'] = completion_command(binding,root,'verification-time',source=source)
    return result


from contextlib import contextmanager


@contextmanager
def _v2_recording_transaction(root, current, target, *, preflight=None):
    manager = storage.transaction(root,current,authority=_v2_authority(current),preflight=preflight,migrate_legacy=False,classify_target_errors=True)
    try:
        store = manager.__enter__()
    except storage.TargetAcquisitionFailure as exc:
        if not target['derived_run_dir']:
            raise exc.error from exc
        yield None
        return
    try:
        yield store
    except BaseException:
        if not manager.__exit__(*__import__('sys').exc_info()):
            raise
    else:
        manager.__exit__(None,None,None)


@_with_core_policy_source
def _prepare_completion_v2(*, intent_root, platform, session_id, input_event_id, source, claim_scope=None, correction_of=None):
    _require_supported_platform(platform)
    supplied=dict(os.environ if source is None else source)
    coordinates=dict(GHOST_ALICE_SESSION_INTENT_ROOT=str(Path(intent_root).expanduser().resolve()),
        GHOST_ALICE_PLATFORM=platform,GHOST_ALICE_SESSION_ID=session_id)
    for key,value in coordinates.items():
        observed=supplied.get(key)
        if key=='GHOST_ALICE_SESSION_INTENT_ROOT' and observed:
            observed=str(Path(observed).expanduser().resolve())
        if observed and observed!=value:
            raise CompletionRecordingError('authority-mismatch','Completion coordinate differs from current '+key)
        supplied[key]=value
    if _v2_run_is_paused(supplied):
        return dict(status='skipped', reason='autopilot-off')
    current,root,target,material,selected=_v2_material(supplied,expected_input=input_event_id,claim_scope=claim_scope)
    if selected['status']!='selected' and selected.get('reason')!='no-current-unmet-criteria':
        return selected
    # No selected current execution unit means no Ghost-ALICE Autopilot table/descriptor creation.
    if not material.get('selected_work_units'):
        return selected
    checked = None
    def preflight(connection):
        nonlocal checked
        locked_current, locked_root, locked_target, locked, selection = _v2_material(current, expected_input=input_event_id, transaction=connection,claim_scope=claim_scope)
        if (selection['status']!='selected' and selection.get('reason')!='no-current-unmet-criteria') or locked['intent_state']!=material['intent_state']:
            raise CompletionRecordingError('input-stale','Current route/contract changed before preparation')
        if (locked_current, locked_root, locked_target) != (current, root, target):
            raise CompletionRecordingError('target-stale','Current preparation target changed before the locked check')
        checked = (connection, locked)
    with _v2_recording_transaction(root,current,target,preflight=preflight) as store:
        if store is None:
            return dict(status='recording-unavailable',reason='derived-target-unwritable')
        if (checked is None or checked[0] is not store.connection or store.run_dir != root
                or store.key != str(root) or store.root != Path(current['GHOST_ALICE_SESSION_INTENT_ROOT'])
                or any(store.authority.get(k)!=v for k,v in _v2_authority(current).items())):
            raise CompletionRecordingError('target-stale','Prepared store differs from the original locked check')
        locked = checked[1]
        binding,reference,snapshots=_v2_contracts(current,root,target,locked)
        results=[]
        for snapshot in snapshots:
            predecessor=None
            if correction_of is not None:
                previous=store.completion_unit(snapshot['work_unit_id'])
                predecessor=_correction_predecessor(store,previous,correction_of,snapshot,reference)
            selected_criteria=[dict(work_unit_id=c['work_unit_id'],contract_revision=c['contract_revision'],criterion_id=c['id'])
                for c in locked['intent_state']['acceptance_criteria']
                if c['work_unit_id']==snapshot['work_unit_id'] and (c.get('status')!='met' or predecessor is not None)]
            if selected_criteria:
                results.append(_v2_receipt_output(*_capture_v2_locked(store,binding,reference,snapshot,selected_criteria,correction_of),source=current))
            elif (previous := store.completion_unit(snapshot['work_unit_id'])) is not None:
                receipt=store.completion_read('receipt',previous['receipt_token'])
                if receipt['binding']==binding and receipt['contract']==snapshot and receipt['target']==reference:
                    results.append(_v2_receipt_output(receipt,previous,source=current))
        if not results:
            return dict(status='skipped',reason='no-current-unmet-criteria')
        return results[0] if len(results)==1 else dict(status='prepared',schema_version='autopilot-completion-receipt.v2',units=results)


@_with_core_policy_source
def v2_preflight_result(result, source):
    """Keep current receipts; expose guidance when the selected work changes."""
    if _v2_run_is_paused(source):
        return dict(status='skipped', reason='autopilot-off')
    current,root,target,_,_ = _v2_material(source)
    rows = result.get('units',[result])
    notices = []
    with _v2_recording_transaction(root,current,target) as store:
        if store is None:
            return dict(status='recording-unavailable',reason='derived-target-unwritable')
        for row in rows:
            marker = dict(kind='preflight-work', work_unit_id=row['work_unit_id'],
                contract_revision=row['contract_revision'], criterion_ids=sorted(row['criterion_ids']),
                target_reference=row['target_reference'])
            try:
                store.completion_read('notice',_digest(marker))
            except FileNotFoundError:
                store.completion_put('notice',marker)
                notices.append('\n'.join([
                    'work-unit: '+row['work_unit_id'], 'contract-revision: '+str(row['contract_revision']),
                    'publication: native Stop consumes the Core-reviewed response']))
    if notices:
        guidance = ['[autopilot]', 'completion-preflight: prepared',
            'For completion or partial-completion claims, follow verification-before-completion/references/autopilot-publication.md, section Prepare and record the response review. Native Stop handles publication of authenticated supported proof. Preparation and original results remain available; do not repeat them for routine explanations.']
        return dict(result, additional_context='\n'.join(guidance + notices))
    return result


def _dependency_snapshots(locators):
    import autopilot_completion_contract as contract
    if not isinstance(locators,list) or not all(isinstance(p,str) and Path(p).is_absolute() for p in locators):
        raise CompletionRecordingError('dependency-invalid','Dependency locators must be absolute paths')
    # Keep the caller's absolute logical name, including symlinked parents.
    # Each reuse/application must read through that same name again.
    rows=[dict(locator=p,digest='sha256:'+hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in locators]
    contract.validate_dependencies(rows)
    return rows


def _v2_records(store,references,*,read_dependencies=True):
    if not isinstance(references,list) or not references or not all(isinstance(ref,str) for ref in references) or len(set(references))!=len(references):
        raise CompletionRecordingError('evidence-unbound','Exact accepted original evidence references are required')
    try:
        records={ref:store.completion_read('evidence',ref) for ref in references}
        origins={r['origin_token']:store.completion_read('origin',r['origin_token']) for r in records.values()}
    except (FileNotFoundError,KeyError) as exc:
        raise CompletionRecordingError('evidence-unbound','Unbound evidence cannot be promoted into fresh v2 proof') from exc
    if not read_dependencies:return records,origins,{}
    locators=sorted({d['locator'] for r in records.values() for d in r.get('dependencies',[])})
    dependencies={}
    for locator in locators:
        try:
            dependencies[locator]='sha256:'+hashlib.sha256(Path(locator).read_bytes()).hexdigest()
        except FileNotFoundError:
            dependencies[locator]=None  # Missing relevant artifact affects only its original records.
    return records,origins,dependencies


def _v2_validate_current_receipt(store,receipt_token,current,root,target,material):
    import autopilot_completion_contract as contract
    try:
        receipt=store.completion_read('receipt',receipt_token)
    except FileNotFoundError as exc:
        raise CompletionRecordingError('receipt-missing','Missing v2 receipt; original proof remains unchanged') from exc
    if receipt.get('schema_version')!=contract.RECEIPT_SCHEMA:
        raise CompletionRecordingError('receipt-incompatible','Unsupported/legacy publication receipt')
    binding,reference,snapshots=_v2_contracts(current,root,target,material)
    snapshot=next((s for s in snapshots if s['work_unit_id']==receipt['contract']['work_unit_id']),None)
    if receipt['binding']!=binding or receipt['target']!=reference or receipt['contract']!=snapshot:
        raise CompletionRecordingError('receipt-stale','Current input, owner, selection or contract differs from publication receipt')
    state=store.completion_unit(snapshot['work_unit_id'])
    if state is None or (state['receipt_token']!=receipt_token and receipt_token not in state.get('publications',{})):
        raise CompletionRecordingError('receipt-stale','Current per-unit receipt differs')
    origin=store.completion_read('origin',receipt['origin_token'])
    contract.validate_origin(origin,receipt['origin_token'])
    if origin['contract']!=snapshot or origin['target']!=reference or origin['captured_at']!=receipt['prepared_at']:
        raise CompletionRecordingError('origin-stale','Receipt lost its original prospective capture')
    return receipt,state,origin


def _v2_retained_capture(store, token, current_receipt):
    """Read original capture provenance; current receipt alone grants publication."""
    import autopilot_completion_contract as contract
    try:
        receipt = store.completion_read('receipt', token)
        origin = store.completion_read('origin', receipt['origin_token'])
    except FileNotFoundError as exc:
        raise CompletionRecordingError('origin-receipt-missing', 'Original prepared receipt is unavailable') from exc
    if receipt.get('schema_version') != contract.RECEIPT_SCHEMA:
        raise CompletionRecordingError('origin-receipt-incompatible', 'Original receipt must use v2')
    contract.receipt_selection_keys(receipt)
    # Do not use compatible_definition here: first ingestion must match the
    # original full contract/revision/context, not infer historical authority.
    if (any(receipt['binding'].get(k) != current_receipt['binding'][k]
            for k in ('platform', 'session_id', 'intent_root'))
            or receipt['target'] != current_receipt['target']
            or receipt['contract'] != current_receipt['contract']):
        raise CompletionRecordingError('origin-receipt-incompatible', 'Original receipt owner, target, contract or context differs')
    contract.validate_origin(origin, receipt['origin_token'])
    if (origin['contract'] != receipt['contract'] or origin['target'] != receipt['target']
            or origin['captured_at'] != receipt['prepared_at']
            or any(origin[k] != receipt['binding'][k] for k in ('input_event_id', 'input_digest'))):
        raise CompletionRecordingError('origin-stale', 'Original receipt lost its prospective capture binding')
    if contract.timestamp(receipt['prepared_at']) > contract.timestamp(current_receipt['prepared_at']):
        raise CompletionRecordingError('origin-stale', 'Original capture must not follow current receipt')
    return receipt, origin


def _correction_predecessor(store,state,reference,snapshot,target):
    import autopilot_completion_contract as contract
    if state is None:raise CompletionRecordingError('correction-invalid','Correction requires an accepted publication')
    try:publication=store.completion_read('publication',reference)
    except FileNotFoundError as exc:raise CompletionRecordingError('correction-invalid','Correction predecessor is unavailable') from exc
    if (not contract.publication_member(state,reference,publication)
            or publication['request']['contract']!=snapshot or publication['request']['target']!=target):
        raise CompletionRecordingError('correction-invalid','Correction predecessor belongs to another contract, owner or target')
    return publication


def _correction_prior_bindings(store,state,publication):
    """Read the last accepted binding for each original criterion in a chain."""
    import autopilot_completion_contract as contract
    chain=[];seen=set();current=publication
    while True:
        reference=contract.digest(current)
        if reference in seen:raise CompletionRecordingError('correction-invalid','Cyclic correction archive')
        seen.add(reference);chain.append(current)
        correction=current['request'].get('correction')
        if correction is None:break
        current=_correction_predecessor(store,state,correction['supersedes'],current['request']['contract'],current['request']['target'])
    return {(c['work_unit_id'],c['contract_revision'],c['criterion_id']):c['evidence_reference']
        for item in reversed(chain) for c in item['request']['selected_criteria']}


def _correction_binding(store,state,receipt,correction):
    import autopilot_completion_contract as contract
    if (not isinstance(correction,dict) or set(correction)!={'supersedes','affected_criteria','reason'}
            or not isinstance(correction['reason'],str) or not correction['reason'].strip()
            or receipt.get('correction_of')!=correction['supersedes']):
        raise CompletionRecordingError('correction-invalid','Correction needs its prepared predecessor, affected criteria and reason')
    predecessor=_correction_predecessor(store,state,correction['supersedes'],receipt['contract'],receipt['target'])
    rows=correction['affected_criteria']
    if not isinstance(rows,list) or not rows or any(not isinstance(c,dict) or set(c)!={'work_unit_id','contract_revision','criterion_id'} for c in rows):
        raise CompletionRecordingError('correction-invalid','Exact affected qualified criteria are required')
    keys=[contract.criterion_key(dict(c,id=c['criterion_id'])) for c in rows]
    prior=set(_correction_prior_bindings(store,state,predecessor))
    current={(c['work_unit_id'],c['contract_revision'],c['criterion_id']) for c in receipt['selected_criteria']}
    if len(set(keys))!=len(keys) or not set(keys)<=prior & current:
        raise CompletionRecordingError('correction-invalid','Affected criterion is outside the accepted current contract')
    return predecessor,set(keys)


@_with_core_policy_source
def _publish_completion_v2(*,receipt_token,completion_check,verified_at,evidence_source,source,
                           evidence_references,dependencies,criterion_dependencies,origin_receipt_token=None,original_dependency_snapshots=None,correction=None):
    import autopilot_completion_contract as contract
    if _v2_run_is_paused(source):
        return dict(status='skipped', reason='autopilot-off')
    # The helper is an explicit publication operation, including reference-only
    # reuse. Preserve that scope through the lock without inventing claim text.
    claim_scope=dict(phase='publication',text=completion_check if isinstance(completion_check,str) else '')
    current,root,target,material,selection=_v2_material(source,claim_scope=claim_scope)
    if selection['status']!='selected' and selection.get('reason')!='no-current-unmet-criteria':
        raise CompletionRecordingError(selection['reason'],'Current completion route is unavailable')
    with _v2_recording_transaction(root,current,target) as store:
        if store is None:
            return dict(status='recording-unavailable',reason='derived-target-unwritable')
        current,root,target,material,selection=_v2_material(current,transaction=store.connection,claim_scope=claim_scope)
        if selection['status']!='selected' and selection.get('reason')!='no-current-unmet-criteria':
            raise CompletionRecordingError(selection['reason'],'Current completion route is unavailable')
        receipt,state,origin=_v2_validate_current_receipt(store,receipt_token,current,root,target,material)
        now=datetime.now(timezone.utc).isoformat()
        predecessor,affected=(None,None) if correction is None else _correction_binding(store,state,receipt,correction)
        if receipt.get('correction_of') and correction is None:
            raise CompletionRecordingError('correction-invalid','Correction receipt requires its explicit correction binding')
        if predecessor is not None and origin_receipt_token is None and evidence_references is None:
            prior_records,_,_=_v2_records(store,predecessor['request']['evidence_references'],read_dependencies=False)
            eligible=[r for r in prior_records.values() if r['verified_at']==verified_at]
            if len(eligible)!=1:raise CompletionRecordingError('correction-invalid','Correction must retain one original verification time')
            prior=eligible[0]
            prior_receipt=store.completion_read('receipt',predecessor['receipt_token'])
            # Corrections retain the proof's capture, not the newer publication receipt.
            origin_receipt_token=next((token for token in state.get('publications',{})
                if store.completion_read('receipt',token)['origin_token']==prior['origin_token']),None)
            if origin_receipt_token is None and prior_receipt['origin_token']==prior['origin_token']:
                origin_receipt_token=predecessor['receipt_token']
            if origin_receipt_token is None:raise CompletionRecordingError('origin-receipt-missing','Correction original capture is unavailable')
            original_dependency_snapshots=copy.deepcopy(prior['dependencies'])
            if criterion_dependencies is None and prior['dependency_mapping']=='explicit':
                criterion_dependencies=copy.deepcopy(prior['criterion_dependencies'])
        if original_dependency_snapshots is not None and origin_receipt_token is None:
            raise CompletionRecordingError('dependency-invalid', 'Original dependency snapshots require original capture receipt')
        if evidence_references is not None:
            if completion_check is not None or verified_at is not None or dependencies or criterion_dependencies is not None or origin_receipt_token is not None:
                raise CompletionRecordingError('evidence-relabel','Reference reuse cannot supply new proof, time or dependencies')
            records,origins,current_dependencies=_v2_records(store,evidence_references)
            # A stored row alone is not accepted provenance. Reuse the same
            # archive authenticator as Core reporting under this transaction.
            from autopilot_claim_evidence import ClaimArchive
            archive=ClaimArchive(store.connection,store.root,receipt['contract']['owner'],current)
            for ref,record in records.items():
                if archive.accepted_evidence(receipt['contract']['work_unit_id'],ref,now)!=record:
                    raise CompletionRecordingError('evidence-unbound','Original reference lacks its accepted archive chain')
        else:
            proof_receipt = receipt
            if origin_receipt_token is not None:
                if original_dependency_snapshots is None:
                    raise CompletionRecordingError('dependency-invalid', 'Original dependency snapshots must be explicit, including an empty list')
                if dependencies:
                    raise CompletionRecordingError('dependency-invalid', 'Retained proof cannot acquire replacement dependency snapshots')
                contract.validate_dependencies(original_dependency_snapshots)
                proof_receipt, origin = _v2_retained_capture(store, origin_receipt_token, receipt)
            from autopilot_work_items import _load_core_policy_module
            assessor=_load_core_policy_module('completion_assessment.py','ap_core_completion_assessment')
            if assessor is None:raise CompletionRecordingError('provider-capability-missing','Core completion_assessment provider is unavailable')
            assessment=assessor.lookup_publication_assessment(current,proof_receipt['contract'],completion_check,
                verified_at,transaction=store.connection)
            completion_check=assessment.candidate['proof']
            proof_hash=contract.proof_digest(completion_check)
            contract.timestamp(verified_at)
            _validate_proof_time(completion_check,contract.original_verification_time(completion_check,verified_at),
                allow_partial=True,claim_evidence=[b['evidence'] for b in assessment.candidate['bindings']])
            locator=evidence_source if evidence_source is not None else _original_evidence_source(completion_check,allow_partial=True)
            rows=assessor.assessment_definitions(assessment,proof_receipt['contract'],completion_check,verified_at)
            capture_reader=getattr(assessor,'assessment_capture_binding',None)
            capture_binding=(capture_reader(assessment,proof_receipt['contract'],completion_check,verified_at)
                if callable(capture_reader) else None)
            if (origin_receipt_token is None and capture_binding is not None
                    and capture_binding['receipt_token'] != receipt_token):
                # Core's immutable review names the authenticated original
                # capture; the caller's current receipt still grants publication.
                if dependencies or criterion_dependencies is not None:
                    raise CompletionRecordingError('dependency-invalid',
                        'Retained capture requires original dependency snapshots for explicit dependencies')
                origin_receipt_token = capture_binding['receipt_token']
                original_dependency_snapshots = []
                proof_receipt, origin = _v2_retained_capture(store, origin_receipt_token, receipt)
            if not {contract.criterion_key(row) for row in rows} <= contract.receipt_selection_keys(proof_receipt):
                raise CompletionRecordingError('receipt-coverage-invalid', 'Proof exceeds the original receipt selection')
            if criterion_dependencies is not None:
                if dependencies:
                    raise CompletionRecordingError('dependency-invalid','Choose explicit qualified mapping or shared dependencies')
                if not isinstance(criterion_dependencies,list) or not all(isinstance(d,dict) and isinstance(d.get('locators'),list)
                        and all(isinstance(p,str) and Path(p).is_absolute() for p in d['locators']) for d in criterion_dependencies):
                    raise CompletionRecordingError('dependency-invalid','Invalid qualified dependency relation')
                locators=sorted({p for d in criterion_dependencies for p in d['locators']})
                if origin_receipt_token is not None:
                    snapshots=copy.deepcopy(original_dependency_snapshots)
                    if set(locators)!={d['locator'] for d in snapshots}:
                        raise CompletionRecordingError('dependency-invalid', 'Original dependency snapshots must cover exact mapped locators')
                else:
                    snapshots=_dependency_snapshots(locators)
                criterion_dependencies=[dict(d,locators=sorted(d['locators']))
                    for d in criterion_dependencies]
            else:
                snapshots=copy.deepcopy(original_dependency_snapshots) if origin_receipt_token is not None else _dependency_snapshots(dependencies or [])
            record=contract.evidence_record(origin=origin,origin_token=proof_receipt['origin_token'],proof=completion_check,
                proof_locator=locator,verified_at=verified_at,criteria=rows,dependencies=snapshots,now=now,
                criterion_dependencies=criterion_dependencies,
                causal_assessment_reference=assessment.reference if capture_binding is not None else None,
                source=current,transaction=store.connection)
            ref=store.completion_put('evidence',record)
            records={ref:record};origins={proof_receipt['origin_token']:origin}
            observed=_dependency_snapshots([d['locator'] for d in snapshots]) if origin_receipt_token is not None else snapshots
            current_dependencies={d['locator']:d['digest'] for d in observed}
        if correction is not None:
            from autopilot_work_items import _load_core_policy_module
            assessor=_load_core_policy_module('completion_assessment.py','ap_core_completion_assessment')
            if assessor is None:raise CompletionRecordingError('provider-capability-missing','Core completion_assessment provider is unavailable')
            prior_bindings=_correction_prior_bindings(store,state,predecessor)
            for key in affected:
                candidates=[record for record in records.values() if any(contract.criterion_key(c)==key for c in record['criteria'])]
                if len(candidates)!=1:raise CompletionRecordingError('correction-invalid','Affected criterion needs one exact corrected proof')
                record=candidates[0]
                assessor.authorize_correction(current,criterion=dict(work_unit_id=key[0],contract_revision=key[1],criterion_id=key[2]),
                    proof=record['proof'],verified_at=record['verified_at'],contract=record['contract'],
                    original_evidence_reference=prior_bindings[key],transaction=store.connection)
        coverage=contract.validate_references(records,origins,receipt['contract'],receipt['target'],current_dependencies,now,
            allow_empty=correction is not None,source=current,transaction=store.connection,accepted=evidence_references is not None)
        requested_keys={(c['work_unit_id'],c['contract_revision'],c['criterion_id']) for c in receipt['selected_criteria']}
        coverage={key:ref for key,ref in coverage.items() if key in requested_keys and (affected is None or key in affected)}
        if not coverage and correction is None:
            raise CompletionRecordingError('criterion-mismatch','No original evidence covers current requested conditions')
        used=set(coverage.values())
        records={ref:record for ref,record in records.items() if ref in used or correction is not None}
        request=contract.publication_request(binding=receipt['binding'],contract=receipt['contract'],target=receipt['target'],
            coverage=coverage,records=records,created_at=receipt['prepared_at'],checked_at=now,correction=correction)
        previous_reference=(state.get('correction_heads',{}).get(correction['supersedes']) if correction else state.get('publications',{}).get(receipt_token))
        if previous_reference:
            previous=store.completion_read('publication',previous_reference)
            if previous['request']['idempotency_key']!=request['idempotency_key']:
                raise CompletionRecordingError('publication-conflict','Accepted request cannot be overwritten by different proof')
            return dict(previous['result'],publication_reference=previous_reference)
        original_rows=[dict(evidence_reference=ref,verified_at=r['verified_at'],captured_at=r['captured_at'],
            origin_token=r['origin_token'],proof_locator=r['proof_locator'],proof_digest=r['proof_digest'],
            work_unit_id=r['contract']['work_unit_id'],contract_revision=r['contract']['contract_revision'],
            criteria=copy.deepcopy(r['criteria']),dependencies=copy.deepcopy(r['dependencies']),
            criterion_dependencies=copy.deepcopy(r['criterion_dependencies']),target_reference=copy.deepcopy(r['target']))
            for ref,r in sorted(records.items())]
        times={r['verified_at'] for r in records.values()}
        result=dict(schema_version=contract.REQUEST_SCHEMA,status='pending-adapter',work_unit_id=receipt['contract']['work_unit_id'],
            contract_revision=receipt['contract']['contract_revision'],receipt_token=receipt_token,
            evidence_references=sorted(records),evidence_sources=sorted({r['proof_locator'] for r in records.values()}),
            original_evidence=original_rows,verified_at=next(iter(times)) if len(times)==1 else None,
            request_created_at=request['request_created_at'],publication_checked_at=request['publication_checked_at'],
            idempotency_key=request['idempotency_key'],
            reopened_criteria=[dict(work_unit_id=c['work_unit_id'],contract_revision=c['contract_revision'],criterion_id=c['id'])
                for c in material['intent_state']['acceptance_criteria']
                if c['work_unit_id']==receipt['contract']['work_unit_id'] and c.get('status')!='met'
                and contract.criterion_key(c) not in coverage])
        publication=dict(request=request,result=result,receipt_token=receipt_token)
        reference=store.completion_put('publication',publication)
        updates=(dict(correction_publications=[*state.get('correction_publications',[]),reference],
            correction_heads={**state.get('correction_heads',{}),correction['supersedes']:reference}) if correction else
            dict(publications={**state.get('publications',{}),receipt_token:reference}))
        store.completion_set_unit(dict(state,status='pending-adapter',publication_reference=reference,**updates))
        return dict(result,publication_reference=reference)


def _validate_v2_publication(store,publication,current,root,target,material):
    import autopilot_completion_contract as contract
    request=publication['request']
    receipt,state,_=_v2_validate_current_receipt(store,publication['receipt_token'],current,root,target,material)
    records,origins,dependencies=_v2_records(store,request['evidence_references'])
    now=datetime.now(timezone.utc).isoformat()
    # Reconstruct the accepted request from immutable original records first.
    # Current artifact compatibility decides application coverage separately;
    # it must never rewrite the accepted request or the proof's original time.
    original_dependencies={d['locator']:d['digest'] for record in records.values() for d in record['dependencies']}
    original_coverage=contract.archive_coverage(records,origins,receipt['contract'],receipt['target'],now,source=current,transaction=store.connection)
    requested_keys={(c['work_unit_id'],c['contract_revision'],c['criterion_id']) for c in receipt['selected_criteria']}
    recorded_coverage={(c['work_unit_id'],c['contract_revision'],c['criterion_id']):c['evidence_reference']
        for c in request['selected_criteria']}
    if any(key not in requested_keys or original_coverage.get(key)!=ref for key,ref in recorded_coverage.items()):
        raise CompletionRecordingError('publication-tampered','Accepted selected coverage lost original binding')
    original_coverage=recorded_coverage
    expected=contract.publication_request(binding=receipt['binding'],contract=receipt['contract'],target=receipt['target'],
        coverage=original_coverage,records=records,created_at=receipt['prepared_at'],checked_at=request['publication_checked_at'],correction=request.get('correction'))
    if request!=expected or request['schema_version']!=contract.REQUEST_SCHEMA:
        raise CompletionRecordingError('publication-tampered','Queued request differs from immutable accepted original proof')
    if (not contract.publication_member(state,contract.digest(publication),publication)
            or publication['result']['idempotency_key']!=request['idempotency_key']
            or timestamp_future(request['publication_checked_at'],now)):
        raise CompletionRecordingError('publication-tampered','Publication/application binding differs')
    if contract.digest(publication) in state.get('correction_heads',{}):
        raise CompletionRecordingError('publication-superseded','A corrected predecessor cannot apply again')
    if request.get('correction') is not None:
        _correction_binding(store,state,receipt,request['correction'])
    coverage=contract.validate_references(records,origins,receipt['contract'],receipt['target'],dependencies,now,allow_empty=True,source=current,transaction=store.connection,accepted=True)
    coverage={key:ref for key,ref in coverage.items() if key in recorded_coverage}
    selected=[c for c in request['selected_criteria']
        if (c['work_unit_id'],c['contract_revision'],c['criterion_id']) in coverage]
    used={c['evidence_reference'] for c in selected}
    effective=dict(request,selected_criteria=selected,evidence_references=sorted(used))
    return effective,{ref:record for ref,record in records.items() if ref in used},state


def _apply_correction(store,publication,state,current,material):
    import autopilot_completion_contract as contract
    from autopilot_work_items import _load_core_ledger_module
    request=publication['request']; correction=request.get('correction')
    if correction is None:return
    receipt=store.completion_read('receipt',publication['receipt_token'])
    predecessor,affected=_correction_binding(store,state,receipt,correction)
    # Repeated application is harmless; old requests can never withdraw newer proof.
    key=contract.digest([request['idempotency_key'],request['selected_criteria']])
    if key in state.get('applied_requests',[]):return
    prior_bindings=_correction_prior_bindings(store,state,predecessor)
    original={key:store.completion_read('evidence',reference) for key,reference in prior_bindings.items()}
    records,_,_=_v2_records(store,request['evidence_references'],read_dependencies=False)
    binding=request['binding']
    ledger=_load_core_ledger_module(Path(binding['intent_root'])/binding['platform']/binding['session_id']/'intent-state.json',current)
    for row in material['intent_state']['acceptance_criteria']:
        qualified=contract.criterion_key(row)
        if qualified not in affected or row.get('status')!='met':continue
        if row.get('met_completion_check_digest','').removeprefix('sha256:')!=original[qualified]['proof_digest'].removeprefix('sha256:'):
            raise CompletionRecordingError('correction-stale','A newer met proof cannot be withdrawn by this correction')
        candidates=[record for record in records.values() if any(contract.criterion_key(c)==qualified for c in record['criteria'])]
        if len(candidates)!=1:raise CompletionRecordingError('correction-invalid','Affected criterion requires one Core-assessed corrected proof')
        record=candidates[0]
        ledger.invalidate_acceptance_criterion(root=Path(binding['intent_root']),platform=binding['platform'],session_id=binding['session_id'],
            criterion_id=qualified[2],expected_input_event_id=binding['input_event_id'],expected_criterion=contract.definition(row),
            expected_completion_check_digest=original[qualified]['proof_digest'].removeprefix('sha256:'),
            invalidation_reason='completion-correction',transaction=store.connection,correction_source=current,
            correction_assessment=dict(contract=record['contract'],proof=record['proof'],verified_at=record['verified_at'],
                original_evidence_reference=prior_bindings[qualified]))


def timestamp_future(value,now):
    import autopilot_completion_contract as contract
    return contract.timestamp(value)>contract.timestamp(now)


def _core_dependency_assessment(record, evidence_reference, criterion_id):
    """Consume the Core dependency-policy observation of the original mapped bytes."""
    from autopilot_completion_contract import _core_evidence_policy
    return _core_evidence_policy().assess_criterion_dependencies(record, evidence_reference, criterion_id)


def _retained_met_evidence(store, state, current, root, target, material, *, expected_unit, invalidate,
                           criterion_keys=None):
    """Check actual met-proof backing, never reuse historical input authority."""
    import autopilot_completion_contract as contract
    from autopilot_work_items import _load_core_ledger_module
    if state.get('work_unit_id')!=expected_unit:
        raise CompletionRecordingError('unit-incompatible','Retained proof state belongs to another selected unit')
    binding,reference,snapshots=_v2_contracts(current,root,target,material)
    snapshot=next((c for c in snapshots if c['work_unit_id']==state['work_unit_id']),None)
    receipt=store.completion_read('receipt',state['receipt_token'])
    origin=store.completion_read('origin',receipt['origin_token'])
    contract.validate_origin(origin,receipt['origin_token'])
    # Retained capture input is immutable provenance. Today's owner, meaning,
    # target and Core writer binding are checked independently; input alone
    # neither refreshes this proof nor creates a retry delta.
    historical_binding=dict(binding,input_event_id=origin['input_event_id'],input_digest=origin['input_digest'])
    if (snapshot is None or store.completion_unit(state['work_unit_id'])!=state
            or receipt.get('schema_version')!=contract.RECEIPT_SCHEMA
            or receipt['binding']!=historical_binding or receipt['target']!=reference
            or receipt['contract']!=snapshot or state['contract_revision']!=snapshot['contract_revision']
            or origin['contract']!=snapshot or origin['target']!=reference
            or origin['captured_at']!=receipt['prepared_at']):
        raise CompletionRecordingError('unit-incompatible','Retained proof unit differs from current selected owner/contract/target')
    ledger=_load_core_ledger_module(Path(binding['intent_root'])/binding['platform']/binding['session_id']/'intent-state.json',current)
    invalidator=getattr(ledger,'invalidate_acceptance_criterion',None)
    if not callable(invalidator):
        raise CompletionRecordingError('provider-capability-missing','Core evidence invalidation writer is required')
    backing={}
    now=datetime.now(timezone.utc).isoformat()
    for key,application_ref in state.get('application_references',{}).items():
        application=store.completion_read('application',application_ref)
        publication=store.completion_read('publication',application['publication_reference'])
        request=publication['request']
        receipt=store.completion_read('receipt',publication['receipt_token'])
        # Immutable application binding uses its original dependency snapshot.
        # Only the relation backing today's met criterion is read below.
        records,origins,_=_v2_records(store,request['evidence_references'],read_dependencies=False)
        original_dependencies={d['locator']:d['digest'] for record in records.values() for d in record['dependencies']}
        original_coverage=contract.archive_coverage(records,origins,receipt['contract'],receipt['target'],now,source=current,transaction=store.connection)
        selected={(c['work_unit_id'],c['contract_revision'],c['criterion_id']):c['evidence_reference']
                  for c in request['selected_criteria']}
        if any(original_coverage.get(c)!=ref for c,ref in selected.items()):
            raise CompletionRecordingError('publication-tampered','Historical accepted coverage lost original binding')
        expected=contract.publication_request(binding=receipt['binding'],contract=receipt['contract'],target=receipt['target'],
            coverage=selected,records=records,created_at=receipt['prepared_at'],checked_at=request['publication_checked_at'],correction=request.get('correction'))
        used={c['evidence_reference'] for c in application['selected_criteria']}
        if (request!=expected or request['target']!=reference
                or not contract.publication_member(state,application['publication_reference'],publication)
                or application['binding']!=request['binding'] or application['target']!=request['target']
                or key!=application['application_key']
                or key!=contract.digest([request['idempotency_key'],application['selected_criteria']])
                or application['evidence_references']!=sorted(used)
                or any(c not in request['selected_criteria'] for c in application['selected_criteria'])):
            raise CompletionRecordingError('application-incompatible','Historical application lost exact accepted binding')
        # Mapping to today's meaning/owner is explicit; the historical receipt's
        # input remains provenance and is never sent to the current writer.
        compatible=contract.archive_coverage(records,origins,snapshot,reference,now,source=current,transaction=store.connection)
        for applied in application['selected_criteria']:
            ref=applied['evidence_reference'];record=records[ref]
            current_key=(snapshot['work_unit_id'],snapshot['contract_revision'],applied['criterion_id'])
            if compatible.get(current_key)!=ref:
                continue
            backing.setdefault(current_key,{})[ref]=record
    definitions={contract.criterion_key(c):c for c in snapshot['criteria']}
    outcomes=[]
    for row in sorted(material['intent_state']['acceptance_criteria'],key=contract.criterion_key):
        row_key=contract.criterion_key(row)
        if (row_key not in definitions or row.get('status')!='met'
                or (criterion_keys is not None and row_key not in criterion_keys)):
            continue
        proof=row.get('met_completion_check_digest','').removeprefix('sha256:')
        matches={ref:r for ref,r in backing.get(row_key,{}).items() if r['proof_digest'].removeprefix('sha256:')==proof}
        if len(matches)!=1:
            outcomes.append(_RetainedCriterionOutcome(row_key,proof,None,(),error=CompletionRecordingError(
                'met-evidence-incompatible','Current met proof lacks unambiguous retained Ghost-ALICE Autopilot backing')))
            continue
        ref,record=next(iter(matches.items()))
        assessment=_core_dependency_assessment(record,ref,row['id'])
        outcomes.append(_RetainedCriterionOutcome(row_key,proof,ref,assessment['observations'],
            assessment['changed'],assessment['error']))
    scan=_RetainedEvidenceScan(tuple(outcomes))
    # No writes until common provenance and every independent outcome exist.
    # Unknown sibling bytes neither prove mutation nor erase a known mismatch.
    if invalidate:
        for outcome in scan.outcomes:
            if outcome.changed:
                invalidator(root=Path(binding['intent_root']),platform=binding['platform'],session_id=binding['session_id'],criterion_id=outcome.key[2],
                    expected_input_event_id=binding['input_event_id'],expected_criterion=definitions[outcome.key],
                    expected_completion_check_digest=outcome.proof_digest,invalidation_reason='dependency-mismatch',transaction=store.connection)
    return scan


def _v2_unresolved_invalidations(material,unit):
    """Distinguish reopened prior proof from work not performed yet."""
    latest_evidence_events={}
    for event in material.get('events',[]):
        if event.get('event') in {'acceptance-criterion-met','acceptance-criterion-evidence-invalidated'}:
            latest_evidence_events[(event.get('work_unit_id'),event.get('contract_revision'),event.get('criterion_id'))]=event
    invalidated=[]
    for criterion in material['intent_state']['acceptance_criteria']:
        if criterion['work_unit_id']!=unit or criterion.get('status')=='met':continue
        key=(unit,criterion['contract_revision'],criterion['id'])
        event=latest_evidence_events.get(key,{})
        if event.get('event')=='acceptance-criterion-evidence-invalidated':
            invalidated.append(dict(criterion_id=criterion['id'],prior_completion_check_digest=event['prior_completion_check_digest'],reason=event['invalidation_reason']))
    return invalidated


def _v2_current_unit_outcome(store,current,root,target,material,unit,retained,selected):
    """Keep immutable history readable; only Core support authorizes current coverage."""
    import autopilot_completion_contract as contract
    _,reference,snapshots=_v2_contracts(current,root,target,material)
    snapshot=next(c for c in snapshots if c['work_unit_id']==unit)
    rows=[c for c in material['intent_state']['acceptance_criteria'] if c['work_unit_id']==unit]
    met={contract.criterion_key(c) for c in rows if c.get('status')=='met'}
    supported={};errors=[];assessments={}
    for outcome in retained.outcomes:
        if outcome.key not in met:continue
        evidence=outcome.evidence_reference
        if evidence not in assessments:
            try:
                record=store.completion_read('evidence',evidence)
                dependencies={d['locator']:d['digest'] for d in record['dependencies']}
                dependencies.update({r['locator']:r['observed'] for r in retained.observations
                    if r['evidence_reference']==evidence})
                coverage=contract._core_evidence_policy().assess_reference_coverage({evidence:record},snapshot,
                    reference,dependencies,allow_empty=True,source=current,transaction=store.connection,accepted=True)
                assessments[evidence]=(coverage,None)
            except (ValueError,OSError) as exc:
                assessments[evidence]=({},exc)
        coverage,error=assessments[evidence]
        if outcome.key in coverage and outcome.error is None and not outcome.changed:
            supported[outcome.key]=evidence
        else:
            error=outcome.error or error
            errors.append(dict(work_unit_id=outcome.key[0],contract_revision=outcome.key[1],criterion_id=outcome.key[2],
                evidence_reference=evidence,reason=getattr(error,'reason','retained-proof-unsupported'),
                detail=str(error) if error else 'Core does not support this current criterion from the retained proof'))
    remaining=[dict(work_unit_id=c['work_unit_id'],contract_revision=c['contract_revision'],criterion_id=c['id'])
        for c in rows if contract.criterion_key(c) not in supported]
    selected=[c for c in selected if supported.get((c['work_unit_id'],c['contract_revision'],c['criterion_id']))==c['evidence_reference']]
    result=dict(unit_status='pending' if remaining or retained.errors else 'complete',remaining_criteria=remaining,
        selected_criteria=selected,evidence_references=sorted({c['evidence_reference'] for c in selected}))
    if errors:result.update(reason='retained-proof-unavailable',retained_coverage_errors=errors,detail=errors[0]['detail'])
    if retained.changed:result.update(reason='dependency-mutated')
    if retained.errors:result.update(reason='retained-dependency-unavailable',detail=retained.errors[0]['detail'],
        retained_criterion_errors=retained.errors)
    return result


def _v2_recovery_observation(store,current,reference,material,unit,row,snapshot,*,root,target,claim_scope=None):
    """Observe only facts that can change the recording repair decision."""
    state=store.completion_unit(unit)
    changed=False
    publication=None
    if state and state.get('publication_reference'):
        try: publication=store.completion_read('publication',state['publication_reference'])
        except (ValueError,FileNotFoundError): publication=None
    relevant = dict(contract=snapshot,target=reference,reason=row.get('reason',row['status']),error_type=row.get('error_type'),errno=row.get('errno'))
    if row.get('retained_coverage_errors'):relevant['retained_coverage_errors']=row['retained_coverage_errors']
    relevant['criterion_proof_state']=[dict(id=c['id'],revision=c['contract_revision'],status=c.get('status'),proof=c.get('met_completion_check_digest')) for c in material['intent_state']['acceptance_criteria'] if c['work_unit_id']==unit]
    invalidated=_v2_unresolved_invalidations(material,unit)
    if invalidated:
        relevant['invalidated_criteria']=invalidated
        changed=True
    if publication:
        # Original references + actual relevant artifact content, not new
        # binding, request/receipt token, input ID, or publication time.
        request=publication['request']
        relevant['evidence_references']=request['evidence_references']
        relevant['selected_criteria']=request['selected_criteria']
        relevant['dependencies']=[]
        # Read each dependency independently below. An unavailable sibling is
        # an observation failure, not evidence that any business claim changed.
        try: evidence_records = _v2_records(store,request['evidence_references'],read_dependencies=False)[0]
        except (ValueError,OSError) as exc:
            evidence_records={}
            relevant['evidence_error']=dict(reason=getattr(exc,'reason',type(exc).__name__))
        for evidence in evidence_records.values():
            for dependency in evidence['dependencies']:
                path=dependency['locator']
                try: observed='sha256:'+hashlib.sha256(Path(path).read_bytes()).hexdigest()
                except FileNotFoundError: observed=None
                except OSError as exc: observed=dict(error=type(exc).__name__,errno=exc.errno)
                relevant['dependencies'].append(dict(locator=path,observed=observed))
                if not isinstance(observed,dict):changed = changed or observed != dependency['digest']
    elif state and claim_scope and claim_scope.get('phase') == 'final':
        # The failed prerequisite may belong to Core rather than a Ghost-ALICE Autopilot writer.
        # Observe its authenticated result, without resetting recovery for a new
        # input, unchanged answer, or unrelated provider-file mutation.
        try:
            from autopilot_work_items import _load_core_policy_module
            assessor = _load_core_policy_module('completion_assessment.py', 'ap_core_completion_assessment')
            if assessor is None:
                raise CompletionRecordingError('provider-capability-missing', 'Core completion review is unavailable')
            reviewed = assessor.lookup_current_response(current, claim_scope['text'], transaction=store.connection)
            receipt = store.completion_read('receipt', state['receipt_token'])
            assessment = assessor.lookup_publication_assessment(current, receipt['contract'],
                claim_scope['text'], reviewed.candidate['verified_at'], transaction=store.connection)
            relevant['publication_assessment'] = dict(status='ready', reference=assessment.reference)
        except (ValueError, OSError) as exc:
            relevant['publication_assessment'] = dict(status='unavailable',
                reason=getattr(exc, 'reason', type(exc).__name__))
    if row.get('reason')=='derived-target-unwritable' or row.get('error_type') in {'PermissionError','FileExistsError','NotADirectoryError'}:
        parent=Path(reference['path'])
        while not parent.exists() and parent!=parent.parent:parent=parent.parent
        stat=parent.stat()
        relevant['acquisition_state']=dict(parent=str(parent),mode=stat.st_mode,uid=stat.st_uid,gid=stat.st_gid)
    try:
        retained=(_retained_met_evidence(store,state,current,root,target,material,
            expected_unit=unit,invalidate=False) if state else _RetainedEvidenceScan())
        if retained.observations:relevant['retained_met_dependencies']=retained.observations
        if retained.errors:relevant['retained_criterion_errors']=retained.errors
        changed=changed or retained.changed
    except (ValueError,OSError) as exc:
        relevant['retained_evidence_error']=dict(reason=getattr(exc,'reason',type(exc).__name__))
    relevant['recording_code'] = {name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
        for name in ('autopilot_completion_service.py','autopilot_storage.py','autopilot_completion_contract.py','autopilot_work_items.py')}
    from autopilot_work_items import _load_core_ledger_module
    core=_load_core_ledger_module(material['state_path'],current)
    relevant['provider_code']=dict(path=str(Path(core.__file__).resolve()),digest=hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest())
    return dict(work_unit_id=unit,relevant=relevant),state,changed


def _bounded_v2_recovery(current, root, target, material, results, hook_input=None, *, apply_existing_only=False):
    """One diagnostic per relevant actual state; input timestamps never reset it."""
    pending = [r for r in results if r.get('unit_status') != 'complete' and
        (r.get('status') != 'applied' or not r.get('application_reference') or r.get('reason')
         or _v2_unresolved_invalidations(material,r['work_unit_id']))]
    if not pending:
        return dict(_noop_payload(),completion_recording=dict(status='v2-unit-state',units=results))
    fingerprints=[]; lines=[]; any_new=False; changed=False
    def preflight(connection):
        _,_,_,locked,selected = _v2_material(current,transaction=connection,claim_scope=_v2_claim_scope(hook_input))
        acceptable={'no-current-unmet-criteria'} | ({'response-no-publication'} if apply_existing_only else set())
        if (selected['status'] not in {'selected'} and selected.get('reason') not in acceptable
                or locked['intent_state']!=material['intent_state']):
            raise CompletionRecordingError('input-stale','Current execution changed before recovery diagnostic')
    with storage.completion_authority_transaction(root,current,authority=_v2_authority(current),preflight=preflight) as store:
        _,reference,snapshots = _v2_contracts(current,root,target,material)
        by_unit={row['work_unit_id']:row for row in snapshots}
        for row in pending:
            unit=row['work_unit_id']; state=store.completion_unit(unit)
            head=store.completion_recovery_head(unit) or {}
            marker,state,unit_changed=_v2_recovery_observation(store,current,reference,material,unit,row,by_unit[unit],root=root,target=target,
                claim_scope=_v2_claim_scope(hook_input))
            changed=changed or unit_changed
            fingerprint=_digest(marker);fingerprints.append(fingerprint)
            new_diagnostic=head.get('fingerprint')!=fingerprint
            if new_diagnostic:
                store.completion_put('recovery-diagnostic',marker)
                any_new=True
            partial_reference=store.completion_put('recovery-partial',row)
            store.completion_set_recovery_head(unit,dict(head,fingerprint=fingerprint,partial_reference=partial_reference,
                partial_retry_pending=(row.get('reason')=='dependency-mutated' and unit_changed and head.get('fingerprint')!=fingerprint)))
            lines.extend(['work-unit: '+unit,'completion-recording: '+row.get('reason',row['status'])])
            if row.get('detail'): lines.append('recording-detail: '+row['detail'])
    changed=changed or any(r.get('reason') in {'dependency-mutated','dependency-mismatch'} for r in pending)
    status='diagnostic' if any_new else 'parked'
    lines.append('recovery-status: '+status)
    lines.append('Ghost-ALICE Autopilot retains the recording error and original proof; dependent application waits for a relevant state change.')
    return dict({'continue':True},systemMessage='\n'.join(['[autopilot]',*lines]) if any_new else '',
        completion_recording=dict(status='v2-unit-state',units=results),
        recovery=dict(status=status,fingerprints=fingerprints,claims_changed=changed))


@_with_core_policy_source
def recording_failure_payload(source, hook_input, error):
    """Native fallback shares bounded recording recovery; it grants no tool permission."""
    try:
        if _v2_run_is_paused(source):
            return _noop_payload()
        current,root,target,material,selected = _v2_material(source,claim_scope=_v2_claim_scope(hook_input))
        if not material.get('selected_work_units') or (selected['status']!='selected' and selected.get('reason')!='no-current-unmet-criteria'):
            return dict(_noop_payload(),completion_recording=selected)
        rows=[dict(work_unit_id=unit['work_unit_id'],status='recording-error',
            reason=getattr(error,'reason','recording-error'),detail=str(error),error_type=type(error).__name__,errno=getattr(error,'errno',None))
            for unit in material['selected_work_units']]
        return _bounded_v2_recovery(current,root,target,material,rows,hook_input)
    except Exception:  # Native defensive boundary: no persisted allowance means no block.
        # No current owner/current-authority CAS means no retry allowance may be
        # manufactured. Keep a visible partial diagnostic without Stop blocking.
        return dict({'continue':True},systemMessage='[autopilot-mode adapter error]\nerror: '+str(error)+
            '\nGhost-ALICE Autopilot recording code cannot access recording state and parks dependent advancement. The current-session assistant preserves the original supported answer/source/time; the recording error does not justify another business check.',
            completion_recording=dict(status='recording-error',reason='authority-unavailable',detail=str(error)),recovery=dict(status='parked'))


def _v2_parked_results(current,root,target,material,*,claim_scope=None,publication_units=None):
    """Check known failure before target acquisition or privileged advancement."""
    parked={}
    def preflight(connection):
        _,_,_,live,_=_v2_material(current,transaction=connection,claim_scope=claim_scope)
        if live['intent_state']!=material['intent_state'] or not live.get('selected_work_units'):
            raise CompletionRecordingError('input-stale','Execution changed before retry decision')
    with storage.completion_authority_transaction(root,current,authority=_v2_authority(current),preflight=preflight) as store:
        _,reference,snapshots=_v2_contracts(current,root,target,material)
        for snapshot in snapshots:
            unit=snapshot['work_unit_id']
            state=store.completion_unit(unit)
            if publication_units is not None and unit not in publication_units and not (state and state.get('publication_reference')):
                # A prior request is not an accepted publication and cannot
                # override Core's decision about the current response.
                continue
            head=store.completion_recovery_head(unit)
            if not head or not head.get('partial_reference'):continue
            partial=store.completion_read('recovery-partial',head['partial_reference'])
            if partial.get('work_unit_id')!=unit or partial.get('unit_status')=='complete':
                raise CompletionRecordingError('recovery-incompatible','Recovery partial cannot assert business completion or another unit')
            # A newly recorded Core completion assessment can release retained coverage without
            # rewriting the publication or met ledger. Reuse diagnostic history,
            # but consume the live Core result in the normal outcome path.
            if partial.get('retained_coverage_errors'):continue
            marker,_,_=_v2_recovery_observation(store,current,reference,material,unit,partial,snapshot,root=root,target=target,
                claim_scope=claim_scope)
            if _digest(marker)==head['fingerprint']:
                if head.get('partial_retry_pending'):
                    # A race changed compatible publication coverage during the
                    # rolled-back application. Diagnose the now-stable partial
                    # coverage once; this repeats no business verification.
                    store.completion_set_recovery_head(unit,dict(head,partial_retry_pending=False))
                else:
                    parked[unit]=partial
    return parked


def _publish_reviewed_response(current, root, target, unit, text):
    """Record Core-reviewed native proof without asking the model to publish it."""
    from autopilot_work_items import _load_core_policy_module
    with _v2_recording_transaction(root, current, target) as store:
        if store is None:
            raise CompletionRecordingError('derived-target-unwritable', 'Publication target is unavailable')
        state = store.completion_unit(unit)
        if not state or state.get('publication_reference'):
            return
        assessor = _load_core_policy_module('completion_assessment.py', 'ap_core_completion_assessment')
        if assessor is None:
            raise CompletionRecordingError('provider-capability-missing', 'Core completion review is unavailable')
        reviewed = assessor.lookup_current_response(current, text, transaction=store.connection)
        candidate = reviewed.candidate
        if candidate['contract']['work_unit_id'] != unit:
            return
        token = state['receipt_token']
        verified_at = candidate['verified_at']
        # A current response review may describe an accepted original whose
        # publication has not been applied. Reuse that archive reference so
        # its explicit business dependencies survive input/receipt rebinding;
        # observation sources are not a substitute for those dependencies.
        from autopilot_claim_evidence import ClaimArchive
        archive = ClaimArchive(store.connection, store.root, candidate['contract']['owner'], current)
        accepted = archive.accepted_evidence_records(unit, datetime.now(timezone.utc).isoformat())
        retained = candidate.get('reuse_evidence')
        if retained is not None:
            if retained not in accepted:
                raise CompletionRecordingError('evidence-unbound', 'Reviewed original lacks its accepted archive chain')
            references = [retained]
        else:
            capture = candidate.get('capture_binding')
            references = [reference for reference, record in accepted.items()
                if record['proof'] == candidate['proof'] and record['verified_at'] == verified_at
                and record['contract'] == candidate['contract']
                and (capture is None or record['origin_token'] == capture['origin_token'])]
            if len(references) > 1:
                raise CompletionRecordingError('evidence-unbound', 'Reviewed proof has ambiguous accepted dependency records')
    if references:
        _publish_completion_v2(receipt_token=token, completion_check=None, verified_at=None,
            evidence_source=None, source=current, evidence_references=references,
            dependencies=[], criterion_dependencies=None)
        return
    _publish_completion_v2(receipt_token=token, completion_check=text, verified_at=verified_at,
        evidence_source=None, source=current, evidence_references=None, dependencies=[], criterion_dependencies=None)


@_with_core_policy_source
def apply_completion_v2(source, *, hook_input=None, apply_existing_only=False):
    """Stop's privileged adapter path; publication alone never writes met."""
    if _v2_run_is_paused(source):
        return _noop_payload()
    claim_scope=_v2_claim_scope(hook_input)
    # Native observation can precede human admission. Core classifies that
    # snapshot before any publication/application storage is acquired.
    current,root,target,material,selection=_v2_material(source,claim_scope=claim_scope,require_input=False)
    # Reporting without new completion coverage still permits applying an
    # existing authenticated publication. That obligation is independent of
    # whether the user approved any subsequent task plan.
    if selection.get('reason')=='response-no-publication':
        apply_existing_only=True
    acceptable = {'no-current-unmet-criteria'} | ({'response-no-publication'} if apply_existing_only else set())
    if selection['status']!='selected' and selection.get('reason') not in acceptable:
        return dict(_noop_payload(),completion_recording=selection)
    # Core validation code returns proof coverage; Ghost-ALICE Autopilot adapter code records publications and schedules
    # separately approved work without judging the response's meaning again.
    coverage=selection['completion_route'].get('publication_criteria')
    publication_units=set() if apply_existing_only else (
        None if coverage is None else {row['work_unit_id'] for row in coverage})
    parked=_v2_parked_results(current,root,target,material,claim_scope=claim_scope,
        publication_units=publication_units)
    if len(parked)==len(material.get('selected_work_units',[])):
        return _bounded_v2_recovery(current,root,target,material,list(parked.values()),hook_input,
            apply_existing_only=apply_existing_only)
    if not (root/storage.AUTHORITY_FILE).exists():
        if apply_existing_only:
            return dict(_noop_payload(),completion_recording=dict(status='v2-unit-state',units=[]))
        acquired=_prepare_completion_v2(intent_root=Path(current['GHOST_ALICE_SESSION_INTENT_ROOT']),
            platform=current['GHOST_ALICE_PLATFORM'],session_id=current['GHOST_ALICE_SESSION_ID'],
            input_event_id=material['latest_input_event']['event_id'],source=current,claim_scope=claim_scope)
        if acquired.get('status')=='recording-unavailable':
            results=[dict(acquired,work_unit_id=unit['work_unit_id']) for unit in material.get('selected_work_units',[])]
            return _bounded_v2_recovery(current,root,target,material,results,hook_input)
    results=[]
    # Independent units commit independently. Failure in one cannot adopt or
    # erase the other unit's original accepted evidence/application state.
    for unit in material.get('selected_work_units',[]):
        if unit['work_unit_id'] in parked:
            results.append(parked[unit['work_unit_id']])
            continue
        retained_dependency_changed=False
        recording_scope=None
        recording_phase=False
        try:
            if (not apply_existing_only and claim_scope['phase'] == 'final'
                    and publication_units is not None and unit['work_unit_id'] in publication_units):
                _publish_reviewed_response(current, root, target, unit['work_unit_id'], claim_scope['text'])
            with _v2_recording_transaction(root,current,target) as store:
                if store is None:
                    results.append(dict(work_unit_id=unit['work_unit_id'],status='recording-unavailable',reason='derived-target-unwritable'))
                    continue
                current,root,target,locked,selected=_v2_material(current,transaction=store.connection,claim_scope=claim_scope)
                if selected['status']!='selected' and selected.get('reason') not in acceptable:
                    results.append(dict(work_unit_id=unit['work_unit_id'],status='recording-unavailable',reason=selected['reason']))
                    continue
                state=store.completion_unit(unit['work_unit_id'])
                if state is None or not state.get('publication_reference'):
                    if publication_units is not None and unit['work_unit_id'] not in publication_units: continue
                    results.append(dict(work_unit_id=unit['work_unit_id'],status='prepared' if state else 'unprepared'))
                    continue
                retained=_retained_met_evidence(store,state,current,root,target,locked,
                    expected_unit=unit['work_unit_id'],invalidate=True)
                retained_dependency_changed=retained.changed
                retained.require_backing()
                publication=store.completion_read('publication',state['publication_reference'])
                if apply_existing_only and state.get('status')=='applied':
                    # Reusing a committed application does not make its own
                    # unavailable criterion dependencies readable. An older
                    # independent criterion still reports its separate error
                    # through the whole-unit outcome without masking this one.
                    publication_keys={(row['work_unit_id'],row['contract_revision'],row['criterion_id'])
                        for row in publication['request']['selected_criteria']}
                    for outcome in retained.outcomes:
                        if outcome.key in publication_keys and isinstance(outcome.error,OSError):
                            raise outcome.error
                    # Historical application is already a committed result, not
                    # a pending request to bind to today's explanatory input.
                    # Retained proof above still checks owner, meaning, source
                    # and dependencies and performs guarded invalidation.
                    # Application may have committed only the compatible subset
                    # of an originally wider publication. Use its actual record,
                    # whose exact immutable binding was validated above.
                    retained_application=None
                    for key,ref in state.get('application_references',{}).items():
                        application=store.completion_read('application',ref)
                        if key in state.get('applied_requests',[]) and application.get('publication_reference')==state['publication_reference']:
                            retained_application=(ref,application)
                    if retained_application:
                        application_ref,application=retained_application
                        _,_,_,refreshed,_=_v2_material(current,transaction=store.connection,claim_scope=claim_scope)
                        results.append(dict(work_unit_id=unit['work_unit_id'],status='applied',application_reused=True,
                            contract_revision=state['contract_revision'],application_reference=application_ref,
                            original_evidence=publication['result']['original_evidence'],
                            **_v2_current_unit_outcome(store,current,root,target,refreshed,unit['work_unit_id'],
                                retained,application['selected_criteria'])))
                        continue
                request,records,state=_validate_v2_publication(store,publication,current,root,target,locked)
                _apply_correction(store,publication,state,current,locked)
                _,_,_,locked,_=_v2_material(current,transaction=store.connection,claim_scope=claim_scope)
                import autopilot_completion_contract as contract
                application_key=contract.digest([request['idempotency_key'],request['selected_criteria']])
                live_status={(c['work_unit_id'],c['contract_revision'],c['id']):c.get('status')
                    for c in locked['intent_state']['acceptance_criteria']}
                requires_application=any(live_status.get((c['work_unit_id'],c['contract_revision'],c['criterion_id']))!='met'
                    for c in request['selected_criteria'])
                if (request['selected_criteria'] or request.get('correction')) and (application_key not in state['applied_requests'] or requires_application):
                    from autopilot_work_items import materialize_met_criteria_v2
                    # Narrow only an I/O failure in this already authenticated
                    # recording phase. Earlier provenance failures stay global.
                    recording_scope={(c['work_unit_id'],c['contract_revision'],c['criterion_id'])
                        for c in request['selected_criteria']}
                    recording_scope.update((c['work_unit_id'],c['contract_revision'],c['criterion_id'])
                        for c in request.get('correction',{}).get('affected_criteria',[]))
                    recording_scope.update(o.key for o in retained.outcomes if o.changed or o.error is not None)
                    recording_phase=True
                    materialize_met_criteria_v2(request,records,current,store.connection)
                    application=dict(schema_version='autopilot-completion-application.v2',
                        publication_reference=state['publication_reference'],binding=request['binding'],
                        target=request['target'],selected_criteria=request['selected_criteria'],
                        evidence_references=request['evidence_references'],application_key=application_key,
                        applied_at=datetime.now(timezone.utc).isoformat())
                    application_reference=store.completion_put('application',application)
                    state=dict(state,status='applied',applied_requests=list(dict.fromkeys([*state['applied_requests'],application_key])),
                        application_references={**state.get('application_references',{}),application_key:application_reference})
                    store.completion_set_unit(state)
                    recording_phase=False
                post_request,_,_=_validate_v2_publication(store,publication,current,root,target,locked)
                if post_request['selected_criteria']!=request['selected_criteria']:
                    raise CompletionRecordingError('dependency-mutated','Dependencies changed during privileged application')
                _, _, _, refreshed, _ = _v2_material(current, transaction=store.connection,claim_scope=claim_scope)
                post_retained=_retained_met_evidence(store,state,current,root,target,refreshed,expected_unit=unit['work_unit_id'],invalidate=False)
                post_retained.require_unchanged()
                post_retained.require_backing()
                results.append(dict(work_unit_id=unit['work_unit_id'],status=state['status'],
                    contract_revision=state['contract_revision'],
                    original_evidence=publication['result']['original_evidence'],
                    application_reference=state.get('application_references',{}).get(application_key),
                    **_v2_current_unit_outcome(store,current,root,target,refreshed,unit['work_unit_id'],
                        post_retained,request['selected_criteria'])))
        except (ValueError,OSError) as exc:
            if getattr(exc,'reason',None)=='dependency-mutated' or retained_dependency_changed:
                # The application transaction rolled back. Persist only scoped
                # invalidation of proof that was already met before that attempt.
                try:
                    with storage.transaction(root,current,authority=_v2_authority(current),migrate_legacy=False) as recovery:
                        _,_,_,live,selection=_v2_material(current,transaction=recovery.connection,claim_scope=claim_scope)
                        if selection['status']!='selected' and selection.get('reason') not in acceptable:
                            raise CompletionRecordingError(selection['reason'],'Retained proof recovery route is unavailable')
                        queued=recovery.completion_unit(unit['work_unit_id'])
                        # Retained invalidation has its own current receipt,
                        # accepted application and exact met-proof CAS guards.
                        # An unrelated current publication's processing error
                        # must not prevent this independently supported repair.
                        _retained_met_evidence(recovery,queued,current,root,target,live,expected_unit=unit['work_unit_id'],invalidate=True)
                except (ValueError,OSError) as recovery_error:
                    if getattr(exc,'reason',None)=='dependency-mutated':exc=recovery_error
            scoped=(dict(affected_criteria=[dict(work_unit_id=k[0],contract_revision=k[1],criterion_id=k[2])
                for k in sorted(recording_scope)]) if recording_phase and isinstance(exc,OSError) else {})
            results.append(dict(work_unit_id=unit['work_unit_id'],status='recording-unavailable',
                reason=getattr(exc,'reason','publication-incompatible'),detail=str(exc),**scoped))
    current,root,target,material,_=_v2_material(current,claim_scope=claim_scope)
    return _bounded_v2_recovery(current,root,target,material,results,hook_input,
        apply_existing_only=apply_existing_only)

"""Ghost-ALICE Autopilot archive primitives for Core's original-claim assessment.

This provider authenticates source coordinates and the original immutable acceptance chain.
Core owns proof/coverage policy; this provider reads capture and acceptance records,
never executes business work or reads business dependencies.
The existing SQLite database is opened read-only under a query-only authorizer.
SQLite SHM reader marks and empty WAL sidecars are coordination metadata; no
authority rows, committed WAL, evidence, or business files are written here.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import sys
import json
from pathlib import Path
import sqlite3
from typing import Mapping


def _identity(source):
    if not isinstance(source, Mapping):
        raise OSError('authority-unavailable')
    platform, session, locator = (source.get(k) for k in
        ('GHOST_ALICE_PLATFORM', 'GHOST_ALICE_SESSION_ID', 'GHOST_ALICE_SESSION_INTENT_ROOT'))
    if (platform not in ('codex', 'claude') or not isinstance(session, str) or not session.strip()
            or session != session.strip() or Path(session).name != session
            or session in ('.', '..') or '\\' in session or not isinstance(locator, str)
            or not locator or not Path(locator).is_absolute()
            or (platform == 'codex' and source.get('CODEX_THREAD_ID')
                and source['CODEX_THREAD_ID'] != session)):
        raise OSError('authority-unavailable')
    return Path(locator).resolve(), dict(platform=platform, session_id=session)


def _read_authorizer(action, arg1, arg2, _database, _trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'BEGIN':
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in ('database_list', 'user_version') and arg2 is None:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class ClaimArchive:
    """Raw exact-source archive rows and immutable JSON, without policy results."""
    def __init__(self, connection, root, owner, source):
        self.connection, self.root, self.owner = connection, root, owner
        self.source = dict(source)

    def unit_records(self, work_unit_id):
        # Unit names are session-local. Candidate selection by the receipt's
        # binding prevents legitimate foreign units from poisoning this owner.
        # Core must still authenticate every selected receipt and application.
        return self.connection.execute(
            "SELECT u.run_key,u.body_json FROM ap_completion_v2_units AS u WHERE u.work_unit_id=? "
            "AND EXISTS (SELECT 1 FROM ap_completion_v2_records AS r WHERE r.run_key=u.run_key "
            "AND r.kind='receipt' AND json_extract(r.body_json,'$.binding.intent_root')=? "
            "AND json_extract(r.body_json,'$.binding.platform')=? "
            "AND json_extract(r.body_json,'$.binding.session_id')=?)",
            (work_unit_id, str(self.root), self.owner['platform'], self.owner['session_id']))

    def capture_binding(self, current_contract):
        """Bind original preparation separately from current publication authority."""
        global contract
        import autopilot_completion_contract as contract
        _require(current_contract.get('owner') == self.owner, 'capture-owner-mismatch')
        tables = {r[0] for r in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('ap_completion_v2_units','ap_completion_v2_records')")}
        if len(tables) != 2:
            return None
        matches = []
        for run_key, raw in self.unit_records(current_contract['work_unit_id']):
            unit = json.loads(raw)
            _require(isinstance(unit, dict) and unit.get('work_unit_id') == current_contract['work_unit_id'],
                'unit-incompatible')
            token = unit.get('receipt_token')
            _require(isinstance(token, str), 'receipt-invalid')
            store = _RunRecords(self, run_key)
            now = datetime.now(timezone.utc).isoformat()
            receipt = _read_receipt(store, token, self.root, self.owner, now)
            if receipt['contract'] != current_contract:
                continue
            authority_input = receipt['binding']['input_event_id']
            selected = {contract.criterion_key(dict(c, id=c['criterion_id']))
                for c in receipt['selected_criteria']}
            # Immutable insertion order chooses an existing capture, not a
            # model-supplied timestamp. Current receipt validation stays separate.
            for (original_token,) in self.connection.execute(
                    "SELECT identity FROM ap_completion_v2_records WHERE run_key=? AND kind='receipt' "
                    "AND json_extract(body_json,'$.contract.work_unit_id')=? ORDER BY rowid",
                    (run_key, current_contract['work_unit_id'])):
                original = _read_receipt(store, original_token, self.root, self.owner, now)
                covered = {contract.criterion_key(dict(c, id=c['criterion_id']))
                    for c in original['selected_criteria']}
                if (original['contract'] == current_contract and original['target'] == receipt['target']
                        and selected <= covered and not original.get('correction_of')):
                    token, receipt = original_token, original
                    break
            matches.append(dict(receipt_token=token, origin_token=receipt['origin_token'],
                captured_at=receipt['prepared_at'], input_event_id=receipt['binding']['input_event_id'],
                input_digest=receipt['binding']['input_digest'], target=receipt['target'],
                authority_input_event_id=authority_input))
        _require(len(matches) <= 1, 'capture-ambiguous')
        return matches[0] if matches else None

    def accepted_records(self, work_unit_id, now):
        """Read original accepted chains; no current-work assessment or file reads."""
        accepted = []
        with _archive_runtime(self.source):
            for run_key, raw in self.unit_records(work_unit_id):
                unit = json.loads(raw)
                _require(isinstance(unit, dict) and unit.get('work_unit_id') == work_unit_id
                    and isinstance(unit.get('application_references'), dict)
                    and isinstance(unit.get('publications'), dict)
                    and isinstance(unit.get('applied_requests'), list), 'unit-incompatible')
                store = _RunRecords(self, run_key)
                for key, ref in unit['application_references'].items():
                    app, records, _origins, _snapshots, original_contract = _application_records(
                        store, unit, key, ref, self.root, self.owner, now)
                    accepted.append((ref, app, records, original_contract))
        return accepted

    def accepted_evidence_records(self, work_unit_id, now):
        """Read accepted originals, including publications pending application."""
        found={}
        with _archive_runtime(self.source):
            for run_key,raw in self.unit_records(work_unit_id):
                unit=json.loads(raw);store=_RunRecords(self,run_key)
                references=set(unit.get('publications',{}).values())|set(unit.get('correction_publications',[]))
                for reference in references:
                    _,records,_,_,_=_publication_records(store,unit,reference,self.root,self.owner,now)
                    found.update(records)
        return found

    def accepted_evidence(self, work_unit_id, evidence_reference, now):
        """Read exact accepted proof, including a publication pending application."""
        found=self.accepted_evidence_records(work_unit_id,now)
        if evidence_reference not in found:raise FileNotFoundError(evidence_reference)
        return found[evidence_reference]

    def read_record(self, run_key, kind, identity):
        row = self.connection.execute(
            'SELECT body_json FROM ap_completion_v2_records WHERE run_key=? AND kind=? AND identity=?',
            (run_key, kind, identity)).fetchone()
        if row is None:
            raise FileNotFoundError(identity)
        value = json.loads(row[0])
        digest = 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True,
            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        if digest != identity:
            raise ValueError('Immutable completion record was altered')
        return value


@contextmanager
def _archive_runtime(source):
    """Load this provider's modules for both direct Core readers and Ghost-ALICE Autopilot callers."""
    adapters = str(Path(__file__).resolve().parent)
    sys.path.insert(0, adapters)
    try:
        global contract
        import autopilot_completion_contract as contract
        from autopilot_work_items import _bind_core_policy_source
        with _bind_core_policy_source(source):
            yield
    finally:
        sys.path.remove(adapters)


def capture_binding(source, contract, *, transaction=None):
    """Return authenticated capture data; a shared transaction proves read order.

    The binding establishes capture before review, not native attestation of a
    historical business operation. Missing capture retains legacy time checks.
    """
    root, owner = _identity(source)
    adapters = str(Path(__file__).resolve().parent)
    sys.path.insert(0, adapters)
    try:
        if transaction is not None:
            databases = transaction.execute('PRAGMA database_list').fetchall()
            _require(any(row[1] == 'main' and Path(row[2]).resolve() == root / 'ghost-state.sqlite3'
                for row in databases), 'capture-authority-mismatch')
            return ClaimArchive(transaction, root, owner, source).capture_binding(contract)
        with open_claim_archive(source) as archive:
            return archive.capture_binding(contract)
    finally:
        sys.path.remove(adapters)


@contextmanager
def open_claim_archive(source):
    """Yield an existing exact-source snapshot; never create schema or a run."""
    root, owner = _identity(source)
    database = root / 'ghost-state.sqlite3'
    if not database.is_file():
        raise OSError('authority-unavailable')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=10,
                                 isolation_level=None)) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.set_authorizer(_read_authorizer)
        connection.execute('BEGIN')
        yield ClaimArchive(connection, root, owner, source)


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


class _RunRecords:
    def __init__(self, archive, key):
        self.archive, self.key = archive, key

    def completion_read(self, kind, identity):
        return self.archive.read_record(self.key, kind, identity)


def _read_receipt(store, token, root, owner, now):
    receipt = store.completion_read('receipt', token)
    _require(receipt.get('schema_version') == contract.RECEIPT_SCHEMA, 'receipt-invalid')
    origin = store.completion_read('origin', receipt['origin_token'])
    contract.validate_origin(origin, receipt['origin_token'])
    target, binding = receipt['target'], receipt['binding']
    contract.validate_target(target)
    _require(target['authority'] == str(root) and target['owner'] == owner
        and Path(target['path']).resolve() == Path(store.key)
        and receipt['contract'] == origin['contract'] and target == origin['target']
        and receipt['contract']['owner'] == owner
        and binding == dict(owner, intent_root=str(root), input_event_id=origin['input_event_id'],
                            input_digest=origin['input_digest'])
        and receipt['prepared_at'] == origin['captured_at']
        and contract.timestamp(receipt['prepared_at']) <= contract.timestamp(now), 'receipt-owner-mismatch')
    contract.receipt_selection_keys(receipt)
    return receipt


def _publication_records(store, unit, reference, root, owner, now):
    publication = store.completion_read('publication', reference)
    receipt = _read_receipt(store, publication['receipt_token'], root, owner, now)
    request = publication['request']
    refs = request['evidence_references']
    _require(isinstance(refs, list) and refs and len(refs) == len(set(refs)), 'evidence-ambiguous')
    records = {ref: store.completion_read('evidence', ref) for ref in refs}
    origins = {r['origin_token']:store.completion_read('origin', r['origin_token']) for r in records.values()}
    snapshots = {d['locator']:d['digest'] for r in records.values() for d in r['dependencies']}
    coverage = contract.archive_coverage(records, origins, receipt['contract'], receipt['target'], now,
        source=store.archive.source, transaction=store.archive.connection)
    selected = {(c['work_unit_id'],c['contract_revision'],c['criterion_id']):c['evidence_reference']
                for c in request['selected_criteria']}
    requested = {(c['work_unit_id'],c['contract_revision'],c['criterion_id']) for c in receipt['selected_criteria']}
    _require(len(selected) == len(request['selected_criteria']) and
        all(k in requested and coverage.get(k) == ref for k,ref in selected.items()), 'publication-coverage-invalid')
    expected = contract.publication_request(binding=receipt['binding'], contract=receipt['contract'],
        target=receipt['target'], coverage=selected, records=records,
        created_at=receipt['prepared_at'], checked_at=request['publication_checked_at'], correction=request.get('correction'))
    _require(request == expected and contract.publication_member(unit, reference, publication)
        and publication['result']['idempotency_key'] == request['idempotency_key']
        and contract.timestamp(request['publication_checked_at']) <= contract.timestamp(now), 'publication-incompatible')
    return publication, records, origins, snapshots, request


def _application_records(store, unit, key, reference, root, owner, now):
    """Reconstruct accepted publication, including its immutable application."""
    app = store.completion_read('application', reference)
    _require(app.get('schema_version') == 'autopilot-completion-application.v2'
        and key in unit.get('applied_requests', []) and key == app['application_key'], 'application-unaccepted')
    publication,records,origins,snapshots,request=_publication_records(
        store,unit,app['publication_reference'],root,owner,now)
    applied = app['selected_criteria']
    used = sorted({c['evidence_reference'] for c in applied})
    _require(app['binding'] == request['binding'] and app['target'] == request['target']
        and key == contract.digest([request['idempotency_key'], applied])
        and (applied or request.get('correction')) and len(applied) == len({(c['work_unit_id'],c['contract_revision'],c['criterion_id']) for c in applied})
        and app['evidence_references'] == used and all(c in request['selected_criteria'] for c in applied)
        and contract.timestamp(request['publication_checked_at']) <= contract.timestamp(app['applied_at']) <= contract.timestamp(now),
        'application-incompatible')
    return app, records, origins, snapshots, request['contract']

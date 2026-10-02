"""Transactional Autopilot authority; JSON files are migration sources or inboxes.

Bound runs share the Core ledger database and transaction. Standalone runs use
an explicit authority.json and a separate database, never a discovered session.
Dependencies: Python standard library; Core storage API for bound runs.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from typing import Any, Mapping
import uuid

_ACTIVE = ContextVar("autopilot_storage", default=None)
AUTHORITY_FILE = "authority.json"
DATABASE_FILE = "ghost-state.sqlite3"
INBOXES = {"consistency-decision.json", "conduct-plan.json"}
MIGRATED_FILES = {"approved-run.json", "tasks.jsonl", "events.jsonl", "conduct-plan.json",
                  "conduct-plan.candidate.json", "consistency-decision.applied.json",
                  "conduct-plan.applied.json"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _raw(path):
    try:
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path.name}: {exc}") from exc


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".authority-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def bound_authority(run):
    approval = run.get("approval_evidence")
    binding = approval.get("session_intent") if isinstance(approval, Mapping) else None
    if not isinstance(binding, Mapping):
        return None
    platform, session_id = binding.get("platform"), binding.get("session_id")
    locator = binding.get("state_path")
    if platform not in {"codex", "claude", "agent-runtime"} or not isinstance(session_id, str) or not session_id:
        raise ValueError("invalid approved session authority")
    if not isinstance(locator, str) or not Path(locator).is_absolute():
        raise ValueError("session authority requires an absolute canonical state locator")
    path = Path(locator).resolve()
    if path.name != "intent-state.json" or path.parent.name != session_id or path.parent.parent.name != platform:
        raise ValueError("session authority locator identity mismatch")
    return {"schema_version": "autopilot-authority.v1", "kind": "session",
            "ledger_root": str(path.parent.parent.parent), "platform": platform, "session_id": session_id}


def _descriptor(run_dir, proposed=None):
    path = run_dir / AUTHORITY_FILE
    if path.exists():
        authority = _raw(path)
        if not isinstance(authority, dict) or authority.get("schema_version") != "autopilot-authority.v1":
            raise ValueError("invalid Autopilot authority descriptor")
        if proposed and any(authority.get(k) != v for k, v in proposed.items()):
            raise ValueError("Autopilot authority differs from requested session")
    else:
        authority = proposed
        if authority is None and (run_dir / "approved-run.json").is_file():
            run = _raw(run_dir / "approved-run.json")
            authority = bound_authority(run)
            if authority is None:
                from autopilot_state import _approved_run_allows_continue
                run_id = run.get("run_id")
                if isinstance(run_id, str) and run_id.strip() and _approved_run_allows_continue(run):
                    identity = hashlib.sha256(_json([str(run_dir), run_id]).encode()).hexdigest()
                    authority = {"schema_version": "autopilot-authority.v1", "kind": "standalone",
                                 "authority_id": "legacy-run:" + identity}
        if authority is None:
            raise ValueError("standalone run requires explicit authority.json with authority_id")
    if authority.get("kind") == "standalone":
        if not isinstance(authority.get("authority_id"), str) or not authority["authority_id"].strip():
            raise ValueError("standalone authority_id is required")
        root = run_dir
    elif authority.get("kind") == "session":
        root = Path(authority.get("ledger_root", ""))
        if not root.is_absolute() or authority.get("platform") not in {"codex", "claude", "agent-runtime"}:
            raise ValueError("invalid bound authority coordinates")
        sid = authority.get("session_id")
        if not isinstance(sid, str) or not sid or Path(sid).name != sid or sid in {".", ".."} or "\\" in sid:
            raise ValueError("invalid bound authority session")
    else:
        raise ValueError("unknown Autopilot authority kind")
    if authority.get("database_initialized") and not (root / DATABASE_FILE).is_file():
        raise ValueError("Autopilot authority database is missing; legacy fallback is forbidden")
    return dict(authority), root.resolve()


def _schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS ap_runs (run_key TEXT PRIMARY KEY, authority_json TEXT NOT NULL, generation TEXT NOT NULL, run_json TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS ap_generations (run_key TEXT NOT NULL, generation TEXT NOT NULL, run_json TEXT NOT NULL, PRIMARY KEY(run_key,generation))")
    conn.execute("CREATE TABLE IF NOT EXISTS ap_objects (run_key TEXT NOT NULL, generation TEXT NOT NULL, name TEXT NOT NULL, body_json TEXT NOT NULL, PRIMARY KEY(run_key,generation,name))")
    conn.execute("CREATE TABLE IF NOT EXISTS ap_receipts (run_key TEXT NOT NULL, generation TEXT NOT NULL, kind TEXT NOT NULL, receipt_id TEXT NOT NULL, digest TEXT NOT NULL, body_json TEXT NOT NULL, PRIMARY KEY(run_key,generation,kind,receipt_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS ap_archives (run_key TEXT NOT NULL, generation TEXT NOT NULL, target TEXT NOT NULL, body BLOB NOT NULL, PRIMARY KEY(run_key,generation,target))")
    conn.execute("CREATE TABLE IF NOT EXISTS ap_migrations (run_key TEXT PRIMARY KEY, sources_json TEXT NOT NULL)")


def _enable_wal(connection):
    # Two first writers can contend while changing journal mode before BEGIN;
    # SQLite may return BUSY here without using its normal busy timeout. Only
    # this idempotent initialization is retried, never a run/proof mutation.
    deadline = time.monotonic() + 10.0
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", -1) & 255
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                raise
            if connection.in_transaction:
                connection.rollback()
            time.sleep(0.01)


@contextmanager
def _standalone_transaction(root):
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / DATABASE_FILE, timeout=10, isolation_level=None)
    try:
        _enable_wal(conn)
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        conn.close()


class RunStore:
    def __init__(self, conn, run_dir, authority, root):
        self.connection, self.run_dir = conn, run_dir
        self.authority, self.root = authority, root
        self.key = str(run_dir)
        self.inbox_bytes = {}

    def run(self):
        row = self.connection.execute("SELECT authority_json,generation,run_json FROM ap_runs WHERE run_key=?", (self.key,)).fetchone()
        if row is None:
            return None
        expected = {k: v for k, v in self.authority.items() if k not in {"database_initialized", "migration_complete"}}
        if json.loads(row[0]) != expected:
            raise ValueError("database run authority does not match descriptor")
        value = json.loads(row[2])
        if value.get("approval_generation"):
            from autopilot_state import SESSION_MATERIAL
            if value["approval_generation"] != row[1] or SESSION_MATERIAL.approval_generation(value) != row[1]:
                raise ValueError("database approval generation is inconsistent")
        return value

    def generation(self):
        row = self.connection.execute("SELECT generation FROM ap_runs WHERE run_key=?", (self.key,)).fetchone()
        if row is None:
            raise ValueError("run has not been admitted into authority")
        return row[0]

    def put_run(self, value):
        generation = value.get("approval_generation") or "standalone:" + self.authority.get("authority_id", "")
        if self.authority["kind"] == "session":
            from autopilot_state import SESSION_MATERIAL
            if not value.get("approval_generation") or SESSION_MATERIAL.approval_generation(value) != generation:
                raise ValueError("bound run requires the current approval generation")
        expected = bound_authority(value)
        if self.authority["kind"] == "session" and (expected is None or any(self.authority.get(k) != v for k, v in expected.items())):
            raise ValueError("run approval differs from bound database authority")
        authority = {k: v for k, v in self.authority.items() if k not in {"database_initialized", "migration_complete"}}
        previous = self.run()
        if previous is not None and self.generation() != generation:
            old_generation = self.generation()
            for name in INBOXES:
                path = self.run_dir / name
                if path.is_file():
                    raw = path.read_bytes()
                    body = json.loads(raw)
                    self.write("superseded:" + name, body)
                    digest = hashlib.sha256(_json(body).encode()).hexdigest()
                    self.connection.execute("INSERT OR IGNORE INTO ap_receipts VALUES(?,?,?,?,?,?)",
                        (self.key, old_generation, name + "#superseded", digest, digest, _json(body)))
                    target = ".approval-history/" + old_generation.replace(":", "-") + "/" + name
                    self.queue_archive(target, raw)
        self.connection.execute("INSERT INTO ap_generations VALUES(?,?,?) ON CONFLICT(run_key,generation) DO NOTHING", (self.key, generation, _json(value)))
        self.connection.execute("INSERT INTO ap_runs VALUES(?,?,?,?) ON CONFLICT(run_key) DO UPDATE SET generation=excluded.generation,run_json=excluded.run_json", (self.key, _json(authority), generation, _json(value)))

    def read(self, name):
        if name == "approved-run.json":
            value = self.run()
        else:
            if self.run() is None:
                raise FileNotFoundError(name)
            row = self.connection.execute("SELECT body_json FROM ap_objects WHERE run_key=? AND generation=? AND name=?", (self.key, self.generation(), name)).fetchone()
            value = json.loads(row[0]) if row else None
        if value is None:
            raise FileNotFoundError(name)
        return value

    def write(self, name, value):
        if name == "approved-run.json":
            self.put_run(value)
        else:
            self.connection.execute("INSERT INTO ap_objects VALUES(?,?,?,?) ON CONFLICT(run_key,generation,name) DO UPDATE SET body_json=excluded.body_json", (self.key, self.generation(), name, _json(value)))

    def receipt(self, name, value):
        digest = hashlib.sha256(_json(value).encode()).hexdigest()
        identity = str(value.get("decision_id") or value.get("source_candidate_id") or digest)
        if name.endswith("#rejected"):
            identity += ":" + digest
        row = self.connection.execute("SELECT digest FROM ap_receipts WHERE run_key=? AND generation=? AND kind=? AND receipt_id=?", (self.key, self.generation(), name, identity)).fetchone()
        if row and row[0] != digest:
            raise ValueError("receipt id was reused with different content")
        return identity, digest, row is not None

    def consume(self, name, value, applied_name):
        receipt_kind = name + "#rejected" if ".rejected" in applied_name else name
        identity, digest, used = self.receipt(receipt_kind, value)
        if not used:
            self.connection.execute("INSERT INTO ap_receipts VALUES(?,?,?,?,?,?)", (self.key, self.generation(), receipt_kind, identity, digest, _json(value)))
        self.write(applied_name, value)
        self.connection.execute("DELETE FROM ap_objects WHERE run_key=? AND generation=? AND name=?", (self.key, self.generation(), name))
        self.queue_archive(applied_name, self.inbox_bytes.get(name, (_json(value) + "\n").encode()))

    def queue_archive(self, target, raw):
        self.connection.execute("INSERT INTO ap_archives VALUES(?,?,?,?) ON CONFLICT(run_key,generation,target) DO UPDATE SET body=excluded.body",
            (self.key, self.generation(), target, raw))

    def already_handled(self, name, value=None, *, raw=None):
        if value is not None:
            digest = hashlib.sha256(_json(value).encode()).hexdigest()
            if self.receipt(name, value)[2] or self.receipt(name + "#rejected", value)[2]:
                return True
            return self.connection.execute("SELECT 1 FROM ap_receipts WHERE run_key=? AND kind=? AND digest=?",
                (self.key, name + "#superseded", digest)).fetchone() is not None
        digest = "raw:" + hashlib.sha256(raw).hexdigest()
        return any(json.loads(row[0]).get("raw_digest") == digest for row in self.connection.execute(
            "SELECT body_json FROM ap_receipts WHERE run_key=? AND generation=? AND kind=?",
            (self.key, self.generation(), name + "#rejected")))

    def migrate(self):
        if self.run() is not None:
            return
        if self.authority.get("migration_complete"):
            raise ValueError("migrated run is missing from authority; legacy fallback is forbidden")
        approved = self.run_dir / "approved-run.json"
        if not approved.is_file():
            return
        sources = {name: hashlib.sha256((self.run_dir / name).read_bytes()).hexdigest() for name in MIGRATED_FILES if (self.run_dir / name).is_file()}
        value = _raw(approved)
        # Validate legacy contracts before importing them as current authority.
        from autopilot_state import SESSION_MATERIAL
        from autopilot_work_items import validate_work_items
        value["approval_generation"] = SESSION_MATERIAL.approval_generation(value)
        self.put_run(value)
        for name in sources:
            if name == "approved-run.json":
                continue
            body = _raw(self.run_dir / name)
            if name == "tasks.jsonl":
                body = validate_work_items(body)
            self.write(name, body)
        after = {name: hashlib.sha256((self.run_dir / name).read_bytes()).hexdigest() for name in sources}
        if after != sources:
            raise ValueError("legacy run changed during migration; stop legacy writers and retry")
        self.connection.execute("INSERT INTO ap_migrations VALUES(?,?)", (self.key, _json(sources)))

    def archive_inboxes(self):
        # The producer owns the live inbox. Archive only the captured committed
        # snapshot; never rename/delete a path another producer may replace.
        conn = sqlite3.connect((self.root / DATABASE_FILE).as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT target,body FROM ap_archives WHERE run_key=?", (self.key,)).fetchall()
        finally:
            conn.close()
        for target, body in rows:
            path = self.run_dir / target
            try:
                if path.is_file() and path.read_bytes() == body:
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(body)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            except OSError:
                pass


def active(run_dir=None):
    store = _ACTIVE.get()
    return store if store is not None and (run_dir is None or Path(run_dir).resolve() == store.run_dir) else None


def core_transaction(root):
    store = active()
    return store.connection if store and store.authority["kind"] == "session" and Path(root).resolve() == store.root else None


@contextmanager
def transaction(run_dir, source=None, *, authority=None):
    run_dir = Path(run_dir).expanduser().resolve()
    existing = active(run_dir)
    if existing:
        yield existing
        return
    descriptor, root = _descriptor(run_dir, authority)
    if descriptor["kind"] == "session":
        from autopilot_work_items import _load_core_ledger_module
        core = _load_core_ledger_module(root / descriptor["platform"] / descriptor["session_id"] / "intent-state.json", source)
        if core is None or not hasattr(core, "storage_transaction"):
            from autopilot_work_items import AutopilotStateError
            raise AutopilotStateError("core met writer is unavailable: compatible Core SQLite storage API is required")
        manager = core.storage_transaction(root)
    else:
        manager = _standalone_transaction(root)
    with manager as conn:
        _schema(conn)
        # Write only routing metadata. DB existence is now mandatory even when
        # the first import rolls back, so deletion never selects stale JSON.
        descriptor["database_initialized"] = True
        if not (run_dir / AUTHORITY_FILE).exists() or _raw(run_dir / AUTHORITY_FILE) != descriptor:
            _atomic(run_dir / AUTHORITY_FILE, descriptor)
        store = RunStore(conn, run_dir, descriptor, root)
        token = _ACTIVE.set(store)
        try:
            store.migrate()
            yield store
            has_run = store.run() is not None
        finally:
            _ACTIVE.reset(token)
    if has_run and not descriptor.get("migration_complete"):
        descriptor["migration_complete"] = True
        _atomic(run_dir / AUTHORITY_FILE, descriptor)
    store.archive_inboxes()


def read(path):
    path = Path(path)
    if path.name not in MIGRATED_FILES and not path.name.startswith("consistency-decision.rejected"):
        return _raw(path)
    store = active(path.parent)
    if store:
        return store.read(path.name)
    if not (path.parent / AUTHORITY_FILE).is_file():
        return _raw(path)
    descriptor, root = _descriptor(path.parent.resolve())
    database = root / DATABASE_FILE
    if not database.is_file():
        return _raw(path)  # Explicit standalone authority before its first import.
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("BEGIN")
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ap_runs'").fetchone()
        store = RunStore(conn, path.parent.resolve(), descriptor, root)
        if not table or store.run() is None:
            if descriptor.get("migration_complete"):
                raise ValueError("migrated run is missing from authority; legacy fallback is forbidden")
            return _raw(path)
        return store.read(path.name)
    finally:
        conn.close()


def exists(path):
    try:
        read(path)
        return True
    except FileNotFoundError:
        return False


def write(path, value):
    path = Path(path)
    store = active(path.parent)
    if store:
        store.write(path.name, value)
        return True
    if (path.parent / AUTHORITY_FILE).is_file():
        descriptor, root = _descriptor(path.parent.resolve())
        if descriptor.get("database_initialized"):
            with transaction(path.parent) as store:
                store.write(path.name, value)
            return True
    return False


@contextmanager
def _reading_store(run_dir):
    run_dir = Path(run_dir).resolve()
    if store := active(run_dir):
        yield store
        return
    if not (run_dir / AUTHORITY_FILE).exists():
        yield None
        return
    authority, root = _descriptor(run_dir)
    database = root / DATABASE_FILE
    if not database.exists():
        yield None
        return
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("BEGIN")
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ap_runs'").fetchone()
        store = RunStore(conn, run_dir, authority, root)
        yield store if table and store.run() is not None else None
    finally:
        conn.close()


def inbox(path):
    path = Path(path)
    with _reading_store(path.parent) as store:
        if path.is_file():
            raw = path.read_bytes()
            try:
                value = json.loads(raw)
            except ValueError as exc:
                if store and store.already_handled(path.name, raw=raw):
                    return None
                raise ValueError(f"invalid JSON in {path.name}: {exc}") from exc
            if not isinstance(value, dict):
                if store and store.already_handled(path.name, raw=raw):
                    return None
                raise ValueError(f"{path.name}: expected JSON object")
            if store:
                store.inbox_bytes[path.name] = raw
        else:
            try:
                value = store.read(path.name) if store else _raw(path)
            except FileNotFoundError:
                return None
        if store and store.already_handled(path.name, value):
            return None
        return value


def pending(path):
    try:
        return inbox(path) is not None
    except ValueError:
        return True  # Malformed new input is pending rejection, not completion.


def consume(path, value, applied):
    store = active(Path(path).parent)
    if store is None:
        raise ValueError("receipt consumption requires an authority transaction")
    store.consume(Path(path).name, value, Path(applied).name)


def append_event(run_dir, event):
    path = Path(run_dir) / "events.jsonl"
    try:
        events = read(path)
    except FileNotFoundError:
        events = []
    events.append(event)
    if not write(path, events):
        raise ValueError("event write requires an authority transaction")


def migrate_bound_session(store, source=None):
    if store.authority["kind"] != "session":
        return
    from autopilot_work_items import _load_core_ledger_module
    authority = store.authority
    core = _load_core_ledger_module(store.root / authority["platform"] / authority["session_id"] / "intent-state.json", source)
    return core.migrate_session(root=store.root, platform=authority["platform"],
        session_id=authority["session_id"], transaction=store.connection)


@contextmanager
def accessible_transaction(run_dir, source=None, *, authority=None, permission_denied_noop=False):
    """Only acquisition failure may become a no-op, never processing failure."""
    try:
        manager = transaction(run_dir, source, authority=authority)
        store = manager.__enter__()
    except OSError as exc:
        if not permission_denied_noop or not (isinstance(exc, PermissionError) or exc.errno == errno.EROFS):
            raise
        yield None
        return
    try:
        yield store
    except BaseException:
        if not manager.__exit__(*sys.exc_info()):
            raise
    else:
        manager.__exit__(None, None, None)

"""Restricted SQLite plans and a durable activation journal."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from .core import OperationError, canonical, digest


class StateStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise OperationError("State directory must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "state.sqlite3"
        if self.path.is_symlink():
            raise OperationError("State database must not be a symlink")
        self.db = sqlite3.connect(self.path, timeout=10)
        if os.name == "posix":
            os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('revision', '0');
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, revision INTEGER NOT NULL,
                payload TEXT NOT NULL, digest TEXT NOT NULL, created INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'prepared');
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL, stage TEXT NOT NULL,
                details TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS renewal_deployments (
                parent_id TEXT PRIMARY KEY, child_id TEXT NOT NULL UNIQUE);
        """)

    @property
    def revision(self) -> int:
        return int(self.db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0])

    def plan(self, kind: str, payload: dict, *, expected_revision: int | None = None) -> str:
        identity = secrets.token_hex(16)
        encoded = canonical(payload)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._idle()
            if expected_revision is not None and self.revision != expected_revision:
                raise OperationError('Configuration changed during preparation; prepare a new plan')
            self.db.execute("INSERT INTO plans(id,kind,revision,payload,digest,created) VALUES (?,?,?,?,?,?)",
                            (identity, kind, self.revision, encoded.decode(), digest(encoded), int(time.time())))
        return identity

    def get(self, identity: str) -> tuple[str, dict]:
        self._idle()
        record = self.inspect(identity)
        self._prepared(record)
        return record['kind'], record['payload']

    def inspect(self, identity: str) -> dict:
        """Integrity-checked record, including completed plans for recovery."""
        row = self.db.execute("SELECT kind,revision,payload,digest,status FROM plans WHERE id=?", (identity,)).fetchone()
        if not row:
            raise OperationError("Unknown plan")
        kind, revision, encoded, expected, status = row
        if digest(encoded.encode()) != expected:
            raise OperationError("Plan integrity failure")
        return {'kind': kind, 'revision': revision, 'payload': json.loads(encoded), 'status': status}

    def _prepared(self, record: dict) -> None:
        if record['status'] != 'prepared' or record['revision'] != self.revision:
            raise OperationError('Plan is stale or already applied; prepare a new plan')

    def renewal_child(self, identity: str) -> str | None:
        row = self.db.execute('SELECT child_id FROM renewal_deployments WHERE parent_id=?', (identity,)).fetchone()
        return row[0] if row else None

    def link_renewal(self, parent: str, child: str) -> None:
        """Bind one approved renewal to its candidate before service activation."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._idle()
            parent_record, child_record = self.inspect(parent), self.inspect(child)
            self._prepared(parent_record)
            self._prepared(child_record)
            if parent_record['kind'] != 'panel-renew' or child_record['kind'] != 'deployment':
                raise OperationError('Renewal linkage requires a renewal plan and deployment candidate')
            if self.renewal_child(parent) is not None:
                raise OperationError('Renewal already has a deployment candidate')
            self.db.execute('INSERT INTO renewal_deployments(parent_id,child_id) VALUES (?,?)', (parent, child))

    def _renewal_parent(self, child: str) -> str | None:
        row = self.db.execute('SELECT parent_id FROM renewal_deployments WHERE child_id=?', (child,)).fetchone()
        if row:
            record = self.inspect(row[0])
            self._prepared(record)
            if record['kind'] != 'panel-renew':
                raise OperationError('Invalid parent renewal plan')
            return row[0]
        return None

    def pending(self) -> list[dict]:
        rows = self.db.execute("""SELECT a.plan_id, a.details FROM journal a
            WHERE a.stage='activating' AND NOT EXISTS
                (SELECT 1 FROM journal b WHERE b.plan_id=a.plan_id AND b.id>a.id
                 AND b.stage IN ('committed', 'rolled_back')) ORDER BY a.id""").fetchall()
        return [{'plan_id': identity, 'details': json.loads(details)} for identity, details in rows]

    def _idle(self, allow: str | None = None) -> None:
        if any(item['plan_id'] != allow for item in self.pending()):
            raise OperationError('An interrupted activation requires deployment recover first')

    def record(self, identity: str, stage: str, details: dict) -> None:
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            record = self.inspect(identity)
            if stage == 'activating':
                self._idle()
                self._prepared(record)
                if record['kind'] != 'deployment':
                    raise OperationError('Only deployment plans can activate services')
            elif stage in ('committed', 'rolled_back'):
                raise OperationError('Terminal stages must use commit or abort')
            self.db.execute("INSERT INTO journal(plan_id,stage,details,at) VALUES (?,?,?,?)",
                            (identity, stage, canonical(details).decode(), int(time.time())))

    def commit(self, identity: str) -> None:
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._idle(allow=identity)
            record = self.inspect(identity)
            self._prepared(record)
            if record['kind'] == 'deployment' and not any(item['plan_id'] == identity for item in self.pending()):
                raise OperationError('Deployment commit requires a pending activation')
            if record['kind'] == 'panel-renew' and self.renewal_child(identity) is not None:
                raise OperationError('Renewal must commit through its linked deployment')
            parent = self._renewal_parent(identity)
            self.db.execute("UPDATE plans SET status='applied' WHERE id=?", (identity,))
            self.db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
            self.db.execute("INSERT INTO journal(plan_id,stage,details,at) VALUES (?,'committed','{}',?)",
                            (identity, int(time.time())))
            if parent is not None:
                self.db.execute("UPDATE plans SET status='applied' WHERE id=?", (parent,))
                self.db.execute("INSERT INTO journal(plan_id,stage,details,at) VALUES (?,'committed',?,?)",
                                (parent, canonical({'deployment_plan': identity}).decode(), int(time.time())))

    def abort(self, identity: str, details: dict) -> None:
        """Close an activation only after all rollback actions succeeded."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._prepared(self.inspect(identity))
            if not any(item['plan_id'] == identity for item in self.pending()):
                raise OperationError('Plan has no pending activation to roll back')
            parent = self._renewal_parent(identity)
            self.db.execute("UPDATE plans SET status='failed' WHERE id=?", (identity,))
            self.db.execute("INSERT INTO journal(plan_id,stage,details,at) VALUES (?,'rolled_back',?,?)",
                            (identity, canonical(details).decode(), int(time.time())))
            if parent is not None:
                self.db.execute("UPDATE plans SET status='failed' WHERE id=?", (parent,))
                self.db.execute("INSERT INTO journal(plan_id,stage,details,at) VALUES (?,'rolled_back',?,?)",
                                (parent, canonical({'deployment_plan': identity}).decode(), int(time.time())))

    @contextmanager
    def exclusive(self):
        if os.name != "posix":
            raise OperationError("Apply requires POSIX file locking")
        import fcntl
        lock = self.root / "operation.lock"
        if lock.is_symlink():
            raise OperationError("Unsafe lock file")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except BlockingIOError as exc:
            raise OperationError("Another configuration operation is running") from exc
        finally:
            os.close(fd)

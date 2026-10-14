"""Bounded serial work queue, durable redacted results, no persisted inputs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from .management_errors import ManagementError, public_error


class Jobs:
    def __init__(self, root: Path):
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path = root/'panel-jobs.sqlite3'
        if root.is_symlink() or self.path.is_symlink():
            raise ManagementError('UNSAFE_STATE','任务目录不是受管目录。')
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, operation TEXT, status TEXT, created_at INTEGER, finished_at INTEGER, result TEXT, error TEXT)')
            db.execute("UPDATE jobs SET status='interrupted', finished_at=?, error=? WHERE status IN ('queued','running')",
                       (int(time.time()),json.dumps({'code':'INTERRUPTED','message':'管理器中断了任务。请检查实际状态；部署变更可使用恢复入口。'})))
        if os.name == 'posix':
            self.path.chmod(0o600)
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='proxyhub-operation')
        self.slots = threading.BoundedSemaphore(8)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def submit(self, operation: str, function) -> dict:
        if not self.slots.acquire(blocking=False):
            raise ManagementError('BUSY','任务队列已满，请等待当前任务完成。')
        identity = secrets.token_hex(16)
        try:
            with self._db() as db:
                db.execute('INSERT INTO jobs VALUES (?,?,?,?,NULL,NULL,NULL)',(identity,operation,'queued',int(time.time())))
            self.worker.submit(self._run,identity,function)
        except Exception:
            self.slots.release()
            raise
        return {'job_id':identity}

    def _run(self, identity, function):
        try:
            with self._db() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?",(identity,))
            try:
                result, error, status = function(), None, 'succeeded'
            except Exception as exc:
                result, error, status = None, public_error(exc), 'failed'
            with self._db() as db:
                db.execute('UPDATE jobs SET status=?, finished_at=?, result=?, error=? WHERE id=?',
                           (status,int(time.time()),json.dumps(result,ensure_ascii=False),json.dumps(error,ensure_ascii=False),identity))
                # Bounded history; preserve all active jobs. Inputs are never stored.
                db.execute("DELETE FROM jobs WHERE status NOT IN ('queued','running') AND id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 200)")
        finally:
            self.slots.release()

    def get(self, identity):
        with self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?',(identity,)).fetchone()
        if not row:
            raise ManagementError('NOT_FOUND','任务不存在或已超出保留范围。')
        return self._row(row)

    def list(self):
        with self._db() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 30').fetchall()
        return {'jobs':[self._row(row) for row in rows]}

    @staticmethod
    def _row(row):
        result = dict(zip(('id','operation','status','created_at','finished_at','result','error'),row))
        for field in ('result','error'):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def close(self):
        self.worker.shutdown(wait=True)

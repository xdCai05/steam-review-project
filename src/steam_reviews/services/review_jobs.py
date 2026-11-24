"""Durable single-worker review queue and overview membership."""
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from ..contracts import ConfigError

SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, appid INTEGER NOT NULL, name TEXT NOT NULL,
    status TEXT NOT NULL, created INTEGER NOT NULL, started INTEGER, finished INTEGER,
    window_start INTEGER, window_end INTEGER, segments INTEGER,
    message TEXT, accepted INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_app ON jobs(appid)
    WHERE status IN ('queued','running','pausing','paused');
CREATE TABLE IF NOT EXISTS overview (appid INTEGER PRIMARY KEY, added INTEGER NOT NULL);
'''


@contextmanager
def job_db(database):
    path = database.with_name(database.stem + '.jobs.sqlite')
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


def enqueue(database, appid, name):
    with job_db(database) as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT id FROM jobs WHERE appid=? AND status IN ('queued','running','pausing','paused')", (appid,)).fetchone()
        if active:
            return active['id']
        job_id = 'managed_' + uuid4().hex
        db.execute('INSERT INTO jobs(id,appid,name,status,created) VALUES (?,?,?,?,?)',
                   (job_id, appid, name, 'queued', int(time.time())))
        return job_id


def overview_ids(database):
    with job_db(database) as db:
        return [row['appid'] for row in db.execute('SELECT appid FROM overview ORDER BY added,rowid')]


def list_jobs(database):
    with job_db(database) as db:
        active = db.execute("SELECT * FROM jobs WHERE status IN ('queued','running','pausing','paused') ORDER BY rowid")
        history = db.execute("SELECT * FROM jobs WHERE status NOT IN ('queued','running','pausing','paused') ORDER BY rowid DESC LIMIT 60")
        return [dict(row) for row in [*active, *history]]


def get_job(database, job_id):
    with job_db(database) as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ConfigError('找不到该采集任务')
        return dict(row)


def change_job(database, job_id, action):
    transitions = {'pause': ('running', 'pausing'), 'resume': ('paused', 'queued'),
                   'remove': ('queued', 'cancelled'), 'stop': ('paused', 'cancelled')}
    with job_db(database) as db:
        db.execute('BEGIN IMMEDIATE')
        if action == 'publish':
            job = db.execute("SELECT appid FROM jobs WHERE id=? AND status='limited' AND accepted>0", (job_id,)).fetchone()
            if not job:
                raise ConfigError('该任务没有可加入总览的评测')
            db.execute('INSERT OR IGNORE INTO overview VALUES (?,?)', (job['appid'], int(time.time())))
            return
        if action not in transitions:
            raise ConfigError('未知任务操作')
        old, new = transitions[action]
        finished = int(time.time()) if new == 'cancelled' else None
        changed = db.execute('UPDATE jobs SET status=?, finished=?, message=NULL WHERE id=? AND status=?',
                             (new, finished, job_id, old)).rowcount
        if not changed:
            raise ConfigError('任务状态已变更，请刷新后重试')


def recover_jobs(database):
    with job_db(database) as db:
        db.execute("UPDATE jobs SET status='queued',message='从上次检查点恢复' WHERE status='running'")
        db.execute("UPDATE jobs SET status='paused',message='后端停止，进度已保留' WHERE status='pausing'")


def _queue_head(db):
    return db.execute("SELECT * FROM jobs WHERE status IN ('queued','paused','running','pausing') ORDER BY rowid LIMIT 1").fetchone()


def queue_head(database):
    with job_db(database) as db:
        row = _queue_head(db)
        return dict(row) if row else None


def claim_job(database, settings):
    with job_db(database) as db:
        db.execute('BEGIN IMMEDIATE')
        job = _queue_head(db)
        if not job or job['status'] != 'queued':
            return None
        if job['window_start'] is None:
            db.execute('UPDATE jobs SET window_start=?,window_end=?,segments=?,started=? WHERE id=?',
                       (settings.window_start_ts, settings.window_end_ts, len(settings.segments()), int(time.time()), job['id']))
        db.execute("UPDATE jobs SET status='running',message=NULL WHERE id=?", (job['id'],))
        return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())


def note_writer_wait(database, job_id):
    with job_db(database) as db:
        db.execute("UPDATE jobs SET message='等待其他采集任务释放写锁' WHERE id=? AND status='queued'", (job_id,))


def finish_job(database, job, status, message=None, accepted=0):
    with job_db(database) as db:
        finished = None if status == 'paused' else int(time.time())
        db.execute('UPDATE jobs SET status=?,message=?,finished=?,accepted=? WHERE id=?',
                   (status, message, finished, accepted, job['id']))
        if status == 'completed':
            db.execute('INSERT OR IGNORE INTO overview VALUES (?,?)', (job['appid'], finished))

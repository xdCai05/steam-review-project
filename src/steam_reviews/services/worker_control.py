"""Local lifecycle control for the independent review collection worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

from ..storage.lease import LockBusy, exclusive_file
from ..storage.review_files import atomic_json

START_TIMEOUT = 25
HEARTBEAT_TIMEOUT = 15


def read_record(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def recent(path: Path, seconds: int) -> bool:
    try:
        return time.time() - path.stat().st_mtime < seconds
    except FileNotFoundError:
        return False


class WorkerRuntime:
    def __init__(self, tasks_dir: Path, database: Path):
        self.database_path = database.resolve()
        self.database = os.path.normcase(str(self.database_path))
        self.worker_lock = tasks_dir / '.review-worker.lock'
        self.control_lock = tasks_dir / '.review-worker-control.lock'
        self.heartbeat = tasks_dir / '.review-worker-heartbeat'
        self.request = tasks_dir / '.review-worker-request.json'

    def accepts_start(self, session: str) -> bool:
        request = read_record(self.request)
        return (request.get('action') == 'start' and request.get('session') == session
                and request.get('database') == self.database and recent(self.request, START_TIMEOUT))

    @contextmanager
    def publish(self, stopped: Event, session: str | None = None):
        """Publish the worker identity while the caller holds its exclusive worker lock."""
        session = session or uuid4().hex
        atomic_json(self.heartbeat, {'session': session, 'database': self.database})
        done = Event()

        def monitor():
            last_pulse = time.monotonic()
            while not done.wait(.35):
                request = read_record(self.request)
                if request.get('action') == 'stop' and request.get('session') == session:
                    stopped.set()
                if time.monotonic() - last_pulse >= 4:
                    self.heartbeat.touch()
                    last_pulse = time.monotonic()

        thread = Thread(target=monitor, name='review-worker-control', daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join()
            self.heartbeat.unlink(missing_ok=True)


class WorkerController:
    def __init__(self, runtime: WorkerRuntime, root: Path, worker_script: Path, log: Path):
        self.runtime = runtime
        self.root = root
        self.worker_script = worker_script
        self.log = log
        self._lock = Lock()
        self._child: subprocess.Popen | None = None
        self._launch_session: str | None = None

    def _state(self) -> tuple[dict, str | None]:
        runtime = self.runtime
        request = read_record(runtime.request)
        pending = (request.get('action') == 'start' and request.get('database') == runtime.database
                   and recent(runtime.request, START_TIMEOUT))
        exit_code = self._child.poll() if self._child is not None else None
        finished = pending and request.get('session') == self._launch_session and exit_code is not None
        if pending and not finished and not runtime.heartbeat.exists():
            return {'state': 'starting', 'online': False, 'message': ''}, None
        try:
            with exclusive_file(runtime.worker_lock):
                busy = False
        except LockBusy:
            busy = True
        heartbeat = read_record(runtime.heartbeat) if busy else {}
        session = heartbeat.get('session')
        message = ''
        if busy and isinstance(session, str) and heartbeat.get('database') == runtime.database:
            if request.get('action') == 'stop' and request.get('session') == session:
                state = 'stopping'
            elif recent(runtime.heartbeat, HEARTBEAT_TIMEOUT):
                state = 'online'
                if request.get('action') == 'start' and request.get('session') == session:
                    try:
                        runtime.request.unlink(missing_ok=True)
                    except PermissionError:
                        pass  # The worker may still hold a read handle on Windows.
            else:
                state = 'unavailable'
                message = '采集后端未响应，请检查采集进程。'
        elif pending and not finished and not heartbeat:
            state = 'starting'
        elif busy:
            state = 'unavailable'
            message = '已有采集进程占用此项目，请确认它使用当前数据库和版本。'
        else:
            state = 'offline'
            if finished and exit_code:
                message = '采集进程启动失败，请查看 logs/review-worker-service.log。'
        return {'state': state, 'online': state == 'online', 'message': message}, session

    def status(self) -> dict:
        with self._lock, exclusive_file(self.runtime.control_lock):
            return self._state()[0]

    def change(self, action: str) -> dict:
        if action not in ('start', 'stop'):
            raise ValueError('无效的采集后端操作')
        with self._lock, exclusive_file(self.runtime.control_lock):
            status, session = self._state()
            if action == 'stop' and status['state'] == 'online':
                atomic_json(self.runtime.request, {'action': 'stop', 'session': session})
                return {'state': 'stopping', 'online': False, 'message': ''}
            if action == 'start' and status['state'] == 'offline':
                self._start()
                return {'state': 'starting', 'online': False, 'message': ''}
            return status

    def _start(self) -> None:
        session = uuid4().hex
        self.log.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.runtime.request, {'action': 'start', 'session': session, 'database': self.runtime.database})
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        try:
            with self.log.open('ab') as log:
                self._child = subprocess.Popen(
                    [sys.executable, str(self.worker_script), '--root', str(self.root), '--database', str(self.runtime.database_path),
                     '--control-session', session], cwd=self.root, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, **flags,
                )
        except OSError:
            self.runtime.request.unlink(missing_ok=True)
            raise
        self._launch_session = session

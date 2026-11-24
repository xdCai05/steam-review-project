#!/usr/bin/env python
"""独立处理评测队列。前端服务和浏览器可以单独退出。"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from steam_reviews.contracts import CollectionRequest, ConfigError, SteamError
from steam_reviews.services.collection import collect_reviews_locked
from steam_reviews.services.review_jobs import (
    claim_job, finish_job, get_job, note_writer_wait, queue_head, recover_jobs,
)
from steam_reviews.services.worker_control import WorkerRuntime
from steam_reviews.settings import RequestConfig, Settings, load_settings
from steam_reviews.storage.database import connect
from steam_reviews.storage.lease import LockBusy, collection_write_lock, exclusive_file


@contextmanager
def job_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s  %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    worker_log = logging.getLogger('steam_reviews.worker')
    loggers = [worker_log, logging.getLogger('steam_reviews.services.collection'),
               logging.getLogger('steam_reviews.crawlers.client')]
    original_levels = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
        yield worker_log
    finally:
        for logger, level in zip(loggers, original_levels):
            logger.removeHandler(handler)
            logger.setLevel(level)
        handler.close()


def frozen_settings(settings: Settings, job: dict, task_dir: Path) -> Settings:
    frozen = task_dir / 'manifest.json'
    if frozen.is_file():
        recorded = json.loads(frozen.read_text(encoding='utf-8'))
        window = recorded['window']
        timezone_name = window['timezone']
        request = recorded['request']
        return replace(settings, timezone_name=timezone_name,
                       window_start=datetime.fromtimestamp(window['start_ts'], ZoneInfo(timezone_name)),
                       window_end=datetime.fromtimestamp(window['end_ts'], ZoneInfo(timezone_name)),
                       segment_order=recorded['segment_order'],
                       request=RequestConfig(**{**request, 'languages': tuple(request['languages'])}))
    return replace(settings, window_start=datetime.fromtimestamp(job['window_start'], settings.tz),
                   window_end=datetime.fromtimestamp(job['window_end'], settings.tz))


def window_review_count(settings: Settings, appid: int) -> int:
    with closing(connect(settings.paths.database, readonly=True)) as db:
        return int(db.execute('SELECT COUNT(*) FROM reviews WHERE appid=? AND created_at>=? AND created_at<?',
                              (appid, settings.window_start_ts, settings.window_end_ts)).fetchone()[0])


def run_claimed(settings: Settings, job: dict, stopped: Event) -> None:
    database = settings.paths.database
    job_id = job['id']
    task_dir = settings.task_dir(job_id)

    def should_stop() -> bool:
        return stopped.is_set() or get_job(database, job_id)['status'] == 'pausing'

    with job_log(task_dir / 'review_job.log') as log:
        try:
            settings = frozen_settings(settings, job, task_dir)
            log.info('AppID %d · 评测范围 %s 至 %s（截止时刻不含）',
                     job['appid'], settings.window_start.isoformat(timespec='seconds'),
                     settings.window_end.isoformat(timespec='seconds'))
            request = CollectionRequest(run_id=job_id, appids=[job['appid']],
                                        param_style=settings.request.param_style)
            result = collect_reviews_locked(settings, request, should_stop=should_stop)
            if result.completed:
                accepted = window_review_count(settings, job['appid'])
                message = '采集完成' if accepted else '近 90 天无评测'
                finish_job(database, job, 'completed', message, accepted)
                log.info('%s，窗口内共 %d 条评测', message, accepted)
            elif result.stop_reason == 'capacity_limit':
                accepted = window_review_count(settings, job['appid'])
                finish_job(database, job, 'limited', '达到单款评测容量上限', accepted)
                log.info('达到单款评测容量上限，窗口内已保存 %d 条评测', accepted)
            elif stopped.is_set():
                recover_jobs(database)
                log.info('采集后端停止，检查点已保留')
            elif result.status == 'paused':
                message = '已暂停，检查点已保留' if result.stop_reason == 'stopped' else '采集暂停，检查点已保留'
                finish_job(database, job, 'paused', message)
                log.info('%s（%s）', message, result.stop_reason or 'paused')
            else:
                reason = result.stop_reason or next((part.stop_reason for part in result.segments if part.stop_reason), result.status)
                finish_job(database, job, 'failed', f'采集未完成（{reason}）')
                log.info('采集未完成（%s）', reason)
        except Exception as exc:
            reason = exc.code if isinstance(exc, SteamError) else type(exc).__name__
            if stopped.is_set():
                recover_jobs(database)
            elif (task_dir / 'pending_page.json').is_file():
                finish_job(database, job, 'paused', '写入中断，继续后会先修复未完成的写入')
            else:
                finish_job(database, job, 'failed', f'采集异常（{reason}）')
            log.error('采集异常（%s）', reason)


def run_worker(settings: Settings, stopped: Event, control_session: str | None = None) -> None:
    database = settings.paths.database
    runtime = WorkerRuntime(settings.paths.tasks_dir, database)
    with exclusive_file(runtime.worker_lock):
        if control_session and not runtime.accepts_start(control_session):
            raise ConfigError('采集后端启动请求已过期，请重新启动')
        runtime.heartbeat.unlink(missing_ok=True)
        recover_jobs(database)
        with runtime.publish(stopped, control_session):
            while not stopped.is_set():
                head = queue_head(database)
                if head and head['status'] == 'queued':
                    try:
                        with collection_write_lock(settings):
                            current = load_settings(settings.root)
                            current = replace(current, paths=replace(current.paths, database=database))
                            if stopped.is_set():
                                break
                            job = claim_job(database, current)
                            if job:
                                run_claimed(current, job, stopped)
                    except LockBusy:
                        note_writer_wait(database, head['id'])
                stopped.wait(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--database', type=Path, help='和前端服务一致的评测数据库路径')
    parser.add_argument('--control-session', help=argparse.SUPPRESS)
    args = parser.parse_args()
    stopped = Event()
    for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, lambda _signum, _frame: stopped.set())
    try:
        settings = load_settings(args.root)
        if args.database is not None:
            settings = replace(settings, paths=replace(settings.paths, database=args.database.resolve()))
        run_worker(settings, stopped, args.control_session)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""按游戏和发表月份保存唯一评测；使用提交日志恢复文件/数据库的中断。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ..contracts import StorageFailure
from ..processing.normalize import to_jsonl_line
from . import repository
from .database import transaction


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, data) -> None:
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise StorageFailure(f'评测分片不可读取：{path}', detail={'error': str(exc)}) from exc


def prepare_page(settings, conn, appid: int, records: list[dict]) -> dict:
    """更新已有评测原分片；新增评测按月份分配新分片。此阶段不写磁盘。"""
    folder = settings.review_dir(appid)
    unique = {}
    for record in records:
        previous = unique.get(record['review_id'])
        if previous is None or record['collected_at'] >= previous['collected_at']:
            unique[record['review_id']] = record
    grouped, locations, allocated = {}, [], {}
    for record in unique.values():
        review_id = record['review_id']
        prior = conn.execute('SELECT appid,relative_path FROM review_locations WHERE review_id=?', (review_id,)).fetchone()
        if prior and prior['appid'] != appid:
            raise StorageFailure('同一评测ID关联不同游戏')
        if prior:
            relative = prior['relative_path']
        else:
            month = datetime.fromtimestamp(record['created_at'], settings.tz).strftime('%Y-%m')
            if month not in allocated:
                existing = [int(p.stem.split('_')[-1]) for p in (folder / month).glob('part_*.jsonl')]
                allocated[month] = f'{month}/part_{max(existing, default=0)+1:05d}.jsonl'
            relative = allocated[month]
        target = (folder / relative).resolve()
        if folder.resolve() not in target.parents:
            raise StorageFailure('评测路径越界')
        grouped.setdefault(relative, []).append(record)
        locations.append([review_id, appid, relative])
    files, delta = [], 0
    for relative, incoming in grouped.items():
        path = folder / relative
        merged = {r['review_id']: r for r in read_records(path)}
        for record in incoming:
            old = merged.get(record['review_id'])
            if old is None or record['collected_at'] >= old['collected_at']:
                merged[record['review_id']] = record
        text = ''.join(to_jsonl_line(r)+'\n' for r in merged.values())
        delta += len(text.encode('utf-8')) - (path.stat().st_size if path.exists() else 0)
        files.append({'relative_path': relative, 'text': text})
    return {'appid': appid, 'records': list(unique.values()), 'locations': locations, 'files': files, 'delta_bytes': delta}


def _apply(settings, conn, payload: dict, *, replay: bool) -> dict:
    plan = payload['plan']
    with transaction(conn):
        counts = repository.bulk_upsert_reviews(conn, plan['records'])
        conn.executemany('INSERT INTO review_locations(review_id,appid,relative_path) VALUES (?,?,?) '
                         'ON CONFLICT(review_id) DO UPDATE SET relative_path=excluded.relative_path', plan['locations'])
        state = payload.get('checkpoint')
        if state and not replay:
            state['inserted'] += counts['inserted']
            state['updated'] += counts['updated']
        # Journal is durable before any canonical file is replaced or transaction commits.
        if not replay:
            atomic_json(settings.task_dir(payload['run_id']) / 'pending_page.json', payload)
        root = settings.review_dir(plan['appid']).resolve()
        for entry in plan['files']:
            target = (root / entry['relative_path']).resolve()
            if root not in target.parents:
                raise StorageFailure('恢复日志包含越界路径')
            atomic_bytes(target, entry['text'].encode('utf-8'))
        if state:
            repository.upsert_segment(conn, **state)
    return counts


def recover_pending(settings, conn, run_id: str) -> bool:
    journal = settings.task_dir(run_id) / 'pending_page.json'
    if not journal.exists():
        return False
    payload = json.loads(journal.read_text(encoding='utf-8'))
    if payload['run_id'] != run_id:
        raise StorageFailure('恢复日志的任务ID不匹配')
    _apply(settings, conn, payload, replay=True)
    journal.unlink()
    return True


def commit_page(settings, conn, run_id: str, plan: dict, checkpoint=None) -> dict:
    payload = {'run_id': run_id, 'plan': plan, 'checkpoint': checkpoint}
    counts = _apply(settings, conn, payload, replay=False)
    (settings.task_dir(run_id) / 'pending_page.json').unlink()
    return counts

"""Read models shared by the dashboard and the game comparison endpoint."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))
COMPARISON_LIMIT = 10
Catalog = Mapping[int, Mapping[str, Any]]


class ComparisonError(ValueError):
    """The requested comparison selection is invalid."""


def game_list(db: sqlite3.Connection, catalog: Catalog, appid: int | None = None) -> list[dict[str, Any]]:
    scope = ' WHERE appid = ?' if appid is not None else ''
    args = (appid,) if appid is not None else ()
    rows = {row['appid']: dict(row) for row in db.execute('''
        SELECT appid, COUNT(*) AS total, MIN(created_at) AS first, MAX(created_at) AS last,
               COUNT(is_recommended) AS rated, COALESCE(SUM(is_recommended = 1), 0) AS positive
        FROM reviews''' + scope + ' GROUP BY appid', args)}
    states = {}
    segment_scope = ' WHERE s.appid = ?' if appid is not None else ''
    for row in db.execute('''WITH recent AS (
        SELECT s.appid, s.status, s.stop_reason, r.params,
               DENSE_RANK() OVER (PARTITION BY s.appid ORDER BY r.started_at DESC, r.run_id DESC) AS rank
        FROM crawl_segments s JOIN crawl_runs r ON r.run_id=s.run_id''' + segment_scope + '''
        ) SELECT appid, status, stop_reason, params FROM recent WHERE rank=1''', args):
        states.setdefault(row['appid'], []).append(row)
    details_by_id = {row['appid']: row for row in db.execute(
        'SELECT appid, name, genres, developers, cover_local_path FROM games' + scope, args)}
    result = []
    for key, game in (catalog.items() if appid is None else [(appid, catalog[appid])]):
        details = details_by_id.get(key)
        stats = rows.get(key, {'total': 0, 'first': None, 'last': None, 'rated': 0, 'positive': 0})
        segments = states.get(key, [])
        complete = False
        if segments and all(s['status'] == 'completed' for s in segments):
            window = json.loads(segments[0]['params']).get('window', {})
            if window.get('start_ts') is not None and window.get('end_ts') is not None:
                start = datetime.fromtimestamp(window['start_ts'], CST)
                end = datetime.fromtimestamp(window['end_ts'] - 1, CST)
                complete = len(segments) == (end.year - start.year) * 12 + end.month - start.month + 1
        status = ('达到容量上限' if any(s['stop_reason'] == 'capacity_limit' for s in segments)
                  else '采集完成' if complete
                  else '采集未完成' if segments else '暂无采集记录')
        result.append({**game, **stats, 'status': status,
                       'name': game.get('name') or (details and details['name']) or f'AppID {key}',
                       'store_ready': bool(details and details['name']),
                       'store_checked': details is not None,
                       'has_cover': bool(details and details['cover_local_path']),
                       'genres': json.loads(details['genres'] or '[]') if details else [],
                       'developers': json.loads(details['developers'] or '[]') if details else []})
    return result


def game_detail(db: sqlite3.Connection, appid: int, catalog: Catalog) -> dict[str, Any]:
    game = game_list(db, catalog, appid)[0]
    row = db.execute('''SELECT short_description, publishers, platforms,
        release_date, release_date_raw FROM games WHERE appid=?''', (appid,)).fetchone()
    metadata = dict(row) if row else dict.fromkeys(('short_description', 'release_date', 'release_date_raw'))
    for name in ('publishers', 'platforms'):
        metadata[name] = json.loads(metadata.get(name) or '[]')
    price = db.execute('''SELECT price_final, currency, region, collected_at, is_free
        FROM game_snapshots WHERE appid=? ORDER BY collected_at DESC, snapshot_id DESC LIMIT 1''', (appid,)).fetchone()
    daily = [dict(r) for r in db.execute('''SELECT date(created_at,'unixepoch','+8 hours') AS day,
        COUNT(*) AS total, COUNT(is_recommended) AS rated, COALESCE(SUM(is_recommended=1), 0) AS positive
        FROM reviews WHERE appid=? GROUP BY day ORDER BY day''', (appid,))]
    buckets = [dict(r) for r in db.execute('''WITH grouped AS (
        SELECT CASE WHEN playtime_at_review<120 THEN 0 WHEN playtime_at_review<600 THEN 1 ELSE 2 END AS bucket,
        is_recommended FROM reviews WHERE appid=? AND playtime_at_review IS NOT NULL)
        SELECT bucket, COUNT(*) AS total, COUNT(is_recommended) AS rated,
        COALESCE(SUM(is_recommended=1), 0) AS positive FROM grouped GROUP BY bucket ORDER BY bucket''', (appid,))]
    rating = db.execute('''SELECT review_score_desc, total_reviews, positive_percent_display, collected_at
        FROM review_summary_snapshots WHERE appid=? AND scope='page_language' AND total_reviews IS NOT NULL
        ORDER BY collected_at DESC, summary_id DESC LIMIT 1''', (appid,)).fetchone()
    return {**game, **metadata, 'price': dict(price) if price else None, 'daily': daily,
            'playtime': buckets, 'store_rating': dict(rating) if rating else None}


def compare_games(db: sqlite3.Connection, appids: Sequence[int], catalog: Catalog) -> dict[str, Any]:
    """Return ordered game profiles from one SQLite read snapshot."""
    selected = tuple(appids)
    if not 2 <= len(selected) <= COMPARISON_LIMIT:
        raise ComparisonError(f'请选择 2 至 {COMPARISON_LIMIT} 款游戏进行对比')
    if any(type(appid) is not int or appid not in catalog for appid in selected):
        raise ComparisonError('请选择已加入游戏总览的游戏')
    if len(set(selected)) != len(selected):
        raise ComparisonError('对比游戏不能重复')

    # Keep totals, daily counts and playtime cohorts consistent when collection is running.
    db.execute('SAVEPOINT compare_games')
    try:
        observed_at = int(time.time())
        games = []
        for appid in selected:
            game = game_detail(db, appid, catalog)
            game['recommendation_rate'] = game['positive'] / game['rated'] if game['rated'] else None
            game['playtime_known'] = sum(bucket['total'] for bucket in game['playtime'])
            games.append(game)
        return {'games': games, 'timezone': 'Asia/Shanghai', 'observed_at': observed_at}
    finally:
        db.execute('RELEASE SAVEPOINT compare_games')

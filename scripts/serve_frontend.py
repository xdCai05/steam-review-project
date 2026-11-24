"""Serve the local Steam review dashboard and review task controls."""
import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from steam_reviews.catalog import load_catalog, validate_appid
from steam_reviews.contracts import ConfigError
from steam_reviews.services import review_jobs
from steam_reviews.services.game_queries import ComparisonError, compare_games, game_detail, game_list
from steam_reviews.services.safety import review_storage_bytes
from steam_reviews.services.review_wordcloud import wordclouds
from steam_reviews.services.worker_control import WorkerController, WorkerRuntime
from steam_reviews.settings import load_settings

WEB = ROOT / 'frontend'
CST = timezone(timedelta(hours=8))
JOB_PATH = re.compile(r'/api/review-jobs/(managed_[a-f0-9]{32})')
WORKER_PATH = '/api/review-worker'


def overview_catalog(root, database, catalog):
    initial = load_catalog(root, include_added=False)
    ids = dict.fromkeys([*initial, 431960, *review_jobs.overview_ids(database)])
    return {appid: catalog[appid] for appid in ids if appid in catalog}


def candidate_list(db, catalog, included, params):
    query = params.get('q', [''])[0].strip().casefold()
    genre = params.get('genre', [''])[0]
    if len(query) > 100 or len(genre) > 100:
        raise ValueError('搜索条件过长')
    items, available_genres = [], set()
    for row in db.execute('''SELECT appid,name,developers,genres,short_description,release_date,
                             release_date_raw,cover_local_path FROM games WHERE name IS NOT NULL'''):
        appid = row['appid']
        if appid not in catalog:
            continue
        game = catalog[appid]
        name = game.get('name') or row['name']
        genres = [item if isinstance(item, str) else item.get('description', '')
                  for item in json.loads(row['genres'] or '[]')]
        genres = [label for label in genres if label]
        available_genres.update(genres)
        if genre and genre not in genres:
            continue
        developers = json.loads(row['developers'] or '[]')
        terms = [name, row['name'], game.get('name_en', ''), str(appid), *developers]
        if query and not any(query in str(item).casefold() for item in terms):
            continue
        items.append({'appid': appid, 'name': name, 'has_cover': bool(row['cover_local_path']),
                      'genres': genres, 'developers': developers, 'short_description': row['short_description'],
                      'release_date': row['release_date'] or row['release_date_raw'], 'in_overview': appid in included})
    order = {appid: index for index, appid in enumerate(catalog)}
    items.sort(key=lambda game: order[game['appid']])
    pages = max(1, (len(items) + 19) // 20)
    page = min(pages, max(1, int(params.get('page', ['1'])[0])))
    return {'items': items[(page-1)*20:page*20], 'page': page, 'pages': pages,
            'total': len(items), 'genres': sorted(available_genres)}


def job_progress(db, settings, job, included):
    job_id, appid = job['id'], job['appid']
    segments = db.execute('''SELECT start_ts,status,pages,inserted,updated FROM crawl_segments
                             WHERE run_id=? AND appid=? ORDER BY updated_at DESC, segment_index''', (job_id, appid)).fetchall()
    task_dir = settings.task_dir(job_id)
    manifest = task_dir / 'manifest.json'
    limit = settings.require_policy().quota_for(appid).max_review_bytes
    if manifest.is_file():
        frozen = json.loads(manifest.read_text(encoding='utf-8')).get('policy', {})
        limit = frozen.get('resolved_quotas', {}).get(str(appid), {}).get('max_review_bytes', limit)
    log_path = task_dir / 'review_job.log'
    logs = []
    if log_path.is_file():
        with log_path.open('rb') as log:
            end = log.seek(0, 2)
            start = max(0, end - 24_576)
            log.seek(start)
            if start:
                log.readline()
            logs = log.read().decode('utf-8', errors='replace').splitlines()[-70:]
    active = next((segment for segment in segments if segment['status'] in ('running', 'paused')), None)
    month = datetime.fromtimestamp(active['start_ts'], settings.tz).strftime('%Y-%m') if active else None
    return {**job, 'in_overview': appid in included, 'logs': logs,
            'progress': {'done': sum(s['status'] == 'completed' for s in segments), 'total': job['segments'],
                         'month': month, 'pages': sum(s['pages'] for s in segments),
                         'inserted': sum(s['inserted'] for s in segments), 'updated': sum(s['updated'] for s in segments),
                         'bytes': review_storage_bytes(settings.review_dir(appid)), 'max_bytes': limit}}


def review_scope(params, catalog):
    def arg(name, default=''):
        return params.get(name, [default])[0]
    where, values = [], []
    if arg('game'):
        appid = int(arg('game'))
        if appid not in catalog:
            raise ValueError('请选择有效的游戏')
        where.append('appid=?')
        values.append(appid)
    if arg('rating'):
        if arg('rating') not in {'0', '1'}:
            raise ValueError('请选择有效的推荐状态')
        where.append('is_recommended=?')
        values.append(int(arg('rating')))
    start, end = (date.fromisoformat(arg(key)) if arg(key) else None for key in ('start', 'end'))
    if start and end and start > end:
        raise ValueError('结束日期应晚于或等于开始日期')
    for day, operator in ((start, '>='), (end + timedelta(days=1) if end else None, '<')):
        if day:
            where.append('created_at ' + operator + ' ?')
            values.append(int(datetime(day.year, day.month, day.day, tzinfo=CST).timestamp()))
    playtime = arg('playtime')
    bands = {'short': 'playtime_at_review < 120',
             'medium': 'playtime_at_review >= 120 AND playtime_at_review < 600',
             'long': 'playtime_at_review >= 600'}
    if playtime:
        if playtime not in bands:
            raise ValueError('请选择有效的游玩时长')
        where.append(bands[playtime])
    return where, values


def cloud_scope(params, catalog):
    if not params.get('game', [''])[0]:
        raise ValueError('请选择一款游戏')
    where, values = review_scope(params, catalog)
    return ' WHERE ' + ' AND '.join(where), values


def reviews(db, params, catalog):
    def arg(name, default=''):
        return params.get(name, [default])[0]
    where, values = review_scope(params, catalog)
    keyword = arg('q').strip()
    if len(keyword) > 100:
        raise ValueError('关键词最多 100 字')
    if keyword:
        where.append("instr(lower(coalesce(content,'')), lower(?)) > 0")
        values.append(keyword)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    sort = 'coalesce(helpful_votes,0) DESC, ' if arg('sort') == 'helpful' else ''
    total = db.execute('SELECT COUNT(*) FROM reviews' + clause, values).fetchone()[0]
    pages = max(1, (total + 19) // 20)
    page = max(1, min(int(arg('page', '1')), pages))
    rows = db.execute('''SELECT review_id, appid, content, is_recommended, created_at,
        playtime_at_review, playtime_forever, helpful_votes, developer_response, timestamp_dev_responded FROM reviews'''
        + clause + ' ORDER BY ' + sort + 'created_at DESC, review_id DESC LIMIT 20 OFFSET ?',
        [*values, (page - 1) * 20])
    return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'pages': pages}


class Handler(BaseHTTPRequestHandler):
    project_root = ROOT
    database = ROOT / 'data/database/steam_reviews.db'

    def send_bytes(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if content_type.startswith('application/json'):
            self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8', status)

    def do_POST(self):
        url = urlsplit(self.path)
        path = url.path
        match = JOB_PATH.fullmatch(path)
        if path not in {'/api/review-jobs', '/api/review-wordcloud', WORKER_PATH} and not match:
            self.json({'error': '找不到该页面或数据'}, 404)
            return
        if self.headers.get_content_type() != 'application/json':
            self.json({'error': '只接受 JSON 请求'}, 415)
            return
        hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        host = self.headers.get('Host')
        if host not in hosts or self.headers.get('Origin') not in (None, f'http://{host}'):
            self.json({'error': '请从本地项目页面操作任务'}, 403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 256:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
            if path == WORKER_PATH:
                action = body.get('action')
                if set(body) != {'action'} or action not in ('start', 'stop'):
                    self.json({'error': '无效的采集后端操作'}, 400)
                    return
                self.json(self.worker_controller.change(action))
                return
            if path == '/api/review-wordcloud':
                included = overview_catalog(self.project_root, self.database, load_catalog(self.project_root))
                clause, values = cloud_scope(parse_qs(url.query), included)
                wordclouds.start(self.database, clause, values, included[values[0]].get('name') or '')
                self.json(wordclouds.get(self.database, clause, values), 202)
                return
            if match:
                job_id = match[1]
                action = body.get('action')
                if not isinstance(action, str):
                    raise ValueError
                review_jobs.change_job(self.database, job_id, action)
            else:
                appid = body.get('appid')
                validate_appid(appid)
                catalog = load_catalog(self.project_root)
                with closing(self.open_database()) as db:
                    row = db.execute('SELECT name FROM games WHERE appid=? AND name IS NOT NULL', (appid,)).fetchone()
                if appid not in catalog or row is None:
                    raise ConfigError('请选择已准备商店资料的候选游戏')
                job_id = review_jobs.enqueue(self.database, appid, catalog[appid].get('name') or row['name'])
            self.json({'id': job_id}, 200 if match else 202)
        except (ValueError, OverflowError):
            self.json({'error': '任务请求参数有误'}, 400)
        except ConfigError as exc:
            self.json({'error': str(exc)}, 409)
        except (sqlite3.Error, OSError) as exc:
            self.log_error('任务变更失败: %s', exc)
            self.json({'error': '无法控制采集后端，请稍后重试' if path == WORKER_PATH else '无法保存任务，请稍后重试'}, 503)

    def open_database(self):
        db = sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        return db

    def do_GET(self):
        url = urlsplit(self.path)
        assets = {'/': ('index.html', 'text/html'), '/style.css': ('style.css', 'text/css'),
                  '/theme.js': ('theme.js', 'text/javascript'),
                  '/app.js': ('app.js', 'text/javascript'),
                  '/game-comparison.css': ('game-comparison.css', 'text/css'),
                  '/game-comparison.js': ('game-comparison.js', 'text/javascript'),
                  '/compare-reorder.js': ('compare-reorder.js', 'text/javascript'),
                  '/review-tasks.js': ('review-tasks.js', 'text/javascript'),
                  '/review-worker-control.js': ('review-worker-control.js', 'text/javascript'),
                  '/review-worker-control.css': ('review-worker-control.css', 'text/css'),
                  '/review-wordcloud.js': ('review-wordcloud.js', 'text/javascript'),
                  '/assets/steam-thumbsup.png': ('assets/steam-thumbsup.png', 'image/png'),
                  '/assets/steam-thumbsdown.png': ('assets/steam-thumbsdown.png', 'image/png'),
                  '/assets/review-wordcloud-preview.svg': ('assets/review-wordcloud-preview.svg', 'image/svg+xml'),
                  '/vendor/echarts/echarts.min.js': ('vendor/echarts/echarts.min.js', 'text/javascript')}
        if url.path in assets:
            name, content_type = assets[url.path]
            self.send_bytes((WEB / name).read_bytes(), content_type if content_type == 'image/png' else content_type + '; charset=utf-8')
            return
        if url.path == WORKER_PATH:
            try:
                self.json(self.worker_controller.status())
            except (ConfigError, OSError) as exc:
                self.log_error('采集后端状态读取失败: %s', exc)
                self.json({'error': '暂时无法读取采集后端状态'}, 503)
            return
        catalog = load_catalog(self.project_root)
        cover = re.fullmatch(r'/cover/(\d+)', url.path)
        if cover:
            appid = int(cover[1])
            candidates = sorted((self.project_root / 'data/games').glob(f'{appid}_*/header.*')) if appid in catalog else []
            for path in candidates:
                content_type = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                                '.webp': 'image/webp'}.get(path.suffix.lower())
                if content_type and path.is_file():
                    self.send_bytes(path.read_bytes(), content_type)
                    return
        match = re.fullmatch(r'/api/games/(\d+)', url.path)
        job_match = JOB_PATH.fullmatch(url.path)
        if url.path not in {'/api/games', '/api/games/compare', '/api/reviews', '/api/candidates',
                            '/api/review-jobs', '/api/review-wordcloud'} and not (match or job_match):
            self.json({'error': '找不到该页面或数据'}, 404)
            return
        try:
            included = overview_catalog(self.project_root, self.database, catalog)
            if url.path == '/api/review-wordcloud':
                clause, values = cloud_scope(parse_qs(url.query), included)
                self.json(wordclouds.get(self.database, clause, values))
                return
            if url.path == '/api/review-jobs':
                result = {'worker_online': self.worker_controller.status()['online'], 'items': [
                    {**job, 'in_overview': job['appid'] in included} for job in review_jobs.list_jobs(self.database)]}
                self.json(result)
                return
            with closing(self.open_database()) as db:
                if job_match:
                    result = job_progress(db, load_settings(self.project_root), review_jobs.get_job(self.database, job_match[1]), included)
                elif match:
                    appid = int(match[1])
                    if appid not in included:
                        self.json({'error': '该游戏尚未加入总览'}, 404)
                        return
                    result = game_detail(db, appid, included)
                elif url.path == '/api/games/compare':
                    requested = parse_qs(url.query, keep_blank_values=True).get('game', [])
                    if any(not re.fullmatch(r'[0-9]{1,10}', appid) for appid in requested):
                        raise ComparisonError('请选择有效的游戏 AppID')
                    result = compare_games(db, [int(appid) for appid in requested], included)
                elif url.path == '/api/games':
                    result = game_list(db, included)
                    settings = load_settings(self.project_root)
                    for game in result:
                        game['review_bytes'] = review_storage_bytes(settings.review_dir(game['appid']))
                elif url.path == '/api/candidates':
                    result = candidate_list(db, catalog, included, parse_qs(url.query))
                else:
                    result = reviews(db, parse_qs(url.query), included)
            self.json(result)
        except ConfigError as exc:
            self.json({'error': str(exc)}, 404)
        except ComparisonError as exc:
            self.json({'error': str(exc)}, 400)
        except (ValueError, OverflowError):
            self.json({'error': '筛选参数有误，请检查日期和选项'}, 400)
        except (sqlite3.Error, OSError) as exc:
            self.log_error('SQLite: %s', exc)
            self.json({'error': '无法读取评测数据库'}, 500)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--database', type=Path)
    args = parser.parse_args()
    Handler.project_root = args.root.resolve()
    settings = load_settings(Handler.project_root)
    Handler.database = (args.database or settings.paths.database).resolve()
    Handler.worker_controller = WorkerController(
        WorkerRuntime(settings.paths.tasks_dir, Handler.database), Handler.project_root,
        ROOT / 'scripts/review_worker.py', settings.paths.logs_dir / 'review-worker-service.log',
    )
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        print(f'http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

"""分段编排、落盘、入库、检查点、取消与恢复。

不混入提示词或聊天状态，可被命令行和未来工具适配层共用。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from ..collection_policy import CollectionPolicy
from ..contracts import (
    CollectionRequest,
    CollectionStatus,
    CollectionSummary,
    ConfigError,
    ReviewPageRequest,
    SegmentSpec,
    SegmentStatus,
    SteamError,
    StoreData,
)
from ..crawlers.client import HttpClient, build_client
from ..crawlers.images import download_cover
from ..crawlers.reviews import fetch_review_page
from ..crawlers.store import fetch_store_data, fetch_store_page_summary
from ..processing.normalize import normalize_review, to_jsonl_line
from ..settings import Settings
from ..storage import repository
from ..storage.database import connect, init_db, transaction
from ..storage.review_files import prepare_page, commit_page, recover_pending, read_records
from ..storage.lease import exclusive_collection
from .safety import CapacityGuard

logger = logging.getLogger(__name__)

FIELD_VERSION = "reviews-28-v1"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(tmp, path)



class ErrorSink:
    """异常记录追加写入 errors.jsonl。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def add(self, **payload: Any) -> None:
        payload.setdefault("logged_at", int(time.time()))
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.write("\n")
        self.count += 1


# --------------------------------------------------------------------------
# 采集主流程
# --------------------------------------------------------------------------


def _build_client_if_needed(client: HttpClient | None, settings: Settings) -> HttpClient:
    return client or build_client(settings)


def _build_conn_if_needed(conn: Any, settings: Settings) -> Any:
    if conn is not None:
        return conn
    settings.paths.ensure()
    new_conn = connect(settings.paths.database)
    init_db(new_conn)
    return new_conn


def _segment_status_from_row(row: dict[str, Any]) -> SegmentStatus:
    return SegmentStatus(
        appid=int(row["appid"]),
        segment_index=int(row["segment_index"]),
        start_ts=int(row["start_ts"]),
        end_ts=int(row["end_ts"]),
        status=row["status"],
        pages=int(row["pages"] or 0),
        cursor=row["cursor"],
        fetched=int(row["fetched"] or 0),
        inserted=int(row["inserted"] or 0),
        updated=int(row["updated"] or 0),
        errors=int(row["errors"] or 0),
        review_bytes=int(row.get("review_bytes") or 0),
        stop_reason=row.get("stop_reason"),
        finished_at=row["finished_at"],
    )


def _frozen_policy(settings, request, segments, policy):
    """恢复时验证研究口径，使用首次冻结额度；显式调整才更新记录。"""
    overrides = dict(policy.per_game_overrides)
    for appid, amount in (request.cli_budgets or {}).items():
        settings.ensure_appid(appid)
        overrides[appid] = amount
    policy = replace(policy, per_game_overrides=overrides)
    manifest_path = settings.task_dir(request.run_id) / 'manifest.json'
    if not manifest_path.exists():
        return policy
    saved = json.loads(manifest_path.read_text(encoding='utf-8'))
    window = saved.get('window', {})
    if (window.get('start_ts') != settings.window_start_ts
            or window.get('end_ts') != settings.window_end_ts):
        raise ConfigError('研究窗口已改变，请使用新任务ID，不能复用旧游标')
    current = json.loads(json.dumps({**settings.request.__dict__, 'param_style': request.param_style}))
    previous = saved.get('request')
    if previous != current or saved.get('appids') != list(request.appids):
        raise ConfigError('游戏清单或请求参数已改变，请使用新任务ID')
    frozen = saved.get('policy')
    if not frozen:
        raise ConfigError('旧任务缺少容量策略记录，请新建任务')
    if request.policy_override:
        saved.setdefault('policy_changes', []).append({'at': int(time.time()), 'explicit_policy': policy.to_manifest(tuple(request.appids))})
        saved['policy'] = policy.to_manifest(tuple(request.appids))
        write_json_atomic(manifest_path, saved)
        return policy
    values = {int(k): v['max_review_bytes'] for k, v in frozen.get('resolved_quotas', {}).items()}
    for appid, amount in (request.cli_budgets or {}).items():
        values[appid] = amount
    restored = CollectionPolicy(
        max_review_bytes_per_game=frozen['max_review_bytes_per_game'],
        min_free_disk_bytes=frozen['min_free_disk_bytes'],
        max_runtime_seconds=frozen.get('max_runtime_seconds'),
        per_game_overrides=values,
    )
    if request.cli_budgets:
        saved.setdefault('policy_changes', []).append({'at': int(time.time()), 'explicit_appid_budgets': dict(request.cli_budgets)})
        saved['policy'] = restored.to_manifest(tuple(request.appids))
        write_json_atomic(manifest_path, saved)
    return restored


@exclusive_collection
def collect_reviews(
    settings, request, *, client=None, conn=None, should_stop=None,
):
    return collect_reviews_locked(settings, request, client=client, conn=conn, should_stop=should_stop)


def collect_reviews_locked(settings, request, *, client=None, conn=None, should_stop=None):
    """在调用方已持有写锁时运行采集，并管理客户端与数据库连接。"""
    with ExitStack() as resources:
        if client is None:
            client = build_client(settings)
            resources.callback(client.close)
        if conn is None:
            conn = _build_conn_if_needed(None, settings)
            resources.callback(conn.close)
        return _collect_reviews(settings, request, client=client, conn=conn, should_stop=should_stop)


def _collect_reviews(
    settings: Settings,
    request: CollectionRequest,
    *,
    client: HttpClient | None = None,
    conn: Any = None,
    should_stop: Callable[[], bool] | None = None,
) -> CollectionSummary:
    """按游戏 × 时间段采集，落盘后入库，支持中断恢复。

    容量以**每款游戏正式评测 JSONL 的 UTF-8 字节**计量；整页放不下时整页拒绝，
    不推进游标、不写半行，记录停止原因后继续下一款游戏。
    """
    for appid in request.appids:
        settings.ensure_appid(appid)

    settings.task_dir(request.run_id)  # 在创建连接前验证任务ID
    segments = [
        s
        for s in settings.ordered_segments()
        if not request.segment_indexes or s.index in request.segment_indexes
    ]
    if not segments:
        raise ConfigError("没有匹配的时间段")
    policy = _frozen_policy(settings, request, segments, settings.require_policy())
    own_client = client is None
    own_conn = conn is None
    client = _build_client_if_needed(client, settings)
    conn = _build_conn_if_needed(conn, settings)

    tasks_dir = settings.task_dir(request.run_id)
    error_sink = ErrorSink(tasks_dir / "errors.jsonl")
    summary = CollectionSummary(
        run_id=request.run_id,
        status="running",
        output_dir=str(tasks_dir),
        started_at=int(time.time()),
    )

    # 全局正式文件共享：新任务也必须先恢复其他任务留下的未完成提交。
    for pending in settings.paths.tasks_dir.glob('*/pending_page.json'):
        previous_run = pending.parent.name
        recover_pending(settings, conn, previous_run)
        if previous_run != request.run_id:
            repository.finish_run(conn, previous_run, 'paused', 'commit_recovered')
    guard = CapacityGuard(settings, request.run_id, policy, cli_budgets=request.cli_budgets)
    guard.recount_all(request.appids)

    manifest = {
        "run_id": request.run_id,
        "field_version": FIELD_VERSION,
        "created_at": int(time.time()),
        "window": {
            "timezone": settings.timezone_name,
            "start": settings.window_start.isoformat(),
            "end": settings.window_end.isoformat(),
            "start_ts": settings.window_start_ts,
            "end_ts": settings.window_end_ts,
        },
        "segment_order": settings.segment_order,
        "segments": [s.to_dict() for s in segments],
        "appids": list(request.appids),
        "request": {**settings.request.__dict__, 'param_style': request.param_style},
        "store": settings.store.__dict__,
        "rate_limit": settings.rate_limit.__dict__,
        "limits": settings.limits.__dict__,
        "policy": policy.to_manifest(tuple(request.appids)),
        "note": request.note,
    }
    manifest_path = tasks_dir / "manifest.json"
    if manifest_path.is_file():
        # 恢复同一批次：保留首次冻结的参数，只补写本次实际执行的段落
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        previous.setdefault("resumed_at", int(time.time()))
        previous["last_segments"] = manifest["segments"]
        manifest = {**manifest, "policy": previous.get("policy", manifest["policy"])}
        write_json_atomic(manifest_path, {**previous, "policy": manifest["policy"]})
    else:
        write_json_atomic(manifest_path, manifest)

    repository.ensure_run(
        conn,
        request.run_id,
        {
            "appids": list(request.appids),
            "segment_indexes": list(request.segment_indexes or []),
            "param_style": request.param_style,
            "field_version": FIELD_VERSION,
            "window": manifest["window"],
            "request": manifest["request"],
            "policy": policy.to_manifest(tuple(request.appids)),
        },
        note=request.note,
    )

    external_stop = False

    def stop_requested() -> bool:
        nonlocal external_stop
        external_stop = external_stop or bool(should_stop and should_stop())
        return guard.should_stop_all() or external_stop

    execution_failed = False
    try:
        with transaction(conn):
            conn.execute("UPDATE crawl_runs SET status='running', finished_at=NULL, stop_reason=NULL WHERE run_id=?", (request.run_id,))
        for appid in request.appids:
            for segment in segments:
                if stop_requested():
                    break
                explicit_budget = bool((request.cli_budgets or {}).get(appid))
                blocked = guard.blocked_reason(appid)
                if not blocked and not explicit_budget and _game_already_capped(conn, request.run_id, appid):
                    blocked = 'capacity_limit'
                    guard.block_game(appid, blocked)
                if blocked:
                    status, delta_in, delta_up = _mark_segment_blocked(
                        settings, request, conn, appid, segment, blocked
                    )
                else:
                    status, delta_in, delta_up = _run_segment(
                        settings,
                        request,
                        client,
                        conn,
                        error_sink,
                        appid,
                        segment,
                        guard=guard,
                        should_stop=stop_requested,
                    )
                summary.segments.append(status)
                summary.inserted += delta_in
                summary.updated += delta_up
                summary.errors += status.errors
            if stop_requested():
                break
    except BaseException:
        execution_failed = True
        raise
    finally:
        used_total = sum(guard.used_bytes(appid) for appid in request.appids)
        summary.review_bytes = used_total
        # 从段检查点取绝对值，避免崩溃重放与多次恢复重复计账。
        rows = repository.list_segments(conn, request.run_id)
        summary.inserted = sum(int(r['inserted']) for r in rows)
        summary.updated = sum(int(r['updated']) for r in rows)
        with transaction(conn):
            conn.execute('UPDATE crawl_runs SET inserted=?,updated=?,errors=?,review_bytes=? WHERE run_id=?',
                         (summary.inserted, summary.updated, sum(int(r['errors']) for r in rows), used_total, request.run_id))
        summary.finished_at = int(time.time())
        selected_indexes = {segment.index for segment in segments}
        selected_rows = [row for row in rows if row['segment_index'] in selected_indexes]
        states = {row['status'] for row in selected_rows}
        reasons = {row['stop_reason'] for row in selected_rows if row['stop_reason']}
        expected = len(request.appids) * len(segments)
        if execution_failed:
            summary.status = 'failed'
            summary.stop_reason = 'interrupted_commit' if (tasks_dir/'pending_page.json').exists() else 'execution_error'
        elif guard.environment_reason:
            summary.status = "paused"
            summary.stop_reason = guard.environment_reason
        elif "capacity_limit" in reasons:
            summary.status = "partial"
            summary.stop_reason = "capacity_limit"
        elif states == {"completed"} and len(selected_rows) == expected:
            summary.status = "completed"
            summary.completed = True
        elif external_stop or "paused" in states:
            summary.status = "paused"
            summary.stop_reason = 'stopped' if external_stop else next(
                (row['stop_reason'] for row in selected_rows if row['status'] == 'paused'), None)
        elif "failed" in states and "completed" not in states:
            summary.status = "failed"
        elif states:
            summary.status = "partial"
        else:
            summary.status = "failed"
        repository.finish_run(conn, request.run_id, summary.status, summary.stop_reason)
        summary.capacity = guard.snapshot(request.appids)
        summary.checkpoint = {
            "stop_reason": summary.stop_reason,
            "environment_reason": guard.environment_reason,
            "resume_with": "collect_reviews 同一 run_id（额度不清零）",
            "segments": {
                f"{s.appid}:{s.segment_index}": {
                    "status": s.status,
                    "cursor": s.cursor,
                    "pages": s.pages,
                    "review_bytes": s.review_bytes,
                    "stop_reason": s.stop_reason,
                }
                for s in summary.segments
                if s.status != "completed"
            },
        }
        write_json_atomic(tasks_dir / "summary.json", summary.to_dict())
        if own_client:
            client.close()
        if own_conn:
            conn.close()

    return summary



def segment_bytes_from_files(settings: Settings, appid: int, segment_start: int) -> int:
    """当前游戏月份文件的实际字节；任务ID不决定正式数据路径。"""
    month = datetime.fromtimestamp(segment_start, settings.tz).strftime('%Y-%m')
    return sum(p.stat().st_size for p in (settings.review_dir(appid)/month).glob('part_*.jsonl'))


def _game_already_capped(conn: Any, run_id: str, appid: int) -> bool:
    """该批次中此游戏是否已因容量停止；恢复时不再浪费请求。

    额度已冻结进 manifest；如需提高额度必须显式新建批次或传 --appid-budget。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM crawl_segments "
        "WHERE run_id = ? AND appid = ? AND stop_reason = 'capacity_limit'",
        (run_id, appid),
    ).fetchone()
    return bool(row and int(row["n"]) > 0)


def _mark_segment_blocked(
    settings: Settings,
    request: CollectionRequest,
    conn: Any,
    appid: int,
    segment: SegmentSpec,
    reason: str,
) -> tuple[SegmentStatus, int, int]:
    """该游戏已触达容量上限：记录段落状态，不做任何网络请求。"""
    existing = repository.get_segment(conn, request.run_id, appid, segment.index)
    if existing and existing["status"] == "completed":
        return _segment_status_from_row(existing), 0, 0
    pages = int(existing["pages"] or 0) if existing else 0
    cursor = existing["cursor"] if existing else None
    fetched = int(existing["fetched"] or 0) if existing else 0
    inserted = int(existing["inserted"] or 0) if existing else 0
    updated = int(existing["updated"] or 0) if existing else 0
    errors = int(existing["errors"] or 0) if existing else 0
    review_bytes = int(existing["review_bytes"] or 0) if existing else 0
    repository.upsert_segment(
        conn,
        run_id=request.run_id,
        appid=appid,
        segment_index=segment.index,
        start_ts=segment.start_ts,
        end_ts=segment.end_ts,
        status="partial",
        pages=pages,
        cursor=cursor,
        fetched=fetched,
        inserted=inserted,
        updated=updated,
        errors=errors,
        review_bytes=review_bytes,
        stop_reason=reason,
    )
    return (
        SegmentStatus(
            appid=appid,
            segment_index=segment.index,
            start_ts=segment.start_ts,
            end_ts=segment.end_ts,
            status="partial",
            pages=pages,
            cursor=cursor,
            fetched=fetched,
            inserted=inserted,
            updated=updated,
            errors=errors,
            review_bytes=review_bytes,
            stop_reason=reason,
            finished_at=int(time.time()),
        ),
        0,
        0,
    )

def _run_segment(
    settings: Settings,
    request: CollectionRequest,
    client: HttpClient,
    conn: Any,
    error_sink: ErrorSink,
    appid: int,
    segment: SegmentSpec,
    *,
    guard: CapacityGuard,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[SegmentStatus, int, int]:
    """返回 (段落状态, 本次新增条数, 本次更新条数)。

    增量只统计本次调用真正写入的部分，避免恢复时把历史计数重复计入任务账本。
    """
    existing = repository.get_segment(conn, request.run_id, appid, segment.index)
    if existing and existing["status"] == "completed":
        return _segment_status_from_row(existing), 0, 0

    pages = int(existing["pages"] or 0) if existing else 0
    cursor = existing["cursor"] if existing else None
    fetched = int(existing["fetched"] or 0) if existing else 0
    inserted = int(existing["inserted"] or 0) if existing else 0
    updated = int(existing["updated"] or 0) if existing else 0
    errors = int(existing["errors"] or 0) if existing else 0
    segment_bytes = int(existing["review_bytes"] or 0) if existing else 0
    delta_inserted = 0
    delta_updated = 0

    max_pages = request.max_pages_per_segment or settings.limits.max_pages_per_segment
    stop_reason: str | None = None
    status = "running"
    repository.upsert_segment(
        conn,
        run_id=request.run_id,
        appid=appid,
        segment_index=segment.index,
        start_ts=segment.start_ts,
        end_ts=segment.end_ts,
        status=status,
        pages=pages,
        cursor=cursor,
        fetched=fetched,
        inserted=inserted,
        updated=updated,
        errors=errors,
        review_bytes=segment_bytes,
    )
    logger.info('%s 开始采集，第 %d 页', segment.start.strftime('%Y-%m'), pages + 1)

    seen_cursors: set[str] = set()

    while True:
        if should_stop and should_stop():
            status = "paused"
            stop_reason = guard.environment_reason or "stopped"
            break
        if guard.blocked_reason(appid):
            status = "partial"
            stop_reason = guard.blocked_reason(appid)
            break
        if guard.remaining_bytes(appid) <= 0:
            guard.block_game(appid, 'capacity_limit')
            status, stop_reason = 'partial', 'capacity_limit'
            break
        if max_pages and pages >= max_pages:
            status = "partial"
            stop_reason = "max_pages_per_segment"
            break

        page_request = ReviewPageRequest(
            appid=appid,
            segment=segment,
            cursor=cursor,
            num_per_page=settings.request.num_per_page,
            languages=settings.request.languages,
            review_filter=settings.request.review_filter,
            review_type=settings.request.review_type,
            purchase_type=settings.request.purchase_type,
            filter_offtopic_activity=settings.request.filter_offtopic_activity,
            param_style=request.param_style,
        )

        try:
            page = fetch_review_page(client, page_request)
        except SteamError as exc:
            error_sink.add(
                run_id=request.run_id,
                appid=appid,
                segment=segment.label(),
                page=pages + 1,
                cursor=cursor,
                scope="page_request",
                error=exc.to_dict(),
            )
            status = "failed"
            stop_reason = exc.code
            break

        if page.is_terminal:
            status = "completed"
            if page.summary.total_matching is not None:
                repository.insert_summary_snapshot(
                    conn,
                    appid=appid,
                    source="api",
                    scope="query",
                    query_params=page_request.input_json_body(),
                    summary=page.summary,
                    collected_at=page.fetched_at,
                    crawl_run_id=request.run_id,
                )
            break

        # ---- 28 字段映射与校验 -------------------------------------
        records: list[dict[str, Any]] = []
        page_errors: list[dict[str, Any]] = []
        for raw in page.raw_reviews:
            record, field_errors = normalize_review(
                raw,
                appid=appid,
                run_id=request.run_id,
                collected_at=page.fetched_at,
                window_start=segment.start_ts,
                window_end=segment.end_ts,
                expected_language=settings.request.languages[0],
            )
            if record is None:
                for err in field_errors:
                    page_errors.append(
                        {
                            "review_id": str(raw.get("recommendationid")),
                            "field": err.field,
                            "reason": err.reason,
                            "value": err.value,
                        }
                    )
                continue
            for err in field_errors:
                page_errors.append(
                    {
                        "review_id": record["review_id"],
                        "field": err.field,
                        "reason": err.reason,
                        "value": err.value,
                    }
                )
            records.append(record)

        # 整页先规划：已有ID更新原分片，新ID按发表月份保存；不重复占空间。
        plan = prepare_page(settings, conn, appid, records)
        if records and not guard.can_commit(appid, max(0, plan['delta_bytes'])):
            guard.block_game(appid, "capacity_limit")
            status, stop_reason = "partial", "capacity_limit"
            break

        next_cursor = page.next_cursor
        if next_cursor and (next_cursor == cursor or next_cursor in seen_cursors):
            status, stop_reason = "paused", "cursor_repeated"
            break
        if records and not next_cursor:
            # 无有效游标的非空页不证明结束，不提交无法安全续传的页面。
            status, stop_reason = "failed", "missing_cursor"
            break

        state = dict(run_id=request.run_id, appid=appid, segment_index=segment.index,
                     start_ts=segment.start_ts, end_ts=segment.end_ts, status="running",
                     pages=pages+1, cursor=next_cursor, fetched=fetched+len(page.raw_reviews),
                     inserted=inserted, updated=updated, errors=errors+len(page_errors),
                     review_bytes=segment_bytes+plan['delta_bytes'],
                     param_fingerprint=page_request.fingerprint())
        counts = commit_page(settings, conn, request.run_id, plan, state)
        write_json_atomic(settings.task_dir(request.run_id)/'checkpoint.json',
                          {'run_id': request.run_id, 'segments': repository.list_segments(conn, request.run_id)})
        guard.recount(appid)
        segment_bytes += plan['delta_bytes']
        pages += 1
        inserted += counts['inserted']
        updated += counts['updated']
        logger.info('%s 第 %d 页已提交，本页新增 %d 条，更新 %d 条',
                    segment.start.strftime('%Y-%m'), pages, counts['inserted'], counts['updated'])
        delta_inserted += counts['inserted']
        delta_updated += counts['updated']
        errors += len(page_errors)
        fetched += len(page.raw_reviews)
        for item in page_errors:
            error_sink.add(run_id=request.run_id, appid=appid, segment=segment.label(),
                           page=pages, cursor=cursor, scope="record", **item)
        if pages == 1:
            repository.insert_summary_snapshot(conn, appid=appid, source="api", scope="query",
                query_params=page_request.input_json_body(), summary=page.summary,
                collected_at=page.fetched_at, crawl_run_id=request.run_id)
        cursor = next_cursor
        if cursor:
            seen_cursors.add(cursor)

    review_bytes = segment_bytes
    repository.upsert_segment(
        conn,
        run_id=request.run_id,
        appid=appid,
        segment_index=segment.index,
        start_ts=segment.start_ts,
        end_ts=segment.end_ts,
        status=status,
        pages=pages,
        cursor=cursor,
        fetched=fetched,
        inserted=inserted,
        updated=updated,
        errors=errors,
        review_bytes=review_bytes,
        stop_reason=stop_reason,
        finished=status in ("completed", "partial", "failed"),
    )
    logger.info('%s 分段结束：%s', segment.start.strftime('%Y-%m'), stop_reason or status)

    return (
        SegmentStatus(
            appid=appid,
            segment_index=segment.index,
            start_ts=segment.start_ts,
            end_ts=segment.end_ts,
            status=status,
            pages=pages,
            cursor=cursor,
            fetched=fetched,
            inserted=inserted,
            updated=updated,
            errors=errors,
            review_bytes=review_bytes,
            stop_reason=stop_reason,
            finished_at=int(time.time()),
        ),
        delta_inserted,
        delta_updated,
    )


# --------------------------------------------------------------------------
# 商店资料与图片
# --------------------------------------------------------------------------


@exclusive_collection
def collect_store(
    settings: Settings,
    run_id: str,
    appids: Sequence[int] | None = None,
    *,
    client: HttpClient | None = None,
    conn: Any = None,
    with_cover: bool = True,
) -> dict[str, Any]:
    """采集基础资料、价格快照、页面评价口径与封面。"""
    own_client = client is None
    own_conn = conn is None
    client = _build_client_if_needed(client, settings)
    conn = _build_conn_if_needed(conn, settings)

    targets = list(appids or settings.appids)
    for appid in targets:
        settings.ensure_appid(appid)

    tasks_dir = settings.task_dir(run_id)
    errors = ErrorSink(tasks_dir / "errors.jsonl")

    results: dict[str, Any] = {"run_id": run_id, "games": [], "errors": 0}
    for index, appid in enumerate(targets, 1):
        entry: dict[str, Any] = {"appid": appid}
        try:
            data: StoreData = fetch_store_data(
                client,
                appid,
                region=settings.store.region,
                language=settings.store.api_language,
                store_url=settings.game(appid).store_url,
                run_id=run_id,
            )
            cover = None
            if with_cover:
                cover = download_cover(
                    client,
                    appid,
                    data.header_image_url,
                    images_dir=settings.paths.images_dir,
                    target_dir=settings.game_dir(appid),
                )
            repository.upsert_game(conn, data, cover_local_path=cover.local_path if cover else None)
            if data.price:
                repository.insert_price_snapshot(conn, data.price)
            entry.update(
                {
                    "status": "ok" if data.available else "unavailable",
                    "name": data.name,
                    "available": data.available,
                    "note": data.note,
                    "price": data.price.to_dict() if data.price else None,
                    "cover": cover.to_dict() if cover else None,
                }
            )

            summary = fetch_store_page_summary(
                client,
                appid,
                language=settings.store.language,
                collected_at=data.collected_at,
                mature_cookies=settings.store.mature_cookies,
            )
            for scope in summary.scopes():
                repository.insert_summary_snapshot(
                    conn,
                    appid=appid,
                    source="store_page",
                    scope=scope["scope"],
                    query_params={"region": settings.store.region, "language": settings.store.language},
                    summary=None,
                    total_reviews=scope["total_reviews"],
                    positive_percent_display=scope["positive_percent_display"],
                    review_score_desc=scope["review_score_desc"],
                    collected_at=summary.collected_at,
                    crawl_run_id=run_id,
                )
            entry["page_summary"] = {
                "overall_label": summary.overall_label,
                "overall_total": summary.overall_total,
                "overall_percent": summary.overall_percent,
                "recent_label": summary.recent_label,
                "recent_total": summary.recent_total,
                "recent_percent": summary.recent_percent,
                "language_label": "简体中文评测",
                "language_total": summary.language_total,
                "language_percent": summary.language_percent,
            }
            game_dir = settings.game_dir(appid)
            write_json_atomic(game_dir / 'info.json', {**data.to_dict(), 'page_summary': entry.get('page_summary')})
            snapshots = game_dir / 'snapshots.jsonl'
            snapshot = {'collected_at': data.collected_at, 'price': entry['price'], 'page_summary': entry.get('page_summary')}
            with snapshots.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(snapshot, ensure_ascii=False)+'\n')
        except SteamError as exc:
            errors.add(
                run_id=run_id,
                appid=appid,
                scope="store",
                error=exc.to_dict(),
            )
            entry.update({"status": "failed", "error": exc.to_dict()})

        results["games"].append(entry)
        logger.info('商店采集 %s/%s AppID=%s %s', index, len(targets), appid, entry['status'])

    results["errors"] = errors.count
    write_json_atomic(tasks_dir / "summary.json", results)
    if own_client:
        client.close()
    if own_conn:
        conn.close()
    return results


# --------------------------------------------------------------------------
# 状态查询
# --------------------------------------------------------------------------


def get_collection_status(
    settings: Settings, run_id: str, *, conn: Any = None
) -> CollectionStatus:
    own_conn = conn is None
    conn = conn if conn is not None else connect(settings.paths.database, readonly=True)
    run = repository.get_run(conn, run_id)
    if run is None:
        if own_conn:
            conn.close()
        from ..contracts import NotFound

        raise NotFound(f"任务不存在: {run_id}")
    segments = repository.list_segments(conn, run_id)
    totals = {
        "segments": len(segments),
        "status": repository.segment_counts(conn, run_id),
        "pages": sum(int(s["pages"] or 0) for s in segments),
        "fetched": sum(int(s["fetched"] or 0) for s in segments),
        "inserted": sum(int(s["inserted"] or 0) for s in segments),
        "updated": sum(int(s["updated"] or 0) for s in segments),
        "errors": sum(int(s["errors"] or 0) for s in segments),
        "review_bytes": sum(int(s["review_bytes"] or 0) for s in segments),
        "reviews_in_db": repository.count_reviews(conn),
        "stop_reasons": sorted({s["stop_reason"] for s in segments if s.get("stop_reason")}),
    }
    policy = settings.require_policy()
    manifest_file = settings.task_dir(run_id)/'manifest.json'
    frozen = json.loads(manifest_file.read_text(encoding='utf-8')).get('policy', {}) if manifest_file.exists() else {}
    resolved = frozen.get('resolved_quotas', {})
    params = json.loads(run['params'])
    window = params.get('window', {})
    historical = settings
    if window.get('start_ts') is not None and window.get('end_ts') is not None:
        historical = replace(settings, window_start=datetime.fromtimestamp(window['start_ts'], settings.tz),
                             window_end=datetime.fromtimestamp(window['end_ts'], settings.tz))
    expected_segments = len(params.get('segment_indexes') or historical.segments())
    per_game: dict[str, Any] = {}
    for row in segments:
        key = str(row["appid"])
        entry = per_game.setdefault(
            key,
            {
                "appid": int(row["appid"]),
                "used_bytes": 0,
                "quota_bytes": resolved.get(key, {}).get("max_review_bytes", policy.quota_for(int(row["appid"])).max_review_bytes),
                "stop_reasons": set(),
                "statuses": set(),
                "covered_start_ts": None,
                "covered_end_ts": None,
            },
        )
        measured = segment_bytes_from_files(historical, int(row["appid"]), int(row["start_ts"]))
        entry["used_bytes"] += measured if measured else int(row["review_bytes"] or 0)
        entry["statuses"].add(row["status"])
        if row.get("stop_reason"):
            entry["stop_reasons"].add(row["stop_reason"])
        bounds = conn.execute('SELECT MIN(created_at), MAX(created_at) FROM reviews WHERE appid=? AND created_at>=? AND created_at<?',
                              (row['appid'], row['start_ts'], row['end_ts'])).fetchone()
        if bounds[0] is not None:
            entry['covered_start_ts'] = min(entry['covered_start_ts'] or bounds[0], bounds[0])
            entry['covered_end_ts'] = max(entry['covered_end_ts'] or bounds[1], bounds[1])
    for entry in per_game.values():
        entry['used_bytes'] = sum(p.stat().st_size for p in settings.review_dir(entry['appid']).glob('*/*.jsonl'))
        entry['coverage_scope'] = 'saved_game_reviews'
        entry["remaining_bytes"] = max(0, entry["quota_bytes"] - entry["used_bytes"])
        entry["stop_reasons"] = sorted(entry["stop_reasons"])
        entry["statuses"] = sorted(entry["statuses"])
        entry["complete"] = (entry["statuses"] == ["completed"] and sum(int(r["appid"]) == entry["appid"] for r in segments) == expected_segments)
    # 段落列在历史批次里存的是累计值，总量一律以按文件实测的每款用量求和
    totals["review_bytes"] = sum(int(e["used_bytes"]) for e in per_game.values())
    totals["capacity"] = {
        "measurement": policy.measurement,
        "unit": "bytes",
        "games": per_game,
    }
    status = CollectionStatus(
        run_id=run_id,
        status=run["status"],
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        params=json.loads(run["params"]),
        segments=segments,
        totals=totals,
    )
    if own_conn:
        conn.close()
    return status

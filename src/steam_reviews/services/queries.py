"""稳定查询入口：游戏资料、评测分页与统计。后续可原样封装为只读工具。"""

from __future__ import annotations

import json
import time
from typing import Any

from ..analysis import statistics as stats_mod
from ..contracts import (
    GameDetails,
    NotFound,
    ReviewList,
    ReviewQuery,
    ReviewStatistics,
    StatisticsQuery,
)
from ..settings import Settings
from ..storage import repository
from ..storage.database import connect, init_db


def _conn_or_open(settings: Settings, conn: Any) -> tuple[Any, bool]:
    if conn is not None:
        return conn, False
    settings.paths.ensure()
    new_conn = connect(settings.paths.database)
    init_db(new_conn)
    return new_conn, True


def get_game_details(settings: Settings, appid: int, *, conn: Any = None) -> GameDetails:
    settings.ensure_appid(appid)
    conn, own = _conn_or_open(settings, conn)
    try:
        row = repository.get_game_row(conn, appid)
        if row is None:
            raise NotFound(f"数据库中没有该游戏的资料: {appid}")
        price = repository.latest_price(conn, appid)
        total = repository.count_reviews(conn, appid=appid)
        return GameDetails(
            appid=int(row["appid"]),
            name=row["name"],
            store_url=row["store_url"],
            developers=json.loads(row["developers"] or "[]"),
            publishers=json.loads(row["publishers"] or "[]"),
            genres=json.loads(row["genres"] or "[]"),
            release_date=row["release_date"],
            coming_soon=None if row["coming_soon"] is None else bool(row["coming_soon"]),
            header_image_url=row["header_image_url"],
            cover_local_path=row["cover_local_path"],
            collected_at=row["collected_at"],
            latest_price=price,
            review_total=total,
        )
    finally:
        if own:
            conn.close()


def list_reviews(settings: Settings, request: ReviewQuery, *, conn: Any = None) -> ReviewList:
    request.validate()
    conn, own = _conn_or_open(settings, conn)
    try:
        total, items = repository.select_reviews(conn, request)
        return ReviewList(
            filters={
                "appid": request.appid,
                "start_ts": request.start_ts,
                "end_ts": request.end_ts,
                "order": request.order,
            },
            observed_at=int(time.time()),
            total=total,
            items=items,
            limit=request.limit,
            offset=request.offset,
        )
    finally:
        if own:
            conn.close()


def get_review_statistics(
    settings: Settings, request: StatisticsQuery, *, conn: Any = None
) -> ReviewStatistics:
    request.validate()
    conn, own = _conn_or_open(settings, conn)
    try:
        payload = stats_mod.compute(conn, request)
        return ReviewStatistics(
            filters=payload["filters"],
            observed_at=payload["observed_at"],
            totals=payload["totals"],
            by_game=payload["by_game"],
            by_bucket=payload["by_bucket"],
            by_playtime=payload["by_playtime"],
            missing=payload["missing"],
        )
    finally:
        if own:
            conn.close()

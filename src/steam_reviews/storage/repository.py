"""幂等写入、任务进度与查询。只使用参数化 SQL。"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterable, Mapping, Sequence

from ..contracts import (
    BOOL_FIELDS,
    REVIEW_FIELD_KEYS,
    PriceSnapshot,
    QuerySummary,
    ReviewQuery,
    StorageFailure,
    StoreData,
)
from .database import transaction

REVIEW_DB_COLUMNS: tuple[str, ...] = REVIEW_FIELD_KEYS + ("first_seen_at", "last_seen_at")

_INSERT_REVIEW_SQL = (
    f"INSERT INTO reviews ({', '.join(REVIEW_DB_COLUMNS)}) "
    f"VALUES ({', '.join(['?'] * len(REVIEW_DB_COLUMNS))})"
)

_UPDATE_REVIEW_SQL = (
    "UPDATE reviews SET "
    + ", ".join(f"{col} = ?" for col in REVIEW_FIELD_KEYS if col != "review_id")
    + ", first_seen_at = ?, last_seen_at = ? WHERE review_id = ?"
)


def _now() -> int:
    return int(time.time())


def _to_db(record: Mapping[str, Any]) -> list[Any]:
    """记录 → 数据库值：布尔转 0/1，未知保持 NULL。"""
    values: list[Any] = []
    for key in REVIEW_FIELD_KEYS:
        value = record.get(key)
        if key in BOOL_FIELDS and value is not None:
            value = 1 if value else 0
        values.append(value)
    return values


def _from_db(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()} if isinstance(row, sqlite3.Row) else dict(row)
    for key in BOOL_FIELDS:
        if key in data and data[key] is not None:
            data[key] = bool(data[key])
    return data


# --------------------------------------------------------------------------
# 评测
# --------------------------------------------------------------------------


def upsert_review(conn: sqlite3.Connection, record: Mapping[str, Any]) -> str:
    """按 review_id 幂等写入。

    返回 ``inserted`` / ``updated`` / ``unchanged`` / ``conflict``。
    ``conflict`` 表示同一 ID 的 appid 与库内不同，调用方应记为异常。
    """
    review_id = record["review_id"]
    appid = int(record["appid"])
    collected_at = int(record["collected_at"])

    row = conn.execute(
        "SELECT appid, first_seen_at, last_seen_at FROM reviews WHERE review_id = ?",
        (review_id,),
    ).fetchone()

    if row is None:
        values = _to_db(record) + [collected_at, collected_at]
        conn.execute(_INSERT_REVIEW_SQL, values)
        return "inserted"

    if int(row["appid"]) != appid:
        return "conflict"

    first_seen = min(int(row["first_seen_at"] or collected_at), collected_at)
    last_seen = max(int(row["last_seen_at"] or collected_at), collected_at)

    if collected_at >= int(row["last_seen_at"] or 0):
        values = [v for k, v in zip(REVIEW_FIELD_KEYS, _to_db(record)) if k != "review_id"]
        values += [first_seen, last_seen, review_id]
        conn.execute(_UPDATE_REVIEW_SQL, values)
        return "updated"

    conn.execute(
        "UPDATE reviews SET first_seen_at = ? WHERE review_id = ? AND first_seen_at > ?",
        (first_seen, review_id, first_seen),
    )
    return "unchanged"


def bulk_upsert_reviews(
    conn: sqlite3.Connection, records: Sequence[Mapping[str, Any]]
) -> dict[str, int]:
    """一页记录一次性入库；整页成功或整页回滚。"""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "conflict": 0}
    try:
        with transaction(conn):
            for record in records:
                outcome = upsert_review(conn, record)
                counts[outcome] += 1
    except sqlite3.Error as exc:
        raise StorageFailure("评测入库失败", detail={"error": str(exc)}) from exc
    return counts


def count_reviews(
    conn: sqlite3.Connection,
    *,
    appid: int | None = None,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> int:
    sql = "SELECT COUNT(*) AS n FROM reviews WHERE 1 = 1"
    params: list[Any] = []
    if appid is not None:
        sql += " AND appid = ?"
        params.append(appid)
    if start_ts is not None:
        sql += " AND created_at >= ?"
        params.append(start_ts)
    if end_ts is not None:
        sql += " AND created_at < ?"
        params.append(end_ts)
    return int(conn.execute(sql, params).fetchone()["n"])


def select_reviews(conn: sqlite3.Connection, query: ReviewQuery) -> tuple[int, list[dict[str, Any]]]:
    query.validate()
    where = " WHERE 1 = 1"
    params: list[Any] = []
    if query.appid is not None:
        where += " AND appid = ?"
        params.append(query.appid)
    if query.start_ts is not None:
        where += " AND created_at >= ?"
        params.append(query.start_ts)
    if query.end_ts is not None:
        where += " AND created_at < ?"
        params.append(query.end_ts)

    total = int(conn.execute(f"SELECT COUNT(*) AS n FROM reviews{where}", params).fetchone()["n"])
    order = (
        "created_at DESC, review_id DESC"
        if query.order == "created_at_desc"
        else "created_at ASC, review_id ASC"
    )
    rows = conn.execute(
        f"SELECT * FROM reviews{where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [query.limit, query.offset],
    ).fetchall()
    return total, [_from_db(row) for row in rows]


def distinct_review_ids(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(DISTINCT review_id) AS n FROM reviews").fetchone()["n"])


# --------------------------------------------------------------------------
# 游戏资料与快照
# --------------------------------------------------------------------------


def upsert_game(
    conn: sqlite3.Connection,
    data: StoreData,
    *,
    cover_local_path: str | None = None,
) -> str:
    now = data.collected_at
    row = conn.execute("SELECT appid, last_seen_at FROM games WHERE appid = ?", (data.appid,)).fetchone()
    payload = (
        data.name,
        data.store_url,
        data.short_description,
        json.dumps(data.developers, ensure_ascii=False),
        json.dumps(data.publishers, ensure_ascii=False),
        json.dumps(data.genres, ensure_ascii=False),
        json.dumps(data.categories, ensure_ascii=False),
        data.release_date_raw,
        data.release_date,
        None if data.coming_soon is None else int(bool(data.coming_soon)),
        json.dumps(data.platforms, ensure_ascii=False),
        data.supported_languages,
        data.header_image_url,
        cover_local_path,
        now,
    )
    with transaction(conn):
        if row is None:
            conn.execute(
                """
                INSERT INTO games (
                    appid, name, store_url, short_description, developers, publishers,
                    genres, categories, release_date_raw, release_date, coming_soon,
                    platforms, supported_languages, header_image_url, cover_local_path,
                    collected_at, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (data.appid, *payload, now, now),
            )
            return "inserted"
        conn.execute(
            """
            UPDATE games SET name = ?, store_url = ?, short_description = ?, developers = ?,
                publishers = ?, genres = ?, categories = ?, release_date_raw = ?,
                release_date = ?, coming_soon = ?, platforms = ?, supported_languages = ?,
                header_image_url = ?, cover_local_path = ?, collected_at = ?,
                last_seen_at = MAX(COALESCE(last_seen_at, 0), ?)
            WHERE appid = ?
            """,
            (*payload, now, data.appid),
        )
        return "updated"


def insert_price_snapshot(conn: sqlite3.Connection, snapshot: PriceSnapshot) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO game_snapshots (
                appid, is_free, price_initial, price_final, discount_percent,
                currency, region, collected_at, crawl_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.appid,
                None if snapshot.is_free is None else int(bool(snapshot.is_free)),
                snapshot.price_initial,
                snapshot.price_final,
                snapshot.discount_percent,
                snapshot.currency,
                snapshot.region,
                snapshot.collected_at,
                snapshot.crawl_run_id,
            ),
        )


def insert_summary_snapshot(
    conn: sqlite3.Connection,
    *,
    appid: int,
    source: str,
    scope: str,
    query_params: Mapping[str, Any] | None,
    summary: QuerySummary | None,
    collected_at: int,
    crawl_run_id: str,
    total_positive: int | None = None,
    total_negative: int | None = None,
    total_reviews: int | None = None,
    positive_percent_display: float | None = None,
    review_score_desc: str | None = None,
) -> None:
    if summary is not None:
        total_positive = summary.total_positive
        total_negative = summary.total_negative
        total_reviews = summary.total_reviews
        review_score_desc = summary.review_score_desc
    with transaction(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO review_summary_snapshots (
                appid, source, scope, query_params, total_positive, total_negative,
                total_reviews, positive_percent_display, review_score_desc,
                collected_at, crawl_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                appid,
                source,
                scope,
                json.dumps(query_params, ensure_ascii=False, sort_keys=True) if query_params else None,
                total_positive,
                total_negative,
                total_reviews,
                positive_percent_display,
                review_score_desc,
                collected_at,
                crawl_run_id,
            ),
        )


def latest_price(conn: sqlite3.Connection, appid: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT appid, is_free, price_initial, price_final, discount_percent, currency,
               region, collected_at, crawl_run_id
        FROM game_snapshots WHERE appid = ? ORDER BY collected_at DESC, snapshot_id DESC LIMIT 1
        """,
        (appid,),
    ).fetchone()
    return dict(row) if row else None


def get_game_row(conn: sqlite3.Connection, appid: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM games WHERE appid = ?", (appid,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# 任务与进度
# --------------------------------------------------------------------------


def ensure_run(
    conn: sqlite3.Connection, run_id: str, params: Mapping[str, Any], *, note: str | None = None
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM crawl_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row:
        return dict(row)
    with transaction(conn):
        conn.execute(
            "INSERT INTO crawl_runs (run_id, status, params, note, started_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, "running", json.dumps(params, ensure_ascii=False, sort_keys=True), note, _now()),
        )
    return dict(conn.execute("SELECT * FROM crawl_runs WHERE run_id = ?", (run_id,)).fetchone())


def add_run_totals(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    inserted: int = 0,
    updated: int = 0,
    errors: int = 0,
    review_bytes: int | None = None,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE crawl_runs SET inserted = inserted + ?, updated = updated + ?, "
            "errors = errors + ? WHERE run_id = ?",
            (inserted, updated, errors, run_id),
        )
        if review_bytes is not None:
            # 字节是绝对值（按文件实测），恢复时不能累加
            conn.execute(
                "UPDATE crawl_runs SET review_bytes = ? WHERE run_id = ?",
                (int(review_bytes), run_id),
            )


def finish_run(
    conn: sqlite3.Connection, run_id: str, status: str, stop_reason: str | None = None
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE crawl_runs SET status = ?, finished_at = ?, "
            "stop_reason = COALESCE(?, stop_reason) WHERE run_id = ?",
            (status, _now(), stop_reason, run_id),
        )


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM crawl_runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def upsert_segment(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    appid: int,
    segment_index: int,
    start_ts: int,
    end_ts: int,
    status: str,
    pages: int,
    cursor: str | None,
    fetched: int,
    inserted: int,
    updated: int,
    errors: int,
    review_bytes: int = 0,
    stop_reason: str | None = None,
    param_fingerprint: str | None = None,
    finished: bool = False,
) -> None:
    now = _now()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO crawl_segments (
                run_id, appid, segment_index, start_ts, end_ts, status, pages, cursor,
                fetched, inserted, updated, errors, review_bytes, stop_reason,
                param_fingerprint, updated_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, appid, segment_index) DO UPDATE SET
                status = excluded.status,
                pages = excluded.pages,
                cursor = excluded.cursor,
                fetched = excluded.fetched,
                inserted = excluded.inserted,
                updated = excluded.updated,
                errors = excluded.errors,
                review_bytes = excluded.review_bytes,
                stop_reason = excluded.stop_reason,
                param_fingerprint = COALESCE(excluded.param_fingerprint, crawl_segments.param_fingerprint),
                updated_at = excluded.updated_at,
                finished_at = excluded.finished_at
            """,
            (
                run_id,
                appid,
                segment_index,
                start_ts,
                end_ts,
                status,
                pages,
                cursor,
                fetched,
                inserted,
                updated,
                errors,
                review_bytes,
                stop_reason,
                param_fingerprint,
                now,
                now if finished else None,
            ),
        )


def get_segment(
    conn: sqlite3.Connection, run_id: str, appid: int, segment_index: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM crawl_segments WHERE run_id = ? AND appid = ? AND segment_index = ?",
        (run_id, appid, segment_index),
    ).fetchone()
    return dict(row) if row else None


def list_segments(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM crawl_segments WHERE run_id = ? ORDER BY appid, segment_index",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def start_import(conn: sqlite3.Connection, import_id: str, source_path: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO import_runs (import_id, source_path, status, started_at) VALUES (?, ?, 'running', ?)",
            (import_id, source_path, _now()),
        )


def finish_import(
    conn: sqlite3.Connection,
    import_id: str,
    *,
    status: str,
    total: int,
    inserted: int,
    updated: int,
    unchanged: int,
    errors: int,
) -> None:
    with transaction(conn):
        conn.execute(
            """
            UPDATE import_runs SET status = ?, total = ?, inserted = ?, updated = ?,
                unchanged = ?, errors = ?, finished_at = ? WHERE import_id = ?
            """,
            (status, total, inserted, updated, unchanged, errors, _now(), import_id),
        )


def segment_counts(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM crawl_segments WHERE run_id = ? GROUP BY status",
        (run_id,),
    ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}

"""同口径统计与分组。所有口径显式写进返回值，便于报告引用。"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Sequence

from ..contracts import StatisticsQuery, ValidationError

PLAYTIME_BUCKETS = (
    ("0-1h", 0, 60),
    ("1-5h", 60, 300),
    ("5-20h", 300, 1200),
    ("20-50h", 1200, 3000),
    ("50-100h", 3000, 6000),
    ("100h+", 6000, None),
)


def _where(query: StatisticsQuery) -> tuple[str, list[Any]]:
    clause = " WHERE 1 = 1"
    params: list[Any] = []
    if query.appids:
        clause += f" AND appid IN ({', '.join(['?'] * len(query.appids))})"
        params.extend(int(a) for a in query.appids)
    if query.start_ts is not None:
        clause += " AND created_at >= ?"
        params.append(int(query.start_ts))
    if query.end_ts is not None:
        clause += " AND created_at < ?"
        params.append(int(query.end_ts))
    return clause, params


def totals(conn: sqlite3.Connection, query: StatisticsQuery) -> dict[str, Any]:
    where, params = _where(query)
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN is_recommended = 1 THEN 1 ELSE 0 END) AS positive,
            SUM(CASE WHEN is_recommended = 0 THEN 1 ELSE 0 END) AS negative,
            SUM(CASE WHEN is_recommended IS NULL THEN 1 ELSE 0 END) AS missing_recommendation,
            COUNT(DISTINCT author_steamid) AS authors,
            MIN(created_at) AS first_created_at,
            MAX(created_at) AS last_created_at
        FROM reviews{where}
        """,
        params,
    ).fetchone()

    total = int(row["total"] or 0)
    positive = int(row["positive"] or 0)
    negative = int(row["negative"] or 0)
    known = positive + negative
    return {
        "total_reviews": total,
        "positive": positive,
        "negative": negative,
        "known_recommendation": known,
        "missing_recommendation": int(row["missing_recommendation"] or 0),
        # 分母只包含推荐状态已知的记录
        "recommendation_rate": (positive / known) if known else None,
        "distinct_authors": int(row["authors"] or 0),
        "first_created_at": row["first_created_at"],
        "last_created_at": row["last_created_at"],
    }


def by_game(conn: sqlite3.Connection, query: StatisticsQuery) -> list[dict[str, Any]]:
    where, params = _where(query)
    rows = conn.execute(
        f"""
        SELECT appid,
               COUNT(*) AS total,
               SUM(CASE WHEN is_recommended = 1 THEN 1 ELSE 0 END) AS positive,
               SUM(CASE WHEN is_recommended = 0 THEN 1 ELSE 0 END) AS negative,
               SUM(CASE WHEN is_recommended IS NULL THEN 1 ELSE 0 END) AS missing,
               COUNT(DISTINCT author_steamid) AS authors,
               MIN(created_at) AS first_created_at,
               MAX(created_at) AS last_created_at
        FROM reviews{where}
        GROUP BY appid
        ORDER BY total DESC
        """,
        params,
    ).fetchall()
    result = []
    for row in rows:
        positive = int(row["positive"] or 0)
        negative = int(row["negative"] or 0)
        known = positive + negative
        result.append(
            {
                "appid": int(row["appid"]),
                "total": int(row["total"] or 0),
                "positive": positive,
                "negative": negative,
                "missing_recommendation": int(row["missing"] or 0),
                "known_recommendation": known,
                "recommendation_rate": (positive / known) if known else None,
                "distinct_authors": int(row["authors"] or 0),
                "first_created_at": row["first_created_at"],
                "last_created_at": row["last_created_at"],
            }
        )
    return result


def by_bucket(conn: sqlite3.Connection, query: StatisticsQuery) -> list[dict[str, Any]]:
    if query.bucket not in ("day", "month"):
        raise ValidationError(f"bucket 非法: {query.bucket}")
    fmt = "%Y-%m-%d" if query.bucket == "day" else "%Y-%m"
    where, params = _where(query)
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', created_at, 'unixepoch', '+8 hours') AS bucket,
               COUNT(*) AS total,
               SUM(CASE WHEN is_recommended = 1 THEN 1 ELSE 0 END) AS positive,
               SUM(CASE WHEN is_recommended = 0 THEN 1 ELSE 0 END) AS negative
        FROM reviews{where}
        GROUP BY bucket
        ORDER BY bucket
        """,
        params,
    ).fetchall()
    out = []
    for row in rows:
        positive = int(row["positive"] or 0)
        negative = int(row["negative"] or 0)
        known = positive + negative
        out.append(
            {
                "bucket": row["bucket"],
                "total": int(row["total"] or 0),
                "positive": positive,
                "negative": negative,
                "recommendation_rate": (positive / known) if known else None,
            }
        )
    return out


def by_playtime(conn: sqlite3.Connection, query: StatisticsQuery) -> list[dict[str, Any]]:
    where, params = _where(query)
    out: list[dict[str, Any]] = []
    for label, low, high in PLAYTIME_BUCKETS:
        clause = where + " AND playtime_at_review >= ?"
        bucket_params = params + [low * 60]
        if high is not None:
            clause += " AND playtime_at_review < ?"
            bucket_params.append(high * 60)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN is_recommended = 1 THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN is_recommended = 0 THEN 1 ELSE 0 END) AS negative
            FROM reviews{clause}
            """,
            bucket_params,
        ).fetchone()
        positive = int(row["positive"] or 0)
        negative = int(row["negative"] or 0)
        known = positive + negative
        out.append(
            {
                "bucket": label,
                "min_minutes": low * 60,
                "max_minutes": None if high is None else high * 60,
                "total": int(row["total"] or 0),
                "positive": positive,
                "negative": negative,
                "recommendation_rate": (positive / known) if known else None,
            }
        )
    # 缺失 playtime_at_review 的单独一行，避免被静默并入最小桶
    clause = where + " AND playtime_at_review IS NULL"
    row = conn.execute(f"SELECT COUNT(*) AS total FROM reviews{clause}", params).fetchone()
    out.append(
        {
            "bucket": "unknown",
            "min_minutes": None,
            "max_minutes": None,
            "total": int(row["total"] or 0),
            "positive": None,
            "negative": None,
            "recommendation_rate": None,
        }
    )
    return out


def missing_summary(conn: sqlite3.Connection, query: StatisticsQuery) -> dict[str, Any]:
    where, params = _where(query)
    fields = (
        "author_steamid",
        "content",
        "is_recommended",
        "playtime_at_review",
        "playtime_forever",
        "playtime_last_two_weeks",
        "last_played",
        "helpful_votes",
        "refunded",
        "developer_response",
        "comment_count",
        "votes_funny",
        "weighted_vote_score",
        "cpu_name",
        "adapter_description",
        "system_ram",
        "vram_size",
    )
    selects = ", ".join(
        f"SUM(CASE WHEN {field} IS NULL THEN 1 ELSE 0 END) AS {field}" for field in fields
    )
    row = conn.execute(
        f"SELECT COUNT(*) AS total, {selects} FROM reviews{where}", params
    ).fetchone()
    total = int(row["total"] or 0)
    return {
        "total": total,
        "missing": {field: int(row[field] or 0) for field in fields},
        "missing_rate": {
            field: (int(row[field] or 0) / total if total else None) for field in fields
        },
    }


def compute(conn: sqlite3.Connection, query: StatisticsQuery) -> dict[str, Any]:
    query.validate()
    return {
        "filters": {
            "appids": list(query.appids) if query.appids else None,
            "start_ts": query.start_ts,
            "end_ts": query.end_ts,
            "bucket": query.bucket,
        },
        "observed_at": int(time.time()),
        "totals": totals(conn, query),
        "by_game": by_game(conn, query),
        "by_bucket": by_bucket(conn, query),
        "by_playtime": by_playtime(conn, query),
        "missing": missing_summary(conn, query),
    }

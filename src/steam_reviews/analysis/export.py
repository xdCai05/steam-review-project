"""导出 CSV：评测明细与统计结果。"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..contracts import REVIEW_FIELD_KEYS, ReviewQuery

EXPORT_BATCH = 1000


def _write_rows(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def export_reviews_csv(
    conn: sqlite3.Connection, path: Path, *, appid: int | None = None,
    start_ts: int | None = None, end_ts: int | None = None,
) -> int:
    where = " WHERE 1 = 1"
    params: list[Any] = []
    if appid is not None:
        where += " AND appid = ?"
        params.append(appid)
    if start_ts is not None:
        where += " AND created_at >= ?"
        params.append(start_ts)
    if end_ts is not None:
        where += " AND created_at < ?"
        params.append(end_ts)

    header = list(REVIEW_FIELD_KEYS)
    cursor = conn.execute(
        f"SELECT {', '.join(header)} FROM reviews{where} ORDER BY appid, created_at DESC, review_id",
        params,
    )
    return _write_rows(path, header, ([row[col] for col in header] for row in cursor))


def export_games_csv(conn: sqlite3.Connection, path: Path) -> int:
    rows = conn.execute(
        """
        SELECT g.appid, g.name, g.release_date, g.coming_soon,
               g.developers, g.publishers, g.genres, g.platforms,
               s.is_free, s.price_initial, s.price_final, s.discount_percent,
               s.currency, s.region, s.collected_at AS price_collected_at
        FROM games g
        LEFT JOIN game_snapshots s ON s.snapshot_id = (
            SELECT snapshot_id FROM game_snapshots
            WHERE appid = g.appid ORDER BY collected_at DESC, snapshot_id DESC LIMIT 1
        )
        ORDER BY g.appid
        """
    ).fetchall()
    header = [
        "appid", "name", "release_date", "coming_soon", "developers", "publishers",
        "genres", "platforms", "is_free", "price_initial", "price_final",
        "discount_percent", "currency", "region", "price_collected_at",
    ]
    return _write_rows(path, header, ([row[col] for col in header] for row in rows))


def export_dict_rows(path: Path, rows: Sequence[Mapping[str, Any]], header: Sequence[str]) -> int:
    return _write_rows(path, header, ([row.get(col) for col in header] for row in rows))


def export_statistics(
    path_dir: Path, stats: Mapping[str, Any], *, prefix: str = ""
) -> dict[str, int]:
    """把统计结果拆成多张 CSV。"""
    path_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}

    totals = stats["totals"]
    written["totals"] = export_dict_rows(
        path_dir / f"{prefix}totals.csv", [totals], list(totals.keys())
    )

    if stats["by_game"]:
        written["by_game"] = export_dict_rows(
            path_dir / f"{prefix}by_game.csv",
            stats["by_game"],
            list(stats["by_game"][0].keys()),
        )
    if stats["by_bucket"]:
        written["by_bucket"] = export_dict_rows(
            path_dir / f"{prefix}by_{stats['filters']['bucket']}.csv",
            stats["by_bucket"],
            list(stats["by_bucket"][0].keys()),
        )
    if stats["by_playtime"]:
        written["by_playtime"] = export_dict_rows(
            path_dir / f"{prefix}by_playtime.csv",
            stats["by_playtime"],
            list(stats["by_playtime"][0].keys()),
        )
    missing_rows = [
        {"field": field, "missing": count, "missing_rate": stats["missing"]["missing_rate"][field]}
        for field, count in stats["missing"]["missing"].items()
    ]
    written["missing"] = export_dict_rows(
        path_dir / f"{prefix}missing.csv", missing_rows, ["field", "missing", "missing_rate"]
    )
    return written

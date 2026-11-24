#!/usr/bin/env python
"""导出指定范围的评测明细与统计结果（CSV）。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from steam_reviews.analysis import export as export_mod  # noqa: E402
from steam_reviews.contracts import ConfigError, StatisticsQuery  # noqa: E402
from steam_reviews.settings import load_settings  # noqa: E402
from steam_reviews.storage.database import connect, init_db  # noqa: E402



def build_coverage(conn, settings) -> list[dict]:
    """实际覆盖范围：每批次 × 游戏 × 时间段的状态、字节、落库条数与真实日期跨度。"""
    out: list[dict] = []
    rows = conn.execute(
        "SELECT run_id, appid, segment_index, start_ts, end_ts, status, pages, "
        "       fetched, review_bytes, stop_reason FROM crawl_segments "
        "ORDER BY run_id, appid, segment_index"
    ).fetchall()
    from steam_reviews.services.collection import segment_bytes_from_files
    for row in rows:
        stat = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS a, MAX(created_at) AS b "
            "FROM reviews WHERE appid = ? AND created_at >= ? AND created_at < ?",
            (row["appid"], row["start_ts"], row["end_ts"]),
        ).fetchone()
        out.append(
            {
                "run_id": row["run_id"],
                "appid": row["appid"],
                "segment_index": row["segment_index"],
                "segment_start": row["start_ts"],
                "segment_end": row["end_ts"],
                "status": row["status"],
                "pages": row["pages"],
                "fetched": row["fetched"],
                "review_bytes": segment_bytes_from_files(
                    settings, int(row["appid"]), int(row["start_ts"])
                ),
                "stop_reason": row["stop_reason"],
                "records_in_db": stat["n"],
                "covered_from": stat["a"],
                "covered_to": stat["b"],
                "segment_complete": row["status"] == "completed",
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 CSV")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None, help="输出目录，默认 data/exports/<时间戳>")
    parser.add_argument("--appid", type=int, action="append", help="可重复，默认全部")
    parser.add_argument("--bucket", default="day", choices=["day", "month"])
    parser.add_argument("--no-reviews", action="store_true", help="只导出统计，不导出明细")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.root)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return 2

    out_dir = Path(args.out) if args.out else settings.paths.exports_dir / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.paths.database)
    init_db(conn)

    query = StatisticsQuery(
        appids=args.appid or None,
        start_ts=settings.window_start_ts,
        end_ts=settings.window_end_ts,
        bucket=args.bucket,
    )
    from steam_reviews.analysis import statistics as stats_mod

    payload = stats_mod.compute(conn, query)
    payload["coverage"] = build_coverage(conn, settings)
    written = export_mod.export_statistics(out_dir, payload)
    written["games"] = export_mod.export_games_csv(conn, out_dir / "games.csv")
    if payload["coverage"]:
        written["coverage"] = export_mod.export_dict_rows(
            out_dir / "coverage.csv",
            payload["coverage"],
            list(payload["coverage"][0].keys()),
        )

    if not args.no_reviews:
        if args.appid:
            for appid in args.appid:
                written[f"reviews_{appid}"] = export_mod.export_reviews_csv(
                    conn, out_dir / f"reviews_{appid}.csv",
                    appid=appid, start_ts=settings.window_start_ts, end_ts=settings.window_end_ts,
                )
        else:
            written["reviews"] = export_mod.export_reviews_csv(
                conn, out_dir / "reviews.csv",
                start_ts=settings.window_start_ts, end_ts=settings.window_end_ts,
            )

    conn.close()
    (out_dir / "statistics.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ok": True,
                "out_dir": str(out_dir),
                "written": written,
                "totals": payload["totals"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

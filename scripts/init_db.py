#!/usr/bin/env python
"""初始化 SQLite 数据库：建表、建索引，可选校验结构。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from steam_reviews.contracts import ConfigError, StorageFailure  # noqa: E402
from steam_reviews.settings import load_settings  # noqa: E402
from steam_reviews.storage.database import connect, init_db, table_columns  # noqa: E402

EXPECTED_TABLES = (
    "crawl_runs",
    "crawl_segments",
    "game_snapshots",
    "games",
    "import_runs",
    "review_summary_snapshots",
    "reviews",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化数据库")
    parser.add_argument("--root", default=None)
    parser.add_argument("--database", default=None, help="覆盖数据库路径")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.root)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return 2

    database = Path(args.database) if args.database else settings.paths.database
    database.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = connect(database)
        tables = init_db(conn)
        columns = {table: table_columns(conn, table) for table in tables}
        conn.close()
    except StorageFailure as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return 1

    missing = [t for t in EXPECTED_TABLES if t not in tables]
    payload = {
        "ok": not missing,
        "database": str(database),
        "tables": tables,
        "missing_tables": missing,
        "review_columns": columns.get("reviews", []),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())

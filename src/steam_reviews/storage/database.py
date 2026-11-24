"""SQLite 连接、事务与建表。不返回连接给上层业务，只在注入的 conn 上操作。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..contracts import StorageFailure

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(database: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """打开数据库。调用方负责关闭。"""
    path = Path(database)
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if readonly:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        else:
            conn = sqlite3.connect(str(path), timeout=30)
    except sqlite3.Error as exc:
        raise StorageFailure(f"无法打开数据库: {path}", detail={"error": str(exc)}) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> list[str]:
    """执行 schema.sql，并对既有库做增量迁移。返回已存在的表名。"""
    try:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(sql)
        _migrate(conn)
        conn.commit()
    except (OSError, sqlite3.Error) as exc:
        raise StorageFailure("建表失败", detail={"error": str(exc)}) from exc
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


#: 增量列：(表, 列名, 列定义)
_MIGRATIONS = (
    ("crawl_segments", "review_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("crawl_segments", "stop_reason", "TEXT"),
    ("crawl_runs", "review_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("crawl_runs", "stop_reason", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补齐新增列；已存在则跳过。"""
    for table, column, definition in _MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务；嵌套调用时复用外层事务。"""
    if conn.in_transaction:
        yield conn
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row["name"] for row in rows]

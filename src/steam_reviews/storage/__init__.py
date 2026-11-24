"""存储层：连接、事务、幂等写入与检查点。"""

from .database import connect, init_db, transaction
from .repository import (
    bulk_upsert_reviews,
    count_reviews,
    get_game_row,
    get_run,
    get_segment,
    latest_price,
    list_segments,
    select_reviews,
    upsert_game,
    upsert_review,
)

__all__ = [
    "bulk_upsert_reviews",
    "connect",
    "count_reviews",
    "get_game_row",
    "get_run",
    "get_segment",
    "init_db",
    "latest_price",
    "list_segments",
    "select_reviews",
    "transaction",
    "upsert_game",
    "upsert_review",
]

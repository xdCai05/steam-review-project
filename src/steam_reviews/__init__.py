"""Steam 评测采集工具包。

数据流：采集 → 精简 JSONL 落盘 → 清洗校验 → SQLite → 统计与导出。
import 本包不联网、不建库、不创建任务。
"""

from .contracts import (
    REVIEW_FIELD_KEYS,
    CollectionRequest,
    CollectionSummary,
    ReviewPageRequest,
    SteamError,
)
from .settings import Settings, load_settings

__all__ = [
    "REVIEW_FIELD_KEYS",
    "CollectionRequest",
    "CollectionSummary",
    "ReviewPageRequest",
    "Settings",
    "SteamError",
    "load_settings",
]

__version__ = "1.0.0"

"""原始回应 → 固定 28 键正式记录。不做网络请求。"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

from ..contracts import (
    BOOL_FIELDS,
    FieldError,
    INT_FIELDS,
    REVIEW_FIELD_KEYS,
    TEXT_FIELDS,
)


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _non_negative(value: Any) -> int | None:
    number = _int(value)
    if number is None or number < 0:
        return None
    return number


def _positive_ts(value: Any) -> int | None:
    number = _int(value)
    if number is None or number <= 0:
        return None
    return number


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return value


def _text_or_none(value: Any) -> str | None:
    text = _text(value)
    if text is None or text == "":
        return None
    return text


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def normalize_review(
    raw: Mapping[str, Any],
    *,
    appid: int,
    run_id: str,
    collected_at: int,
    window_start: int,
    window_end: int,
    expected_language: str = "schinese",
) -> tuple[dict[str, Any] | None, list[FieldError]]:
    """把一条原始评测映射为 28 键记录。

    返回 ``(record, errors)``。当 review_id / language / created_at 不合法时
    record 为 None，调用方应写入 errors.jsonl 而不是正式表。
    """
    errors: list[FieldError] = []

    review_id = _text(raw.get("recommendationid"))
    if not review_id or not review_id.strip():
        errors.append(FieldError("review_id", "缺少或不合法的主键", None))
        return None, errors
    review_id = review_id.strip()

    language = _text_or_none(raw.get("language"))
    if language != expected_language:
        errors.append(
            FieldError("language", f"语言不是 {expected_language}", language)
        )
        return None, errors

    created_at = _int(raw.get("timestamp_created"))
    if created_at is None:
        errors.append(FieldError("created_at", "缺少或非整数时间戳", raw.get("timestamp_created")))
        return None, errors
    if not (window_start <= created_at < window_end):
        errors.append(
            FieldError(
                "created_at",
                "时间戳越出研究窗口",
                f"{created_at} not in [{window_start},{window_end})",
            )
        )
        return None, errors

    content = _text(raw.get("review"))
    if content is None:
        errors.append(FieldError("content", "缺少正文", None))
    elif content == "":
        errors.append(FieldError("content", "正文为空字符串", ""))

    is_recommended = _bool(raw.get("voted_up"))
    if is_recommended is None:
        errors.append(FieldError("is_recommended", "缺少推荐状态", raw.get("voted_up")))

    author = raw.get("author")
    author = author if isinstance(author, Mapping) else {}

    hardware = raw.get("hardware")
    hardware = hardware if isinstance(hardware, Mapping) else {}

    record: dict[str, Any] = {
        "review_id": review_id,
        "appid": int(appid),
        "author_steamid": _text_or_none(author.get("steamid")),
        "language": language,
        "content": content,
        "is_recommended": is_recommended,
        "created_at": created_at,
        "updated_at": _positive_ts(raw.get("timestamp_updated")),
        "playtime_at_review": _non_negative(author.get("playtime_at_review")),
        "playtime_forever": _non_negative(author.get("playtime_forever")),
        "playtime_last_two_weeks": _non_negative(author.get("playtime_last_two_weeks")),
        "last_played": _positive_ts(author.get("last_played")),
        "helpful_votes": _non_negative(raw.get("votes_up")),
        "steam_purchase": _bool(raw.get("steam_purchase")),
        "received_for_free": _bool(raw.get("received_for_free")),
        "early_access": _bool(raw.get("written_during_early_access")),
        "refunded": _bool(raw.get("refunded")),
        "developer_response": _text_or_none(raw.get("developer_response")),
        "timestamp_dev_responded": _positive_ts(raw.get("timestamp_dev_responded")),
        "comment_count": _non_negative(raw.get("comment_count")),
        "votes_funny": _non_negative(raw.get("votes_funny")),
        "weighted_vote_score": _finite(raw.get("weighted_vote_score")),
        "cpu_name": _text_or_none(hardware.get("cpu_name")),
        "adapter_description": _text_or_none(hardware.get("adapter_description")),
        "system_ram": _non_negative(hardware.get("system_ram")),
        "vram_size": _non_negative(hardware.get("vram_size")),
        "collected_at": int(collected_at),
        "crawl_run_id": run_id,
    }

    # 保证键集合与顺序严格等于约定
    ordered = {key: record[key] for key in REVIEW_FIELD_KEYS}
    return ordered, errors


def to_jsonl_line(record: Mapping[str, Any]) -> str:
    """按 28 键顺序序列化为一行 JSON，保留非 ASCII 原文。"""
    ordered = {key: record.get(key) for key in REVIEW_FIELD_KEYS}
    return json.dumps(ordered, ensure_ascii=False)


def mapping_to_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把已落盘的 JSONL 行还原为固定键顺序的字典。"""
    return {key: raw.get(key) for key in REVIEW_FIELD_KEYS}


__all__ = [
    "BOOL_FIELDS",
    "INT_FIELDS",
    "TEXT_FIELDS",
    "mapping_to_record",
    "normalize_review",
    "to_jsonl_line",
]

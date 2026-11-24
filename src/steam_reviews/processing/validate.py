"""记录校验：键集合、类型、取值范围与研究窗口。"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from ..contracts import (
    BOOL_FIELDS,
    INT_FIELDS,
    REVIEW_FIELD_KEYS,
    TEXT_FIELDS,
    ValidationError,
)


def check_keys(record: Mapping[str, Any]) -> list[str]:
    """返回问题描述列表；空列表表示通过。"""
    problems: list[str] = []
    actual = tuple(record.keys())
    if set(actual) != set(REVIEW_FIELD_KEYS):
        missing = sorted(set(REVIEW_FIELD_KEYS) - set(actual))
        extra = sorted(set(actual) - set(REVIEW_FIELD_KEYS))
        if missing:
            problems.append(f"缺少键: {missing}")
        if extra:
            problems.append(f"多余键: {extra}")
    return problems


def check_types(record: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in TEXT_FIELDS:
        value = record.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(f"{key} 应为字符串，实际 {type(value).__name__}")
    for key in INT_FIELDS:
        value = record.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            problems.append(f"{key} 应为整数，实际 {type(value).__name__}")
    for key in BOOL_FIELDS:
        value = record.get(key)
        if value is not None and not isinstance(value, bool):
            problems.append(f"{key} 应为布尔，实际 {type(value).__name__}")
    score = record.get("weighted_vote_score")
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            problems.append("weighted_vote_score 应为数值")
        elif not math.isfinite(float(score)):
            problems.append("weighted_vote_score 不是有限数值")
    for key in (
        "playtime_at_review",
        "playtime_forever",
        "playtime_last_two_weeks",
        "helpful_votes",
        "comment_count",
        "votes_funny",
        "system_ram",
        "vram_size",
    ):
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            problems.append(f"{key} 不能为负: {value}")
    return problems


def check_window(record: Mapping[str, Any], start_ts: int, end_ts: int) -> list[str]:
    created = record.get("created_at")
    if not isinstance(created, int):
        return ["created_at 缺失，无法判断窗口"]
    if not (start_ts <= created < end_ts):
        return [f"created_at {created} 越出 [{start_ts},{end_ts})"]
    return []


def check_record(
    record: Mapping[str, Any],
    *,
    window: tuple[int, int] | None = None,
    expected_language: str | None = "schinese",
    allowed_appids: Sequence[int] | None = None,
) -> list[str]:
    problems = check_keys(record) + check_types(record)
    if window:
        problems += check_window(record, window[0], window[1])
    if expected_language and record.get("language") != expected_language:
        problems.append(f"language 不是 {expected_language}: {record.get('language')!r}")
    if allowed_appids and record.get("appid") not in set(allowed_appids):
        problems.append(f"appid 不在固定清单内: {record.get('appid')!r}")
    if not record.get("review_id"):
        problems.append("review_id 为空")
    if not record.get("crawl_run_id"):
        problems.append("crawl_run_id 为空")
    return problems


def assert_valid(
    record: Mapping[str, Any],
    *,
    window: tuple[int, int] | None = None,
    expected_language: str | None = "schinese",
    allowed_appids: Sequence[int] | None = None,
) -> None:
    problems = check_record(
        record,
        window=window,
        expected_language=expected_language,
        allowed_appids=allowed_appids,
    )
    if problems:
        raise ValidationError("记录校验失败", detail={"problems": problems})

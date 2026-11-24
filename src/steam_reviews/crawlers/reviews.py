"""单页评测请求：只负责一次 HTTP 调用与结构校验，不写数据库。"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ..contracts import (
    QuerySummary,
    ResponseShapeError,
    ReviewPage,
    ReviewPageRequest,
)
from .client import HttpClient

API_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_response(
    payload: Mapping[str, Any],
    request: ReviewPageRequest,
    *,
    page_index: int,
    fetched_at: int,
    response_bytes: int = 0,
) -> ReviewPage:
    """把接口原始 JSON 解析成 ReviewPage，结构异常抛 ResponseShapeError。"""
    if not isinstance(payload, Mapping):
        raise ResponseShapeError("响应根节点不是对象", detail={"type": type(payload).__name__})

    body = payload.get("response")
    if body is None:
        if "success" in payload:  # 兼容旧式成功标记，避免静默失败
            raise ResponseShapeError(
                "响应缺少 response 字段", detail={"keys": sorted(payload.keys())[:10]}
            )
        raise ResponseShapeError("响应缺少 response 字段", detail={"keys": list(payload)[:10]})
    if not isinstance(body, Mapping):
        raise ResponseShapeError(
            "response 不是对象", detail={"type": type(body).__name__}
        )

    if body.get('error'):
        raise ResponseShapeError('接口返回错误，不能当作空页完成')
    raw_reviews = body.get("reviews")
    if raw_reviews is None:
        # Protobuf JSON可省略空数组，但必须有正常分页汇总才能确认是真正的空页。
        summary = body.get("query_summary")
        if (isinstance(summary, Mapping) and summary.get("num_reviews") == 0
                and not body.get("error")):
            raw_reviews = []
        else:
            raise ResponseShapeError("响应缺少评测数组或有效的空页汇总")
    if not isinstance(raw_reviews, list):
        raise ResponseShapeError(
            "reviews 不是数组", detail={"type": type(raw_reviews).__name__}
        )
    for item in raw_reviews:
        if not isinstance(item, Mapping):
            raise ResponseShapeError("reviews 数组内出现非对象元素")

    summary_raw = body.get("query_summary") or {}
    if not isinstance(summary_raw, Mapping):
        raise ResponseShapeError("query_summary 不是对象")

    summary = QuerySummary(
        num_reviews=_as_int(summary_raw.get("num_reviews")) or len(raw_reviews),
        total_matching=_as_int(body.get("total_matching")),
        day_range_used=_as_int(body.get("day_range_used")),
        review_score=_as_int(summary_raw.get("review_score")),
        review_score_desc=summary_raw.get("review_score_desc"),
        total_positive=_as_int(summary_raw.get("total_positive")),
        total_negative=_as_int(summary_raw.get("total_negative")),
        total_reviews=_as_int(summary_raw.get("total_reviews")),
    )

    cursor = body.get("cursor")
    next_cursor = cursor if isinstance(cursor, str) and cursor else None

    return ReviewPage(
        request=request,
        page_index=page_index,
        raw_reviews=tuple(dict(item) for item in raw_reviews),
        next_cursor=next_cursor,
        summary=summary,
        fetched_at=fetched_at,
        response_bytes=response_bytes,
    )


def fetch_review_page(client: HttpClient, request: ReviewPageRequest) -> ReviewPage:
    """请求一页评测。参数校验失败抛 ValidationError，网络问题由 client 统一处理。"""
    request.validate()
    payload, size = client.get_json(API_URL, params=request.http_params())
    return parse_response(
        payload,
        request,
        page_index=0,
        fetched_at=int(time.time()),
        response_bytes=size,
    )

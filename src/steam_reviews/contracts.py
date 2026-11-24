"""数据类型、常量与异常。

本模块只做定义，不含网络请求、数据库或文件写入，可安全 import。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------
# 固定字段清单
# --------------------------------------------------------------------------

#: 单条评测固定输出的 28 个键，顺序即 JSONL 落盘顺序。
REVIEW_FIELD_KEYS: tuple[str, ...] = (
    "review_id",
    "appid",
    "author_steamid",
    "language",
    "content",
    "is_recommended",
    "created_at",
    "updated_at",
    "playtime_at_review",
    "playtime_forever",
    "playtime_last_two_weeks",
    "last_played",
    "helpful_votes",
    "steam_purchase",
    "received_for_free",
    "early_access",
    "refunded",
    "developer_response",
    "timestamp_dev_responded",
    "comment_count",
    "votes_funny",
    "weighted_vote_score",
    "cpu_name",
    "adapter_description",
    "system_ram",
    "vram_size",
    "collected_at",
    "crawl_run_id",
)

REVIEW_FIELD_COUNT = len(REVIEW_FIELD_KEYS)

#: 布尔字段，便于校验与入库转换。
BOOL_FIELDS = (
    "is_recommended",
    "steam_purchase",
    "received_for_free",
    "early_access",
    "refunded",
)

INT_FIELDS = (
    "appid",
    "created_at",
    "updated_at",
    "playtime_at_review",
    "playtime_forever",
    "playtime_last_two_weeks",
    "last_played",
    "helpful_votes",
    "timestamp_dev_responded",
    "comment_count",
    "votes_funny",
    "system_ram",
    "vram_size",
    "collected_at",
)

TEXT_FIELDS = (
    "review_id",
    "author_steamid",
    "language",
    "content",
    "developer_response",
    "cpu_name",
    "adapter_description",
    "crawl_run_id",
)

# --------------------------------------------------------------------------
# 异常体系：库函数抛明确异常，命令行与未来工具适配层统一转换
# --------------------------------------------------------------------------


class SteamError(Exception):
    """所有可预期错误的基类。"""

    code = "steam_error"
    retryable = False

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class ConfigError(SteamError):
    code = "config_error"


class ValidationError(SteamError):
    code = "validation_error"


class NetworkError(SteamError):
    code = "network_error"
    retryable = True


class UpstreamTimeout(NetworkError):
    code = "upstream_timeout"


class RateLimited(NetworkError):
    code = "rate_limited"


class UpstreamUnavailable(SteamError):
    code = "upstream_unavailable"
    retryable = True


class NonRetryableUpstream(SteamError):
    """4xx 等不可重试的远端拒绝，例如参数错误或无权限。"""

    code = "upstream_rejected"
    retryable = False


class ResponseShapeError(SteamError):
    code = "response_shape_error"


class StorageFailure(SteamError):
    code = "storage_failure"


class RetryExhausted(SteamError):
    code = "retry_exhausted"


class NotFound(SteamError):
    code = "not_found"


# --------------------------------------------------------------------------
# 字段级异常记录
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldError:
    """一条字段级异常，写入 errors.jsonl。"""

    field: str
    reason: str
    value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "reason": self.reason, "value": self.value}


# --------------------------------------------------------------------------
# 时间分段
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentSpec:
    """一个互不重叠的采集时间段，北京时间切分后转为 UTC 秒。"""

    index: int
    start: Any  # datetime，带时区
    end: Any  # datetime，带时区
    start_ts: int
    end_ts: int

    @property
    def date_range_start(self) -> int:
        return self.start_ts

    @property
    def date_range_end_inclusive(self) -> int:
        """接口的结束时间为包含边界，秒级请求使用 end - 1。"""
        return self.end_ts - 1

    def contains(self, ts: int) -> bool:
        return self.start_ts <= ts < self.end_ts

    def label(self) -> str:
        return f"{self.start:%Y%m%d}-{self.end:%Y%m%d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
        }


# --------------------------------------------------------------------------
# 请求与响应
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewPageRequest:
    """单页请求：参数显式、已验证，不含默认值猜测。"""

    appid: int
    segment: SegmentSpec
    cursor: str | None = None
    num_per_page: int = 100
    languages: Sequence[str] = ("schinese",)
    review_filter: int = 1
    review_type: int = 0
    purchase_type: int = 1
    filter_offtopic_activity: bool = False
    param_style: str = "input_json"

    def validate(self) -> None:
        if not isinstance(self.appid, int) or self.appid <= 0:
            raise ValidationError(f"appid 非法: {self.appid!r}")
        if not 1 <= self.num_per_page <= 100:
            raise ValidationError(f"num_per_page 必须在 1..100: {self.num_per_page}")
        if not self.languages:
            raise ValidationError("languages 不能为空")
        if self.review_filter not in (0, 1, 2, 3):
            raise ValidationError(f"filter 非法: {self.review_filter}")
        if self.review_type not in (0, 1, 2):
            raise ValidationError(f"review_type 非法: {self.review_type}")
        if self.purchase_type not in (0, 1, 2):
            raise ValidationError(f"purchase_type 非法: {self.purchase_type}")
        if self.param_style not in ("input_json", "query"):
            raise ValidationError(f"param_style 非法: {self.param_style}")

    def input_json_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "appid": self.appid,
            "languages": list(self.languages),
            "filter": self.review_filter,
            "review_type": self.review_type,
            "purchase_type": self.purchase_type,
            "filter_offtopic_activity": bool(self.filter_offtopic_activity),
            "num_per_page": self.num_per_page,
            "date_range_start": self.segment.date_range_start,
            "date_range_end": self.segment.date_range_end_inclusive,
        }
        if self.cursor:
            body["cursor"] = self.cursor
        return body

    def query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "appid": self.appid,
            "filter": self.review_filter,
            "review_type": self.review_type,
            "purchase_type": self.purchase_type,
            "filter_offtopic_activity": str(bool(self.filter_offtopic_activity)).lower(),
            "num_per_page": self.num_per_page,
            "date_range_start": self.segment.date_range_start,
            "date_range_end": self.segment.date_range_end_inclusive,
        }
        for i, lang in enumerate(self.languages):
            params[f"languages[{i}]"] = lang
        if self.cursor:
            params["cursor"] = self.cursor
        return params

    def http_params(self) -> dict[str, Any]:
        if self.param_style == "input_json":
            return {"input_json": json.dumps(self.input_json_body(), ensure_ascii=False)}
        return self.query_params()

    def fingerprint(self) -> str:
        """参数指纹，用于判断是否需要新任务。"""
        payload = {
            "appid": self.appid,
            "languages": list(self.languages),
            "filter": self.review_filter,
            "review_type": self.review_type,
            "purchase_type": self.purchase_type,
            "filter_offtopic_activity": self.filter_offtopic_activity,
            "num_per_page": self.num_per_page,
            "start_ts": self.segment.start_ts,
            "end_ts": self.segment.end_ts,
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class QuerySummary:
    """接口返回的汇总。score 类字段只在首页且 review_type=0 时出现。"""

    num_reviews: int = 0
    total_matching: int | None = None
    day_range_used: int | None = None
    review_score: int | None = None
    review_score_desc: str | None = None
    total_positive: int | None = None
    total_negative: int | None = None
    total_reviews: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewPage:
    """单页结果。raw_reviews 是未裁剪的原始对象，正式记录在 processing 层生成。"""

    request: ReviewPageRequest
    page_index: int
    raw_reviews: tuple[dict[str, Any], ...]
    next_cursor: str | None
    summary: QuerySummary
    fetched_at: int
    response_bytes: int = 0

    @property
    def is_empty(self) -> bool:
        return len(self.raw_reviews) == 0

    @property
    def is_terminal(self) -> bool:
        """返回空评测列表才是正常完成信号。"""
        return self.is_empty

    def to_meta(self) -> dict[str, Any]:
        return {
            "appid": self.request.appid,
            "segment": self.request.segment.label(),
            "page_index": self.page_index,
            "cursor": self.request.cursor,
            "next_cursor": self.next_cursor,
            "count": len(self.raw_reviews),
            "fetched_at": self.fetched_at,
            "summary": self.summary.to_dict(),
        }


# --------------------------------------------------------------------------
# 商店数据
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceSnapshot:
    appid: int
    is_free: bool | None
    price_initial: int | None
    price_final: int | None
    discount_percent: int | None
    currency: str | None
    region: str
    collected_at: int
    crawl_run_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StoreData:
    appid: int
    name: str | None
    store_url: str
    short_description: str | None
    developers: list[str]
    publishers: list[str]
    genres: list[str]
    categories: list[str]
    release_date_raw: str | None
    release_date: str | None
    coming_soon: bool | None
    platforms: list[str]
    supported_languages: str | None
    header_image_url: str | None
    is_free: bool | None
    price: PriceSnapshot | None
    collected_at: int
    available: bool = True
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["price"] = self.price.to_dict() if self.price else None
        return data


@dataclass(frozen=True)
class StorePageSummary:
    """商店页 HTML 上可识别的评价口径，缺失为 None。"""

    appid: int
    overall_label: str | None
    overall_total: int | None
    overall_percent: int | None
    recent_label: str | None
    recent_total: int | None
    recent_percent: int | None
    language_total: int | None
    language_percent: int | None
    collected_at: int

    def scopes(self) -> list[dict[str, Any]]:
        return [
            {
                "scope": "page_overall",
                "review_score_desc": self.overall_label,
                "total_reviews": self.overall_total,
                "positive_percent_display": self.overall_percent,
            },
            {
                "scope": "page_recent",
                "review_score_desc": self.recent_label,
                "total_reviews": self.recent_total,
                "positive_percent_display": self.recent_percent,
            },
            {
                "scope": "page_language",
                "review_score_desc": None,
                "total_reviews": self.language_total,
                "positive_percent_display": self.language_percent,
            },
        ]


@dataclass(frozen=True)
class CoverResult:
    appid: int
    url: str | None
    local_path: str | None
    status: str  # downloaded / skipped / failed
    byte_size: int = 0
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# 采集任务
# --------------------------------------------------------------------------


@dataclass
class CollectionRequest:
    run_id: str
    appids: Sequence[int]
    segment_indexes: Sequence[int] | None = None
    max_pages_per_segment: int = 0
    dry_run: bool = False
    param_style: str = "input_json"
    note: str | None = None
    #: 单游戏额度覆盖（命令行 --appid-budget），单位字节
    cli_budgets: Mapping[int, int] | None = None
    policy_override: bool = False


@dataclass
class SegmentStatus:
    appid: int
    segment_index: int
    start_ts: int
    end_ts: int
    status: str = "pending"
    pages: int = 0
    cursor: str | None = None
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    errors: int = 0
    review_bytes: int = 0
    stop_reason: str | None = None
    finished_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CollectionSummary:
    run_id: str
    status: str
    segments: list[SegmentStatus] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    errors: int = 0
    review_bytes: int = 0
    completed: bool = False
    stop_reason: str | None = None
    capacity: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    output_dir: str | None = None
    started_at: int | None = None
    finished_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["segments"] = [s.to_dict() for s in self.segments]
        return data


@dataclass
class CollectionStatus:
    run_id: str
    status: str
    started_at: int | None
    finished_at: int | None
    params: dict[str, Any]
    segments: list[dict[str, Any]]
    totals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# 查询与统计
# --------------------------------------------------------------------------


@dataclass
class GameDetails:
    appid: int
    name: str | None
    store_url: str | None
    developers: list[str]
    publishers: list[str]
    genres: list[str]
    release_date: str | None
    coming_soon: bool | None
    header_image_url: str | None
    cover_local_path: str | None
    collected_at: int | None
    latest_price: dict[str, Any] | None
    review_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewQuery:
    appid: int | None = None
    start_ts: int | None = None
    end_ts: int | None = None
    limit: int = 50
    offset: int = 0
    order: str = "created_at_desc"

    def validate(self) -> None:
        if not 1 <= self.limit <= 500:
            raise ValidationError(f"limit 必须在 1..500: {self.limit}")
        if self.offset < 0:
            raise ValidationError(f"offset 不能为负: {self.offset}")
        if self.start_ts and self.end_ts and self.start_ts >= self.end_ts:
            raise ValidationError("start_ts 必须小于 end_ts")


@dataclass
class ReviewList:
    filters: dict[str, Any]
    observed_at: int
    total: int
    items: list[dict[str, Any]]
    limit: int
    offset: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StatisticsQuery:
    appids: Sequence[int] | None = None
    start_ts: int | None = None
    end_ts: int | None = None
    bucket: str = "day"

    def validate(self) -> None:
        if self.bucket not in ("day", "month"):
            raise ValidationError(f"bucket 非法: {self.bucket}")


@dataclass
class ReviewStatistics:
    filters: dict[str, Any]
    observed_at: int
    totals: dict[str, Any]
    by_game: list[dict[str, Any]]
    by_bucket: list[dict[str, Any]]
    by_playtime: list[dict[str, Any]]
    missing: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

"""商店资料、价格快照与页面评价口径采集。"""

from __future__ import annotations

import html as html_module
import re
import time
from typing import Any, Mapping

from ..contracts import (
    NonRetryableUpstream,
    PriceSnapshot,
    ResponseShapeError,
    StoreData,
    StorePageSummary,
)
from .client import HttpClient

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STORE_PAGE_URL = "https://store.steampowered.com/app/{appid}/"

#: 年龄门控：不带这些 cookie 时，商店页会省略整块用户评价区域（实测 184KB vs 226KB）
MATURE_COOKIES = {
    "birthtime": "283993201",
    "lastagecheckage": "1-January-1990",
    "mature_content": "1",
    "wants_mature_content": "1",
}

_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"([\d,]{1,15})")
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    value = html_module.unescape(_TAG_RE.sub("", text)).strip()
    return value or None


def _names(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            name = item.get("description") or item.get("name")
            if name:
                out.append(str(name))
        elif isinstance(item, str):
            out.append(item)
    return out


def _platforms(raw: Any) -> list[str]:
    if not isinstance(raw, Mapping):
        return []
    return [key for key, value in raw.items() if value]


def _to_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日$"), True),
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), True),
    (re.compile(r"^(\d{1,2})\s+(\w{3}),?\s+(\d{4})$"), False),
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_release_date(raw: str | None) -> str | None:
    """能准确解析才返回 YYYY-MM-DD，否则 None。"""
    if not raw:
        return None
    text = raw.strip()
    for pattern, year_first in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        if year_first:
            year, month, day = (int(part) for part in match.groups())
        else:
            day = int(match.group(1))
            month = _MONTHS.get(match.group(2).lower()[:3], 0)
            year = int(match.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _price_snapshot(
    data: Mapping[str, Any],
    *,
    appid: int,
    region: str,
    collected_at: int,
    run_id: str,
) -> PriceSnapshot:
    overview = data.get("price_overview")
    is_free = data.get("is_free")
    if isinstance(overview, Mapping):
        return PriceSnapshot(
            appid=appid,
            is_free=bool(is_free) if is_free is not None else None,
            price_initial=_to_int(overview.get("initial")),
            price_final=_to_int(overview.get("final")),
            discount_percent=_to_int(overview.get("discount_percent")),
            currency=overview.get("currency"),
            region=region,
            collected_at=collected_at,
            crawl_run_id=run_id,
        )
    # 免费、未发售、无法购买：价格为 NULL，但保留 is_free 事实
    return PriceSnapshot(
        appid=appid,
        is_free=bool(is_free) if is_free is not None else None,
        price_initial=None,
        price_final=None,
        discount_percent=None,
        currency=None,
        region=region,
        collected_at=collected_at,
        crawl_run_id=run_id,
    )


def fetch_store_data(
    client: HttpClient,
    appid: int,
    *,
    region: str,
    language: str,
    store_url: str | None = None,
    collected_at: int | None = None,
    run_id: str = "manual",
) -> StoreData:
    """获取一款游戏的商店基础资料与本体价格。未发售/不可访问不算任务失败。"""
    now = collected_at or int(time.time())
    url = store_url or STORE_PAGE_URL.format(appid=appid)
    payload, _ = client.get_json(
        APPDETAILS_URL, params={"appids": appid, "cc": region, "l": language}
    )
    if not isinstance(payload, Mapping):
        raise ResponseShapeError("appdetails 根节点不是对象")

    entry = payload.get(str(appid))
    if not isinstance(entry, Mapping):
        raise ResponseShapeError(f"appdetails 缺少 {appid} 键", detail={"keys": list(payload)[:5]})

    if not entry.get("success"):
        return StoreData(
            appid=appid,
            name=None,
            store_url=url,
            short_description=None,
            developers=[],
            publishers=[],
            genres=[],
            categories=[],
            release_date_raw=None,
            release_date=None,
            coming_soon=None,
            platforms=[],
            supported_languages=None,
            header_image_url=None,
            is_free=None,
            price=None,
            collected_at=now,
            available=False,
            note="商店接口 success=false：未发售、下架或地区不可访问",
        )

    data = entry.get("data") or {}
    if not isinstance(data, Mapping):
        raise ResponseShapeError("appdetails.data 不是对象")

    release = data.get("release_date") or {}
    release_raw = release.get("date") if isinstance(release, Mapping) else None
    coming_soon = release.get("coming_soon") if isinstance(release, Mapping) else None

    return StoreData(
        appid=appid,
        name=data.get("name"),
        store_url=url,
        short_description=data.get("short_description"),
        developers=[str(x) for x in (data.get("developers") or [])],
        publishers=[str(x) for x in (data.get("publishers") or [])],
        genres=_names(data.get("genres")),
        categories=_names(data.get("categories")),
        release_date_raw=release_raw,
        release_date=parse_release_date(release_raw),
        coming_soon=bool(coming_soon) if coming_soon is not None else None,
        platforms=_platforms(data.get("platforms")),
        supported_languages=_clean(data.get("supported_languages")),
        header_image_url=data.get("header_image"),
        is_free=bool(data["is_free"]) if data.get("is_free") is not None else None,
        price=_price_snapshot(
            data, appid=appid, region=region, collected_at=now, run_id=run_id
        ),
        collected_at=now,
        available=True,
        note=None,
    )


_ROW_RE = re.compile(r'<a class="user_reviews_summary_row".*?</a>', re.S)
_LABEL_IN_ROW_RE = re.compile(
    r'<span class="game_review_summary[^"]*"[^>]*>([^<]{1,40})</span>'
)
_SUBTITLE_RE = re.compile(r'<div class="subtitle column[^"]*">\s*([^<]{1,40})')
#: 只认「N 篇用户评测中有 P%」这种口径明确的形式
_COUNT_PERCENT_RE = re.compile(r"([\d,]{1,15})\s*篇用户评测中有\s*(\d{1,3})\s*%")


def _scope_of(subtitle: str | None) -> str | None:
    if not subtitle:
        return None
    if "最近" in subtitle:
        return "recent"
    if "全部" in subtitle or "总体" in subtitle:
        return "overall"
    # 商店页第二行是「简体中文评测：」，按语言口径保存
    if "语言" in subtitle or "中文" in subtitle:
        return "language"
    return None


def parse_store_page_summary(html: str, appid: int, collected_at: int) -> StorePageSummary:
    """按「最近评测 / 简体中文评测 / 全部评测」逐行解析，识别不到的保持 None。"""
    overall_label = overall_total = overall_percent = None
    recent_label = recent_total = recent_percent = None
    language_total = language_percent = None

    for row in _ROW_RE.findall(html):
        subtitle_match = _SUBTITLE_RE.search(row)
        subtitle = _clean(subtitle_match.group(1)) if subtitle_match else None
        scope = _scope_of(subtitle)
        if scope is None:
            continue

        label_match = _LABEL_IN_ROW_RE.search(row)
        label = _clean(label_match.group(1)) if label_match else None

        count = percent = None
        tip_match = re.search(r'data-tooltip-html="([^"]{1,300})"', row)
        source_text = html_module.unescape(tip_match.group(1)) if tip_match else row
        pair = _COUNT_PERCENT_RE.search(source_text)
        if pair:
            count = int(pair.group(1).replace(",", ""))
            percent = int(pair.group(2))

        if scope == "recent":
            recent_label, recent_total, recent_percent = label, count, percent
        elif scope == "language":
            language_total, language_percent = count, percent
        else:
            overall_label, overall_total, overall_percent = label, count, percent

    return StorePageSummary(
        appid=appid,
        overall_label=overall_label,
        overall_total=overall_total,
        overall_percent=overall_percent,
        recent_label=recent_label,
        recent_total=recent_total,
        recent_percent=recent_percent,
        language_total=language_total,
        language_percent=language_percent,
        collected_at=collected_at,
    )


def fetch_store_page_summary(
    client: HttpClient,
    appid: int,
    *,
    language: str,
    collected_at: int | None = None,
    mature_cookies: bool = True,
) -> StorePageSummary:
    now = collected_at or int(time.time())
    try:
        text, _ = client.get_text(
            STORE_PAGE_URL.format(appid=appid),
            params={"l": language},
            cookies=MATURE_COOKIES if mature_cookies else None,
        )
    except NonRetryableUpstream:
        raise
    return parse_store_page_summary(text, appid, now)

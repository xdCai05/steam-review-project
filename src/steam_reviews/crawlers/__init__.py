"""采集层：HTTP 客户端、单页评测、商店资料与图片。

import 本包不发起任何网络请求。
"""

from .client import HttpClient, RateLimiter, build_client
from .images import download_cover
from .reviews import API_URL, fetch_review_page, parse_response
from .store import (
    APPDETAILS_URL,
    STORE_PAGE_URL,
    fetch_store_data,
    fetch_store_page_summary,
    parse_release_date,
    parse_store_page_summary,
)

__all__ = [
    "API_URL",
    "APPDETAILS_URL",
    "STORE_PAGE_URL",
    "HttpClient",
    "RateLimiter",
    "build_client",
    "download_cover",
    "fetch_review_page",
    "fetch_store_data",
    "fetch_store_page_summary",
    "parse_release_date",
    "parse_response",
    "parse_store_page_summary",
]

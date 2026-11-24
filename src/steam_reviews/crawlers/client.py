"""HTTP 客户端：连接复用、限速、超时、状态码检查与统一重试。

错误分类集中在此模块，上层不再重复重试。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Callable, Mapping

import requests

from ..contracts import (
    NetworkError,
    NonRetryableUpstream,
    RateLimited,
    ResponseShapeError,
    RetryExhausted,
    UpstreamTimeout,
    UpstreamUnavailable,
)

logger = logging.getLogger(__name__)

#: 适合重试的服务端状态码
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RateLimiter:
    """固定最小间隔的节流器，单并发使用。"""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> float:
        """必要时休眠，返回实际等待秒数。"""
        if self.min_interval <= 0:
            return 0.0
        now = self._clock()
        if self._last is None:
            self._last = now
            return 0.0
        elapsed = now - self._last
        delay = self.min_interval - elapsed
        if delay > 0:
            self._sleep(delay)
            self._last = self._clock()
            return delay
        self._last = now
        return 0.0


class HttpClient:
    """带限速与重试的 HTTP 客户端。

    - 复用 ``requests.Session`` 保持长连接（实测：每次新建连接失败率约 26%）
    - 参数错误不重试，网络/限流/5xx 按退避重试
    - 不把响应内容当函数结果返回给业务层之前先做结构检查
    """

    def __init__(
        self,
        *,
        user_agent: str,
        min_interval_seconds: float = 2.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        retry_max: int = 5,
        backoff_seconds: tuple[float, ...] = (4, 8, 16, 32, 64),
        backoff_jitter: float = 1.0,
        respect_retry_after: bool = True,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.retry_max = max(0, int(retry_max))
        self.backoff_seconds = tuple(backoff_seconds) or (4, 8, 16, 32, 64)
        self.backoff_jitter = max(0.0, float(backoff_jitter))
        self.respect_retry_after = bool(respect_retry_after)
        self.timeout = (float(connect_timeout), float(read_timeout))
        self.user_agent = user_agent
        self._sleep = sleep
        self._owns_session = session is None
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            }
        )
        self.limiter = RateLimiter(min_interval_seconds, clock=clock, sleep=sleep)
        self.stats = {"requests": 0, "retries": 0, "bytes": 0, "rate_limited": 0}

    # -- 内部 ---------------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        base = self.backoff_seconds[min(attempt, len(self.backoff_seconds) - 1)]
        jitter = random.uniform(0, self.backoff_jitter) if self.backoff_jitter else 0.0
        return float(base) + jitter

    def _retry_after(self, response: requests.Response) -> float | None:
        if not self.respect_retry_after:
            return None
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        last_error: Exception | None = None
        attempts = self.retry_max + 1
        for attempt in range(attempts):
            self.limiter.wait()
            try:
                self.stats["requests"] += 1
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=self.timeout,
                )
            except requests.exceptions.ReadTimeout as exc:
                last_error = UpstreamTimeout(f"读取超时: {url}", detail={"attempt": attempt})
                self._retry_sleep(attempt, None, exc)
                continue
            except requests.exceptions.ConnectTimeout as exc:
                last_error = UpstreamTimeout(f"连接超时: {url}", detail={"attempt": attempt})
                self._retry_sleep(attempt, None, exc)
                continue
            except requests.exceptions.RequestException as exc:
                last_error = NetworkError(
                    f"网络错误: {type(exc).__name__}", detail={"url": url, "attempt": attempt}
                )
                self._retry_sleep(attempt, None, exc)
                continue

            self.stats["bytes"] += len(response.content or b"")

            if response.status_code == 200:
                return response

            if response.status_code == 429:
                self.stats["rate_limited"] += 1
                last_error = RateLimited(
                    "接口限流 (HTTP 429)",
                    detail={
                        "retry_after": self._retry_after(response),
                        "attempt": attempt,
                    },
                )
                self._retry_sleep(attempt, response, None)
                continue

            if response.status_code in RETRYABLE_STATUS:
                last_error = UpstreamUnavailable(
                    f"远端服务端错误 (HTTP {response.status_code})",
                    detail={"url": url, "attempt": attempt},
                )
                self._retry_sleep(attempt, response, None)
                continue

            # 4xx：参数或权限问题，重试无意义
            raise NonRetryableUpstream(
                f"远端拒绝 (HTTP {response.status_code})",
                detail={
                    "url": url,
                    "status": response.status_code,
                    "body_head": response.text[:200],
                },
            )

        raise RetryExhausted(
            f"重试 {self.retry_max} 次后仍失败: {last_error}",
            detail={
                "url": url,
                "last_error": str(last_error),
                "last_code": getattr(last_error, "code", None),
            },
        )

    def _retry_sleep(
        self, attempt: int, response: requests.Response | None, exc: Exception | None
    ) -> None:
        if attempt >= self.retry_max:
            return
        delay = None
        if response is not None:
            delay = self._retry_after(response)
        if delay is None:
            delay = self._backoff(attempt)
        self.stats["retries"] += 1
        reason = f"HTTP {response.status_code}" if response is not None else type(exc).__name__
        logger.warning(
            "请求失败，%.1fs 后重试 (%d/%d)：%s",
            delay,
            attempt + 1,
            self.retry_max,
            reason,
        )
        self._sleep(delay)

    # -- 便捷方法 -----------------------------------------------------
    def get_json(
        self, url: str, *, params: Mapping[str, Any] | None = None
    ) -> tuple[Any, int]:
        response = self.get(url, params=params)
        content_type = response.headers.get("content-type", "")
        body = response.content or b""
        if "json" not in content_type.lower():
            raise ResponseShapeError(
                f"响应不是 JSON（content-type={content_type!r}）",
                detail={"url": url, "body_head": body[:200].decode("utf-8", "replace")},
            )
        try:
            return json.loads(body.decode("utf-8")), len(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResponseShapeError(
                f"JSON 解析失败: {exc}", detail={"url": url, "bytes": len(body)}
            ) from exc

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> tuple[str, int]:
        response = self.get(url, params=params, cookies=cookies)
        body = response.content or b""
        encoding = response.encoding or "utf-8"
        try:
            return body.decode(encoding, "replace"), len(body)
        except LookupError:
            return body.decode("utf-8", "replace"), len(body)

    def get_bytes(
        self, url: str, *, params: Mapping[str, Any] | None = None
    ) -> tuple[bytes, str]:
        response = self.get(url, params=params)
        body = response.content or b""
        if len(body) > 8 * 1024 * 1024:
            raise ResponseShapeError(
                "图片体积超出上限", detail={"bytes": len(body), "url": url}
            )
        return body, response.headers.get("content-type", "")

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def build_client(settings: Any) -> HttpClient:
    """按配置构造客户端。不联网，直到首次 get。"""
    cfg = settings.rate_limit
    return HttpClient(
        user_agent=settings.user_agent,
        min_interval_seconds=cfg.min_interval_seconds,
        connect_timeout=cfg.connect_timeout,
        read_timeout=cfg.read_timeout,
        retry_max=cfg.retry_max,
        backoff_seconds=cfg.backoff_seconds,
        backoff_jitter=cfg.backoff_jitter,
        respect_retry_after=cfg.respect_retry_after,
    )

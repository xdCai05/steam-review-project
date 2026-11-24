"""配置加载与统一路径。

import 本模块不联网、不建库、不创建任务。所有默认值集中在此，不允许各函数
自行硬编码不同规则。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .collection_policy import CollectionPolicy, build_policy
from .catalog import load_catalog
from .contracts import ConfigError, SegmentSpec


# --------------------------------------------------------------------------
# 配置结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GameConfig:
    appid: int
    name: str
    name_en: str | None = None
    store_url: str | None = None


@dataclass(frozen=True)
class RequestConfig:
    languages: tuple[str, ...] = ("schinese",)
    review_filter: int = 1
    review_type: int = 0
    purchase_type: int = 1
    filter_offtopic_activity: bool = False
    num_per_page: int = 100
    verify_num_per_page: int = 20
    param_style: str = "input_json"


@dataclass(frozen=True)
class StoreConfig:
    region: str = "cn"
    language: str = "schinese"
    currency: str = "CNY"
    api_language: str = "schinese"
    mature_cookies: bool = True


@dataclass(frozen=True)
class RateLimitConfig:
    min_interval_seconds: float = 2.0
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    retry_max: int = 5
    backoff_seconds: tuple[float, ...] = (4, 8, 16, 32, 64)
    backoff_jitter: float = 1.0
    respect_retry_after: bool = True


@dataclass(frozen=True)
class PathConfig:
    root: Path
    tasks_dir: Path
    images_dir: Path
    database: Path
    exports_dir: Path
    logs_dir: Path
    samples_dir: Path

    def ensure(self) -> None:
        for path in (
            self.tasks_dir,
            self.images_dir,
            self.database.parent,
            self.exports_dir,
            self.logs_dir,
            self.samples_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class LimitConfig:
    """可选的页数保护，默认关闭（0 = 不限制）。

    条数上限与整体磁盘额度已由 ``CollectionPolicy`` 取代，此处不再保留旧常量。
    """

    max_pages_per_segment: int = 0


@dataclass(frozen=True)
class Settings:
    root: Path
    timezone_name: str
    window_start: datetime
    window_end: datetime
    games: tuple[GameConfig, ...]
    request: RequestConfig
    store: StoreConfig
    rate_limit: RateLimitConfig
    paths: PathConfig
    limits: LimitConfig
    user_agent: str
    api_key: str | None = None
    policy: CollectionPolicy | None = None
    #: 段遍历顺序：desc = 从最新月份向回采集（默认），asc = 从最老月份向前
    segment_order: str = "desc"

    # ---- 派生 ----------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def window_start_ts(self) -> int:
        return int(self.window_start.timestamp())

    @property
    def window_end_ts(self) -> int:
        return int(self.window_end.timestamp())

    @property
    def appids(self) -> tuple[int, ...]:
        return tuple(g.appid for g in self.games)

    def require_policy(self) -> CollectionPolicy:
        """策略只能由 collection_policy.build_policy 解析，这里不做兜底。"""
        if self.policy is None:
            raise ConfigError("Settings 缺少 policy，必须通过 load_settings 解析")
        return self.policy

    def game(self, appid: int) -> GameConfig:
        for g in self.games:
            if g.appid == appid:
                return g
        raise ConfigError(f"appid {appid} 不在固定游戏清单内")

    def ensure_appid(self, appid: int) -> GameConfig:
        return self.game(appid)

    def segments(self) -> list[SegmentSpec]:
        """按北京时间自然月切分 [start, end)，相邻区间不重叠。"""
        specs: list[SegmentSpec] = []
        cursor = self.window_start
        index = 0
        while cursor < self.window_end:
            year, month = cursor.year, cursor.month
            if month == 12:
                first_next = datetime(year + 1, 1, 1, tzinfo=self.tz)
            else:
                first_next = datetime(year, month + 1, 1, tzinfo=self.tz)
            seg_end = min(first_next, self.window_end)
            specs.append(
                SegmentSpec(
                    index=index,
                    start=cursor,
                    end=seg_end,
                    start_ts=int(cursor.timestamp()),
                    end_ts=int(seg_end.timestamp()),
                )
            )
            cursor = seg_end
            index += 1
        return specs

    def ordered_segments(self) -> list[SegmentSpec]:
        """按配置的段顺序返回，index 始终是月份序号，不随顺序改变。"""
        specs = self.segments()
        return list(reversed(specs)) if self.segment_order == "desc" else specs

    def segment(self, index: int) -> SegmentSpec:
        for spec in self.segments():
            if spec.index == index:
                return spec
        raise ConfigError(f"时间段序号不存在: {index}")

    def task_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ConfigError("run_id只能包含字母、数字、下划线和连字符")
        return self.paths.tasks_dir / run_id

    def game_dir(self, appid: int) -> Path:
        game = self.ensure_appid(appid)
        # AppID定位已有目录，名称仅方便人阅读，改名不另建一套数据。
        root = self.paths.tasks_dir.parent / "games"
        matches = sorted(root.glob(f"{appid}_*")) if root.exists() else []
        if len(matches) > 1:
            raise ConfigError(f"游戏{appid}存在多个数据目录")
        if matches:
            return matches[0]
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', game.name).strip(' .')
        return root / f"{appid}_{name or appid}"

    def review_dir(self, appid: int) -> Path:
        return self.game_dir(appid) / "reviews"

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "timezone": self.timezone_name,
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
                "start_ts": self.window_start_ts,
                "end_ts": self.window_end_ts,
            },
            "games": [g.__dict__ for g in self.games],
            "request": self.request.__dict__,
            "store": self.store.__dict__,
            "rate_limit": self.rate_limit.__dict__,
            "limits": self.limits.__dict__,
            "policy": self.policy.to_manifest() if self.policy else None,
            "paths": {k: str(v) for k, v in self.paths.__dict__.items()},
            "segment_order": self.segment_order,
            "segments": [s.to_dict() for s in self.segments()],
            "segments_in_run_order": [s.index for s in self.ordered_segments()],
        }


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where} 缺少必需项: {key}")
    return mapping[key]


def _parse_datetime(value: str, tz: ZoneInfo) -> datetime:
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ConfigError(f"无法解析时间: {value!r}")


def find_project_root(start: Path | None = None) -> Path:
    """向上查找包含 config/collection.json 的目录。"""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "config" / "collection.json").is_file():
            return candidate
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "config" / "collection.json").is_file():
            return candidate
    raise ConfigError("未找到项目根目录（config/collection.json）")


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def load_settings(root: str | Path | None = None, *, now: datetime | None = None) -> Settings:
    base = Path(root).resolve() if root else find_project_root()
    config_path = base / "config" / "collection.json"
    games_path = base / "config" / "games.json"
    if not config_path.is_file():
        raise ConfigError(f"缺少配置文件: {config_path}")
    if not games_path.is_file():
        raise ConfigError(f"缺少游戏清单: {games_path}")

    raw = json.loads(config_path.read_text(encoding="utf-8"))

    games = tuple(
        GameConfig(
            appid=int(g["appid"]),
            name=g.get("name") or str(g["appid"]),
            name_en=g.get("name_en"),
            store_url=g.get("store_url"),
        )
        for g in load_catalog(base).values()
    )
    if not games:
        raise ConfigError("游戏清单为空")

    window = _require(raw, "window", "配置")
    tz_name = str(window.get("timezone") or "Asia/Shanghai")
    tz = ZoneInfo(tz_name)
    if 'start' in window or 'end' in window:
        start = _parse_datetime(_require(window, 'start', 'window'), tz)
        end = _parse_datetime(_require(window, 'end', 'window'), tz)
    else:
        days = window.get('days', 90)
        if type(days) is not int or days <= 0:
            raise ConfigError('window.days 必须为正整数')
        end = (now.astimezone(tz) if now is not None else datetime.now(tz)).replace(microsecond=0)
        start = end - timedelta(days=days)
    if start >= end:
        raise ConfigError("window.start 必须早于 window.end")

    segment_order = str(window.get("segment_order", "desc")).lower()
    if segment_order not in ("asc", "desc"):
        raise ConfigError(f"window.segment_order 只能是 asc 或 desc，收到 {segment_order!r}")

    req = _require(raw, "request", "配置")
    request_cfg = RequestConfig(
        languages=_as_tuple(req.get("languages", ["schinese"])),
        review_filter=int(req.get("filter", 1)),
        review_type=int(req.get("review_type", 0)),
        purchase_type=int(req.get("purchase_type", 1)),
        filter_offtopic_activity=bool(req.get("filter_offtopic_activity", False)),
        num_per_page=int(req.get("num_per_page", 100)),
        verify_num_per_page=int(req.get("verify_num_per_page", 20)),
        param_style=str(req.get("param_style", "input_json")),
    )

    store_raw = raw.get("store", {}) or {}
    store_cfg = StoreConfig(
        region=str(store_raw.get("region", "cn")),
        language=str(store_raw.get("language", "schinese")),
        currency=str(store_raw.get("currency", "CNY")),
        api_language=str(store_raw.get("api_language", "schinese")),
        mature_cookies=bool(store_raw.get("mature_cookies", True)),
    )

    rl = raw.get("rate_limit", {}) or {}
    rate_cfg = RateLimitConfig(
        min_interval_seconds=float(rl.get("min_interval_seconds", 2.0)),
        connect_timeout=float(rl.get("connect_timeout", 10)),
        read_timeout=float(rl.get("read_timeout", 30)),
        retry_max=int(rl.get("retry_max", 5)),
        backoff_seconds=_as_tuple(rl.get("backoff_seconds", [4, 8, 16, 32, 64])),
        backoff_jitter=float(rl.get("backoff_jitter", 1.0)),
        respect_retry_after=bool(rl.get("respect_retry_after", True)),
    )

    paths_raw = raw.get("paths", {}) or {}
    paths = PathConfig(
        root=base,
        tasks_dir=base / str(paths_raw.get("tasks_dir", "data/tasks")),
        images_dir=base / str(paths_raw.get("games_dir", "data/games")),
        database=Path(os.environ.get("STEAM_DB_PATH") or (base / str(paths_raw.get("database", "data/database/steam_reviews.db")))),
        exports_dir=base / str(paths_raw.get("exports_dir", "data/exports")),
        logs_dir=base / str(paths_raw.get("logs_dir", "logs")),
        samples_dir=base / str(paths_raw.get("samples_dir", "data/samples")),
    )

    limits_raw = raw.get("limits", {}) or {}
    limits = LimitConfig(
        max_pages_per_segment=int(limits_raw.get("max_pages_per_segment", 0)),
    )
    for name in ("max_pages_per_segment",):
        if getattr(limits, name) < 0:
            raise ConfigError(f"limits.{name} 不能为负")

    # 资源策略只由 collection_policy 解析，避免默认值散落各处
    policy = build_policy(config=raw.get("policy", {}) or {})

    http_raw = raw.get("http", {}) or {}
    user_agent = str(
        http_raw.get("user_agent")
        or "steam-review-coursework/1.0 (+local research)"
    )

    return Settings(
        root=base,
        timezone_name=tz_name,
        window_start=start,
        window_end=end,
        games=games,
        request=request_cfg,
        store=store_cfg,
        rate_limit=rate_cfg,
        paths=paths,
        limits=limits,
        policy=policy,
        user_agent=user_agent,
        api_key=os.environ.get("STEAM_WEB_API_KEY") or None,
        segment_order=segment_order,
    )


def new_run_id(settings: Settings, prefix: str = "steam_reviews") -> str:
    """任务 ID 形如 steam_reviews_20261121_001，同日递增。"""
    stamp = datetime.now(settings.tz).strftime("%Y%m%d")
    root = settings.paths.tasks_dir
    existing = 0
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and child.name.startswith(f"{prefix}_{stamp}_"):
                try:
                    existing = max(existing, int(child.name.rsplit('_', 1)[1]))
                except ValueError:
                    continue
    return f"{prefix}_{stamp}_{existing + 1:03d}"

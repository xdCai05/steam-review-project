"""采集容量策略：默认值、覆盖优先级、校验与冻结。

本模块是资源策略的**唯一入口**。settings、命令行与服务不得各自复制默认数值。

优先级（从高到低）：
    1. 显式调用 / 命令行参数（全局值，或 ``--appid-budget APPID=BYTES``）
    2. 配置中的单游戏覆盖 ``policy.per_game_overrides``
    3. 配置中的全局值
    4. 本模块默认值

约定：
- 额度单位统一为**字节**（1 MB = 1 000 000 字节），字段名显式带 ``bytes``
- 只计量正式保存的 28 字段评测 JSONL 的 UTF-8 字节（含标点、空格、每行换行）
- 封面、商店资料、任务元信息、日志、异常文件、SQLite、导出副本**不计入**
- 额度按 AppID 跨全部时间段和任务累计；新任务与恢复均不清零
- 时间上限 ``None`` 表示不额外限制；**不使用 0 同时表示无限与零额度**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import ConfigError

#: 计量口径版本，冻结进 manifest；口径改变必须升版本
MEASUREMENT_VERSION = "reviews-jsonl-utf8-v1"
UNIT = "bytes"

DEFAULT_MAX_REVIEW_BYTES_PER_GAME = 100_000_000  # 100 MB（十进制）≈ 95.37 MiB
DEFAULT_MIN_FREE_DISK_BYTES = 5_000_000_000  # 5 GB


def _check_positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} 必须是整数，收到 {value!r}")
    if value <= 0:
        raise ConfigError(f"{name} 必须为正整数，收到 {value}（0 不代表无限额度）")
    return value


@dataclass(frozen=True)
class GameQuota:
    """单个 AppID 的最终额度及来源。"""

    appid: int
    max_review_bytes: int
    source: str  # cli_appid | cli_global | override | config | default

    def to_dict(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "max_review_bytes": self.max_review_bytes,
            "source": self.source,
        }


@dataclass(frozen=True)
class CollectionPolicy:
    """不可变的采集容量策略。"""

    max_review_bytes_per_game: int = DEFAULT_MAX_REVIEW_BYTES_PER_GAME
    min_free_disk_bytes: int = DEFAULT_MIN_FREE_DISK_BYTES
    max_runtime_seconds: int | None = None
    per_game_overrides: Mapping[int, int] = field(default_factory=dict)
    measurement: str = MEASUREMENT_VERSION

    def __post_init__(self) -> None:
        _check_positive_int("max_review_bytes_per_game", self.max_review_bytes_per_game)
        _check_positive_int("min_free_disk_bytes", self.min_free_disk_bytes)
        if self.max_runtime_seconds is not None:
            _check_positive_int("max_runtime_seconds", self.max_runtime_seconds)

        normalized: dict[int, int] = {}
        for key, value in dict(self.per_game_overrides or {}).items():
            appid = _check_positive_int("per_game_overrides.appid", int(key))
            normalized[appid] = _check_positive_int(
                f"per_game_overrides[{appid}]", int(value)
            )
        object.__setattr__(self, "per_game_overrides", MappingProxyType(normalized))

    # -- 解析 ---------------------------------------------------------
    def quota_for(self, appid: int, *, cli_budget: int | None = None) -> GameQuota:
        """按优先级解析某款游戏的最终额度。"""
        if cli_budget is not None:
            return GameQuota(appid, _check_positive_int("cli_budget", cli_budget), "cli_appid")
        if appid in self.per_game_overrides:
            return GameQuota(appid, self.per_game_overrides[appid], "override")
        return GameQuota(appid, self.max_review_bytes_per_game, "global")

    def total_budget_for(self, appids: tuple[int, ...]) -> int:
        return sum(self.quota_for(appid).max_review_bytes for appid in appids)

    # -- 冻结 ---------------------------------------------------------
    def to_manifest(self, appids: tuple[int, ...] | None = None) -> dict[str, Any]:
        """冻结进 manifest：单位、计量版本、每款最终额度。"""
        quotas = {}
        if appids:
            quotas = {str(a): self.quota_for(a).to_dict() for a in appids}
        return {
            "measurement": self.measurement,
            "unit": UNIT,
            "max_review_bytes_per_game": self.max_review_bytes_per_game,
            "min_free_disk_bytes": self.min_free_disk_bytes,
            "max_runtime_seconds": self.max_runtime_seconds,
            "per_game_overrides": {str(k): v for k, v in self.per_game_overrides.items()},
            "resolved_quotas": quotas,
            "notes": {
                "counted": "28 字段评测 JSONL 的 UTF-8 字节，含标点、空格与每条换行",
                "excluded": "封面、商店资料、任务元信息、日志、异常文件、SQLite、导出副本",
                "scope": "按 AppID 跨全部月份与任务共享正式评测文件额度，不重复存储",
            },
        }


def build_policy(
    *,
    config: Mapping[str, Any] | None = None,
    cli: Mapping[str, Any] | None = None,
) -> CollectionPolicy:
    """合并「配置 → 命令行」得到最终策略。未提供的项回落到模块默认值。"""
    config = dict(config or {})
    cli = dict(cli or {})

    overrides_raw = config.get("per_game_overrides") or {}
    overrides: dict[int, int] = {}
    if isinstance(overrides_raw, Mapping):
        for key, value in overrides_raw.items():
            if isinstance(value, Mapping):
                value = value.get("max_review_bytes_per_game", value.get("max_review_bytes"))
            overrides[int(key)] = int(value)

    if cli.get("max_review_bytes_per_game") is not None:
        # 显式全局参数优先于配置中的单游戏默认覆盖。
        overrides.clear()

    def pick(name: str) -> Any:
        if cli.get(name) is not None:
            return cli[name]
        if config.get(name) is not None:
            return config[name]
        return None

    max_bytes = pick("max_review_bytes_per_game")
    min_free = pick("min_free_disk_bytes")
    runtime = pick("max_runtime_seconds")

    return CollectionPolicy(
        max_review_bytes_per_game=(
            DEFAULT_MAX_REVIEW_BYTES_PER_GAME if max_bytes is None else int(max_bytes)
        ),
        min_free_disk_bytes=(
            DEFAULT_MIN_FREE_DISK_BYTES if min_free is None else int(min_free)
        ),
        max_runtime_seconds=None if runtime is None else int(runtime),
        per_game_overrides=overrides,
    )

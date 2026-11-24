"""资源保护：按评测字节预算逐游戏停止；不作为抽样方法。

新方案取代旧的「总条数 / 单款条数 / 整个 data 目录大小 / 3 小时」四道保护线。
唯一容量口径是**每款游戏正式评测 JSONL 的 UTF-8 字节数**，见
``steam_reviews.collection_policy``。

达到任一保护线只保存进度并标记停止原因，**不得**标记为采集完成。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ..collection_policy import CollectionPolicy

#: 写盘时为本页与 SQLite WAL 预留的余量
DISK_HEADROOM_BYTES = 16 * 1024 * 1024

STOP_FILE_NAME = "STOP"


def review_storage_bytes(folder: Path) -> int:
    """统计正式评测分片，兼容读页面时采集任务正在更新文件。"""
    total = 0
    for path in folder.glob('*/*.jsonl'):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


class CapacityGuard:
    """按 AppID 计量正式评测字节，并检查磁盘与运行时长。"""

    def __init__(
        self,
        settings,
        run_id: str,
        policy: CollectionPolicy,
        *,
        cli_budgets: Mapping[int, int] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self.run_id = run_id
        self.policy = policy
        self._clock = clock or time.monotonic
        self.started = self._clock()
        self._cli_budgets = dict(cli_budgets or {})
        self._used: dict[int, int] = {}
        self._quota: dict[int, int] = {}
        self._blocked: dict[int, str] = {}
        self.environment_reason: str | None = None

    # -- 路径与计量 ---------------------------------------------------
    def _app_dir(self, appid: int) -> Path:
        return self.settings.review_dir(appid)

    def quota_bytes(self, appid: int) -> int:
        if appid not in self._quota:
            self._quota[appid] = self.policy.quota_for(
                appid, cli_budget=self._cli_budgets.get(appid)
            ).max_review_bytes
        return self._quota[appid]

    def recount(self, appid: int) -> int:
        """从正式分片重新核对实际字节数；不信任缓存计数。"""
        total = review_storage_bytes(self._app_dir(appid))
        self._used[appid] = total
        return total

    def recount_all(self, appids: Iterable[int]) -> dict[int, int]:
        return {appid: self.recount(appid) for appid in appids}

    def used_bytes(self, appid: int) -> int:
        if appid not in self._used:
            self.recount(appid)
        return self._used[appid]

    def remaining_bytes(self, appid: int) -> int:
        return max(0, self.quota_bytes(appid) - self.used_bytes(appid))

    def can_commit(self, appid: int, page_bytes: int) -> bool:
        """整页放得下才允许提交；放不下时整页拒绝，不写半行。"""
        if page_bytes < 0:
            raise ValueError("page_bytes 不能为负")
        return self.used_bytes(appid) + page_bytes <= self.quota_bytes(appid)

    def commit(self, appid: int, page_bytes: int) -> None:
        self._used[appid] = self.used_bytes(appid) + page_bytes

    # -- 停止状态 -----------------------------------------------------
    def block_game(self, appid: int, reason: str) -> None:
        self._blocked.setdefault(appid, reason)

    def blocked_reason(self, appid: int) -> str | None:
        return self._blocked.get(appid)

    def check_environment(self) -> str | None:
        """检查与单款额度无关的保护线：STOP 文件、运行时长、磁盘余量。"""
        if self.environment_reason:
            return self.environment_reason

        if (self.settings.task_dir(self.run_id) / STOP_FILE_NAME).exists():
            self.environment_reason = "stop_file"
            return self.environment_reason

        if self.policy.max_runtime_seconds is not None:
            if self._clock() - self.started >= self.policy.max_runtime_seconds:
                self.environment_reason = "max_runtime_seconds"
                return self.environment_reason

        need = self.policy.min_free_disk_bytes + DISK_HEADROOM_BYTES
        for folder in {self.settings.paths.tasks_dir, self.settings.paths.database.parent}:
            try:
                free = shutil.disk_usage(folder).free
            except OSError:
                continue
            if free < need:
                self.environment_reason = "min_free_disk_bytes"
                return self.environment_reason

        return None

    def should_stop_all(self) -> bool:
        return self.check_environment() is not None

    def snapshot(self, appids: Iterable[int]) -> dict[str, object]:
        """当前额度使用情况，写入摘要与状态查询。"""
        return {
            "measurement": self.policy.measurement,
            "unit": "bytes",
            "environment_reason": self.environment_reason,
            "games": {
                str(appid): {
                    "used_bytes": self.used_bytes(appid),
                    "quota_bytes": self.quota_bytes(appid),
                    "remaining_bytes": self.remaining_bytes(appid),
                    "blocked_reason": self.blocked_reason(appid),
                }
                for appid in appids
            },
        }

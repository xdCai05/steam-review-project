"""资源保护测试：STOP 文件、磁盘阈值、运行时长、恢复不发请求。

容量额度本身在 test_capacity_policy.py 覆盖；这里只测环境级保护线。
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from steam_reviews.collection_policy import CollectionPolicy  # noqa: E402
from steam_reviews.contracts import CollectionRequest  # noqa: E402
from steam_reviews.services.collection import collect_reviews  # noqa: E402
from steam_reviews.settings import PathConfig, load_settings  # noqa: E402

APPID = 1086940
SEGMENT = 3  # 2026-11-01 ~ 2026-11-21


class FakeClient:
    def __init__(self, raw: dict):
        self.raw = raw
        self.calls: list[int] = []

    def get_json(self, url, *, params=None):
        body = json.loads(params["input_json"])
        self.calls.append(body["num_per_page"])
        if len(self.calls) > 3:  # 第 4 次返回空页，正常终止
            return {"response": {"reviews": []}}, 2
        row = dict(self.raw)
        row["recommendationid"] = f"r{len(self.calls)}"
        row["timestamp_created"] = body["date_range_start"] + 100
        payload = {"response": {"reviews": [row], "cursor": f"c{len(self.calls)}"}}
        return payload, len(json.dumps(payload))

    def close(self):
        pass


def make_settings(tmp: Path, policy: CollectionPolicy):
    paths = PathConfig(
        root=tmp,
        tasks_dir=tmp / "data" / "raw",
        images_dir=tmp / "data" / "images",
        database=tmp / "data" / "database" / "t.db",
        exports_dir=tmp / "data" / "exports",
        logs_dir=tmp / "logs",
        samples_dir=tmp / "data" / "samples",
    )
    paths.ensure()
    return dataclasses.replace(load_settings(ROOT), paths=paths, policy=policy)


class EnvironmentGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = json.loads(
            (ROOT / "tests" / "fixtures" / "sample_review_raw.json").read_text(encoding="utf-8")
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _collect(self, policy: CollectionPolicy | None = None, run_id: str = "guard"):
        settings = make_settings(self.root, policy or CollectionPolicy())
        client = FakeClient(self.raw)
        summary = collect_reviews(
            settings,
            CollectionRequest(run_id=run_id, appids=[APPID], segment_indexes=[SEGMENT]),
            client=client,
        )
        return summary, client

    def test_stop_file_blocks_without_network(self) -> None:
        flag = self.root / "data" / "raw" / "guard" / "STOP"
        flag.parent.mkdir(parents=True)
        flag.touch()
        summary, client = self._collect()
        self.assertEqual(client.calls, [])
        self.assertEqual(summary.status, "paused")
        self.assertEqual(summary.checkpoint["stop_reason"], "stop_file")

    def test_low_disk_pauses_before_first_request(self) -> None:
        with patch(
            "steam_reviews.services.safety.shutil.disk_usage",
            return_value=SimpleNamespace(free=1024),
        ):
            summary, client = self._collect()
        self.assertEqual(client.calls, [])
        self.assertEqual(summary.checkpoint["stop_reason"], "min_free_disk_bytes")

    def test_runtime_limit_pauses(self) -> None:
        policy = CollectionPolicy(max_runtime_seconds=1)
        ticks = iter([0] + [10] * 20)
        with patch("steam_reviews.services.safety.time.monotonic", side_effect=lambda: next(ticks)):
            summary, client = self._collect(policy)
        self.assertEqual(client.calls, [])
        self.assertEqual(summary.checkpoint["stop_reason"], "max_runtime_seconds")

    def test_capacity_stop_is_partial_and_resume_makes_no_calls(self) -> None:
        tiny = CollectionPolicy(max_review_bytes_per_game=1)
        summary, client = self._collect(tiny, run_id="cap")
        self.assertEqual(summary.status, "partial")
        self.assertEqual(summary.stop_reason, "capacity_limit")
        self.assertFalse(summary.completed)

        summary2, client2 = self._collect(tiny, run_id="cap")
        self.assertEqual(client2.calls, [], "恢复同一批次时已触顶的游戏不应再请求")
        self.assertEqual(summary2.stop_reason, "capacity_limit")

    def test_normal_run_does_not_raise(self) -> None:
        summary, client = self._collect(CollectionPolicy())
        self.assertIn(summary.status, ("completed", "paused", "partial"))
        self.assertEqual(summary.errors, 0)
        self.assertGreater(len(client.calls), 0)


if __name__ == "__main__":
    unittest.main()

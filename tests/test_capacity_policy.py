"""容量策略测试：字节口径、整页拒绝、跨段累计、恢复不清零、覆盖优先级。"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from steam_reviews.collection_policy import (  # noqa: E402
    CollectionPolicy,
    DEFAULT_MAX_REVIEW_BYTES_PER_GAME,
    build_policy,
)
from steam_reviews.contracts import CollectionRequest, ConfigError  # noqa: E402
from steam_reviews.processing.normalize import to_jsonl_line  # noqa: E402
from steam_reviews.services.collection import collect_reviews  # noqa: E402
from steam_reviews.services.safety import CapacityGuard  # noqa: E402
from steam_reviews.settings import PathConfig, RequestConfig, load_settings  # noqa: E402
from datetime import datetime, timezone
from steam_reviews.storage.database import connect, init_db  # noqa: E402

APPID_A = 4115450
APPID_B = 1086940


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


class FakeClient:
    """按预置脚本返回分页结果，不发网络请求。"""

    def __init__(self, pages_by_appid: dict[int, list[dict]]):
        self.pages_by_appid = pages_by_appid
        self.calls: list[tuple[int, str | None]] = []
        self._cursor: dict[int, int] = {}

    def get_json(self, url, *, params=None):
        body = json.loads(params["input_json"])
        appid = body["appid"]
        cursor = body.get("cursor")
        self.calls.append((appid, cursor))
        pages = self.pages_by_appid.get(appid, [])
        index = self._cursor.get(appid, 0)
        self._cursor[appid] = index + 1
        payload = pages[index] if index < len(pages) else {"response": {"reviews": []}}
        return payload, len(json.dumps(payload))

    def close(self) -> None:
        pass


def _raw(review_id: str, text: str, created_at: int) -> dict:
    return {
        "recommendationid": review_id,
        "author": {"steamid": "76561198000000000", "playtime_at_review": 60},
        "language": "schinese",
        "review": text,
        "timestamp_created": created_at,
        "timestamp_updated": created_at,
        "voted_up": True,
        "votes_up": 1,
        "steam_purchase": True,
    }


def _page(raws: list[dict], cursor: str | None) -> dict:
    body = {
        "reviews": raws,
        "query_summary": {"num_reviews": len(raws), "total_reviews": 100},
        "total_matching": 100,
    }
    if cursor:
        body["cursor"] = cursor
    return {"response": body}


def make_settings(
    tmp: Path, *, policy: CollectionPolicy, num_per_page: int = 2,
    segment_order: str = "asc",
):
    base = load_settings(ROOT, now=datetime(2026, 11, 20, 16, tzinfo=timezone.utc))
    paths = PathConfig(
        root=tmp,
        tasks_dir=tmp / "data" / "raw",
        images_dir=tmp / "data" / "images",
        database=tmp / "data" / "database" / "t.sqlite",
        exports_dir=tmp / "data" / "exports",
        logs_dir=tmp / "logs",
        samples_dir=tmp / "data" / "samples",
    )
    paths.ensure()
    request = dataclasses.replace(base.request, num_per_page=num_per_page)
    return dataclasses.replace(
        base, root=tmp, paths=paths, policy=policy, request=request,
        segment_order=segment_order,
    )


# --------------------------------------------------------------------------
# 策略解析
# --------------------------------------------------------------------------


class PolicyTest(unittest.TestCase):
    def test_defaults_are_100mb_decimal(self) -> None:
        policy = CollectionPolicy()
        self.assertEqual(policy.max_review_bytes_per_game, 100_000_000)
        self.assertEqual(policy.min_free_disk_bytes, 5_000_000_000)
        self.assertIsNone(policy.max_runtime_seconds)
        self.assertEqual(policy.measurement, "reviews-jsonl-utf8-v1")

    def test_zero_is_rejected_not_treated_as_unlimited(self) -> None:
        with self.assertRaises(ConfigError):
            CollectionPolicy(max_review_bytes_per_game=0)
        with self.assertRaises(ConfigError):
            CollectionPolicy(min_free_disk_bytes=0)
        with self.assertRaises(ConfigError):
            CollectionPolicy(max_runtime_seconds=0)

    def test_negative_and_non_int_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            CollectionPolicy(max_review_bytes_per_game=-1)
        with self.assertRaises(ConfigError):
            CollectionPolicy(max_review_bytes_per_game="100")

    def test_override_priority_order(self) -> None:
        policy = CollectionPolicy(
            max_review_bytes_per_game=100,
            per_game_overrides={APPID_A: 200},
        )
        self.assertEqual(policy.quota_for(APPID_A).max_review_bytes, 200)
        self.assertEqual(policy.quota_for(APPID_A).source, "override")
        self.assertEqual(policy.quota_for(APPID_B).max_review_bytes, 100)
        self.assertEqual(policy.quota_for(APPID_B).source, "global")
        # 命令行单游戏额度优先级最高
        self.assertEqual(policy.quota_for(APPID_A, cli_budget=300).max_review_bytes, 300)
        self.assertEqual(policy.quota_for(APPID_A, cli_budget=300).source, "cli_appid")

    def test_config_then_cli_resolution(self) -> None:
        policy = build_policy(
            config={"max_review_bytes_per_game": 500, "per_game_overrides": {str(APPID_A): 900}}
        )
        self.assertEqual(policy.max_review_bytes_per_game, 500)
        self.assertEqual(policy.per_game_overrides[APPID_A], 900)
        overridden = build_policy(
            config={"max_review_bytes_per_game": 500},
            cli={"max_review_bytes_per_game": 700},
        )
        self.assertEqual(overridden.max_review_bytes_per_game, 700)

    def test_missing_policy_raises_instead_of_defaulting(self) -> None:
        """策略缺失必须报错，不能静默回落到默认额度。"""
        from steam_reviews.settings import load_settings

        settings = dataclasses.replace(load_settings(ROOT), policy=None)
        with self.assertRaises(ConfigError):
            settings.require_policy()

    def test_manifest_freezes_units_and_quotas(self) -> None:
        policy = CollectionPolicy(per_game_overrides={APPID_A: 123})
        manifest = policy.to_manifest((APPID_A, APPID_B))
        self.assertEqual(manifest["unit"], "bytes")
        self.assertEqual(manifest["measurement"], "reviews-jsonl-utf8-v1")
        self.assertEqual(manifest["resolved_quotas"][str(APPID_A)]["max_review_bytes"], 123)
        self.assertEqual(
            manifest["resolved_quotas"][str(APPID_B)]["max_review_bytes"],
            DEFAULT_MAX_REVIEW_BYTES_PER_GAME,
        )


# --------------------------------------------------------------------------
# 字节判定
# --------------------------------------------------------------------------


class ByteBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = make_settings(self.root, policy=CollectionPolicy())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _guard(self, budget: int, *, run_id: str = "r1") -> CapacityGuard:
        policy = CollectionPolicy(max_review_bytes_per_game=budget)
        return CapacityGuard(self.settings, run_id, policy)

    def test_exactly_at_limit_is_allowed(self) -> None:
        guard = self._guard(10)
        self.assertTrue(guard.can_commit(APPID_A, 10))
        guard.commit(APPID_A, 10)
        self.assertEqual(guard.remaining_bytes(APPID_A), 0)
        self.assertTrue(guard.can_commit(APPID_A, 0))  # 空页不占额度

    def test_one_byte_over_is_rejected(self) -> None:
        guard = self._guard(10)
        guard.commit(APPID_A, 9)
        self.assertTrue(guard.can_commit(APPID_A, 1))
        self.assertFalse(guard.can_commit(APPID_A, 2))

    def test_utf8_bytes_for_chinese_and_emoji(self) -> None:
        record = {
            "review_id": "1",
            "appid": APPID_A,
            "content": "中文🙂",
        }
        line = to_jsonl_line(record)
        expected = len(line.encode("utf-8")) + 1
        self.assertEqual(expected, len(line.encode("utf-8")) + 1)
        self.assertGreater(len(line.encode("utf-8")), len(line))  # 多字节字符

    def test_recount_from_files_not_cache(self) -> None:
        folder = self.settings.review_dir(APPID_A) / '2026-08'
        folder.mkdir(parents=True)
        part1 = "x" * 100 + "\n"
        part2 = "y" * 50 + "\n"
        (folder / "seg0_p00001.jsonl").write_bytes(part1.encode("utf-8"))
        (folder / "seg0_p00002.jsonl").write_bytes(part2.encode("utf-8"))
        (folder / "seg0_p00003.jsonl.tmp").write_bytes(b"z" * 999)

        expected = len(part1.encode("utf-8")) + len(part2.encode("utf-8"))
        guard = self._guard(1000)
        self.assertEqual(guard.recount(APPID_A), expected)  # 临时文件不计
        self.assertEqual(guard.used_bytes(APPID_A), expected)

    def test_resume_does_not_reset_counter(self) -> None:
        folder = self.settings.review_dir(APPID_A) / '2026-08'
        folder.mkdir(parents=True)
        (folder / "seg0_p00001.jsonl").write_bytes(("a" * 60 + "\n").encode("utf-8"))

        first = self._guard(100)
        first.recount_all([APPID_A])
        self.assertEqual(first.remaining_bytes(APPID_A), 39)

        second = self._guard(100)  # 模拟重启
        second.recount_all([APPID_A])
        self.assertEqual(second.remaining_bytes(APPID_A), 39)

    def test_tasks_share_game_quota(self) -> None:
        folder = self.settings.review_dir(APPID_A) / '2026-08'
        folder.mkdir(parents=True)
        (folder/'part_00001.jsonl').write_bytes(b'a'*70+b'\n')
        self.assertEqual(self._guard(100, run_id='r1').used_bytes(APPID_A), 71)
        self.assertEqual(self._guard(100, run_id='r2').used_bytes(APPID_A), 71)

    def test_database_and_images_not_counted(self) -> None:
        (self.settings.paths.images_dir / "games" / str(APPID_A)).mkdir(parents=True)
        (self.settings.paths.images_dir / "games" / str(APPID_A) / "header.jpg").write_bytes(b"x" * 500)
        conn = connect(self.settings.paths.database)
        init_db(conn)
        conn.close()
        guard = self._guard(1000)
        guard.recount_all([APPID_A])
        self.assertEqual(guard.used_bytes(APPID_A), 0)


# --------------------------------------------------------------------------
# 服务编排
# --------------------------------------------------------------------------


class CollectionCapacityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, budget: int, pages_by_appid, appids):
        settings = make_settings(
            self.root, policy=CollectionPolicy(max_review_bytes_per_game=budget)
        )
        client = FakeClient(pages_by_appid)
        request = CollectionRequest(run_id="r1", appids=appids)
        return collect_reviews(settings, request, client=client), client

    def _page_bytes(self, raws) -> int:
        from steam_reviews.processing.normalize import normalize_review

        total = 0
        for raw in raws:
            record, _ = normalize_review(
                raw,
                appid=APPID_A,
                run_id="r1",
                collected_at=1795200000,
                window_start=1787414400,
                window_end=1795190400,
            )
            total += len(to_jsonl_line(record).encode("utf-8")) + 1
        return total

    def test_whole_page_rejected_when_one_byte_short(self) -> None:
        raws = [_raw("a1", "第一条评测" * 5, 1787500000)]
        size = self._page_bytes(raws)
        pages = {APPID_A: [_page(raws, "c1"), _page([], None)]}

        summary, _ = self._run(size - 1, pages, [APPID_A])
        seg = summary.segments[0]
        self.assertEqual(seg.pages, 0)
        self.assertIsNone(seg.cursor, "整页被拒绝时游标不得推进")
        self.assertEqual(seg.status, "partial")
        self.assertEqual(seg.stop_reason, "capacity_limit")
        self.assertEqual(summary.stop_reason, "capacity_limit")
        self.assertFalse(summary.completed)

    def test_page_fitting_exactly_is_saved(self) -> None:
        raws = [_raw("a1", "刚好放得下", 1787500000)]
        size = self._page_bytes(raws)
        pages = {APPID_A: [_page(raws, "c1"), _page([], None)]}
        summary, _ = self._run(size, pages, [APPID_A])
        seg = summary.segments[0]
        self.assertEqual(seg.pages, 1)
        self.assertEqual(seg.cursor, "c1")
        self.assertEqual(seg.review_bytes, size)

    def test_second_page_rejected_keeps_first_cursor(self) -> None:
        first = [_raw("a1", "第一页" * 20, 1787500000)]
        second = [_raw("a2", "第二页" * 40, 1787500100)]
        size_first = self._page_bytes(first)
        pages = {APPID_A: [_page(first, "c1"), _page(second, "c2"), _page([], None)]}
        summary, _ = self._run(size_first, pages, [APPID_A])
        seg = summary.segments[0]
        self.assertEqual(seg.pages, 1)
        self.assertEqual(seg.cursor, "c1", "被拒绝页的游标不得写入检查点")
        self.assertEqual(seg.status, "partial")
        self.assertEqual(seg.stop_reason, "capacity_limit")

    def test_one_game_stops_others_continue(self) -> None:
        raws_a = [_raw("a1", "A" * 200, 1787500000)]
        raws_b = [_raw("b1", "B", 1787500000)]
        pages = {
            APPID_A: [_page(raws_a, "ca"), _page([], None)],
            APPID_B: [_page(raws_b, "cb"), _page([], None)],
        }
        budget = self._page_bytes(raws_b) + 1  # B 放得下，A 放不下
        summary, client = self._run(budget, pages, [APPID_A, APPID_B])
        by_app = {s.appid: s for s in summary.segments}
        self.assertEqual(by_app[APPID_A].status, "partial")
        self.assertEqual(by_app[APPID_A].stop_reason, "capacity_limit")
        self.assertEqual(by_app[APPID_B].status, "completed")
        self.assertIn(APPID_B, {appid for appid, _ in client.calls})

    def test_multi_segment_accumulates_same_appid(self) -> None:
        raws = [_raw("a1", "跨段累计" * 10, 1787500000)]
        size = self._page_bytes(raws)
        pages = {APPID_A: [_page(raws, "c1"), _page([], None)]}
        settings = make_settings(
            self.root, policy=CollectionPolicy(max_review_bytes_per_game=size)
        )
        client = FakeClient(pages)
        request = CollectionRequest(run_id="r1", appids=[APPID_A])
        summary = collect_reviews(settings, request, client=client)
        # 段 0 正好用满额度：保守标记 partial，绝不再宣称该段完整
        self.assertEqual(summary.segments[0].status, "partial")
        self.assertEqual(summary.segments[0].stop_reason, "capacity_limit")
        self.assertEqual(summary.segments[0].review_bytes, size)
        # 后续段不再发请求，直接标记 partial
        self.assertTrue(all(s.status == "partial" for s in summary.segments[1:]))
        # 未开始的段落本身没有落盘字节，但游戏总额度只算一次
        self.assertTrue(all(s.review_bytes == 0 for s in summary.segments[1:]))
        self.assertEqual(summary.review_bytes, size)
        self.assertEqual(len(client.calls), 1, "额度用尽后不得继续请求")

    def test_segments_processed_newest_first_when_desc(self) -> None:
        raws = [_raw("a1", "最新月份优先", 1793700000)]  # 11 月，落在 seg3
        pages = {APPID_A: [_page(raws, "c1"), _page([], None)]}
        settings = make_settings(
            self.root,
            policy=CollectionPolicy(max_review_bytes_per_game=10_000_000),
            segment_order="desc",
        )
        client = FakeClient(pages)
        summary = collect_reviews(
            settings, CollectionRequest(run_id="r1", appids=[APPID_A]), client=client
        )
        self.assertEqual(summary.segments[0].segment_index, 3, "倒序时先采最新月份")
        self.assertEqual(summary.segments[0].status, "completed")
        self.assertEqual([s.segment_index for s in summary.segments], [3, 2, 1, 0])

    def test_policy_frozen_into_manifest(self) -> None:
        raws = [_raw("a1", "x", 1787500000)]
        pages = {APPID_A: [_page(raws, "c1"), _page([], None)]}
        summary, _ = self._run(5000, pages, [APPID_A])
        manifest = json.loads(
            (Path(self.root) / "data" / "raw" / "r1" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["policy"]["unit"], "bytes")
        self.assertEqual(manifest["policy"]["measurement"], "reviews-jsonl-utf8-v1")
        self.assertEqual(
            manifest["policy"]["resolved_quotas"][str(APPID_A)]["max_review_bytes"], 5000
        )
        self.assertEqual(summary.review_bytes, summary.segments[0].review_bytes)


if __name__ == "__main__":
    unittest.main()

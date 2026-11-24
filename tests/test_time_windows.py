"""时间段切分测试：时区、月界、区间不重叠、end-1 语义。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from steam_reviews.settings import load_settings  # noqa: E402


class TimeWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = load_settings(ROOT, now=datetime(2026, 11, 20, 16, tzinfo=timezone.utc))

    def test_window_moves_with_run_start(self) -> None:
        newer = load_settings(ROOT, now=datetime(2026, 12, 1, 10, 23, tzinfo=timezone.utc))
        self.assertEqual(newer.window_end.isoformat(), '2026-12-01T18:23:00+08:00')
        self.assertEqual(newer.window_end_ts - newer.window_start_ts, 90 * 86400)

    def test_window_matches_spec(self) -> None:
        self.assertEqual(self.settings.window_start.isoformat(), "2026-08-23T00:00:00+08:00")
        self.assertEqual(self.settings.window_end.isoformat(), "2026-11-21T00:00:00+08:00")

    def test_window_is_90_days(self) -> None:
        seconds = self.settings.window_end_ts - self.settings.window_start_ts
        self.assertEqual(seconds, 90 * 86400)

    def test_four_month_segments_without_overlap(self) -> None:
        segments = self.settings.segments()
        self.assertEqual(len(segments), 4)
        self.assertEqual(segments[0].start.isoformat(), "2026-08-23T00:00:00+08:00")
        self.assertEqual(segments[0].end.isoformat(), "2026-09-01T00:00:00+08:00")
        self.assertEqual(segments[1].end.isoformat(), "2026-10-01T00:00:00+08:00")
        self.assertEqual(segments[2].end.isoformat(), "2026-11-01T00:00:00+08:00")
        self.assertEqual(segments[3].end.isoformat(), "2026-11-21T00:00:00+08:00")
        for prev, cur in zip(segments, segments[1:]):
            self.assertEqual(prev.end_ts, cur.start_ts, "相邻区间必须首尾相接且不重叠")

    def test_segments_cover_whole_window(self) -> None:
        segments = self.settings.segments()
        self.assertEqual(segments[0].start_ts, self.settings.window_start_ts)
        self.assertEqual(segments[-1].end_ts, self.settings.window_end_ts)

    def test_end_is_exclusive_but_request_uses_end_minus_one(self) -> None:
        segment = self.settings.segment(1)
        self.assertEqual(segment.date_range_end_inclusive, segment.end_ts - 1)
        self.assertTrue(segment.contains(segment.start_ts))
        self.assertFalse(segment.contains(segment.end_ts))
        self.assertTrue(segment.contains(segment.end_ts - 1))

    def test_beijing_boundary_is_utc_previous_day(self) -> None:
        first = self.settings.segments()[0]
        utc = datetime.fromtimestamp(first.start_ts, timezone.utc)
        self.assertEqual(utc.isoformat(), "2026-08-22T16:00:00+00:00")
        self.assertEqual(
            datetime.fromtimestamp(first.start_ts, self.settings.tz).isoformat(),
            "2026-08-23T00:00:00+08:00",
        )

    def test_segment_order_is_configurable(self) -> None:
        self.assertEqual(self.settings.segment_order, "desc")
        ascending = [s.index for s in self.settings.segments()]
        run_order = [s.index for s in self.settings.ordered_segments()]
        self.assertEqual(ascending, [0, 1, 2, 3])
        self.assertEqual(run_order, [3, 2, 1, 0], "倒序采集：从最新月份向回")
        self.assertEqual(sorted(run_order), ascending, "顺序改变不影响段落集合")

    def test_segment_index_semantics_unchanged_by_order(self) -> None:
        # index 永远是月份序号，不随采集顺序变化
        self.assertEqual(
            self.settings.segment(0).start.isoformat(), "2026-08-23T00:00:00+08:00"
        )
        self.assertEqual(
            self.settings.segment(3).end.isoformat(), "2026-11-21T00:00:00+08:00"
        )

    def test_ordered_segments_never_overlap(self) -> None:
        specs = self.settings.ordered_segments()
        for prev, cur in zip(specs, specs[1:]):
            self.assertEqual(cur.end_ts, prev.start_ts, "倒序后相邻区间仍首尾相接")

    def test_invalid_segment_order_rejected(self) -> None:
        import tempfile
        import json
        from pathlib import Path as P
        from steam_reviews.contracts import ConfigError

        with tempfile.TemporaryDirectory() as directory:
            root = P(directory)
            (root / 'config').mkdir()
            source = ROOT/'config'
            (root/'config/games.json').write_text((source/'games.json').read_text(encoding='utf-8'),encoding='utf-8')
            config = json.loads((source/'collection.json').read_text(encoding='utf-8'))
            config['window']['segment_order'] = 'sideways'
            (root/'config/collection.json').write_text(json.dumps(config),encoding='utf-8')
            with self.assertRaises(ConfigError):
                load_settings(root)

    def test_original_game_ids_are_preserved(self) -> None:
        self.assertEqual(self.settings.appids[:5], (4115450, 1086940, 1771300, 1245620, 3280350))

    def test_unknown_appid_rejected(self) -> None:
        from steam_reviews.contracts import ConfigError

        with self.assertRaises(ConfigError):
            self.settings.ensure_appid(-1)


if __name__ == "__main__":
    unittest.main()

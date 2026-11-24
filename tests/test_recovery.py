"""回归：参数冻结、坏响应、跨任务去重和文件/数据库中断恢复。"""
import dataclasses
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_capacity_policy import make_settings, FakeClient, _raw, _page, APPID_A
from steam_reviews.collection_policy import CollectionPolicy, build_policy
from steam_reviews.contracts import CollectionRequest, ReviewPageRequest, ConfigError, ResponseShapeError
from steam_reviews.crawlers.reviews import parse_response
from steam_reviews.services.collection import collect_reviews, get_collection_status
from steam_reviews.storage import review_files


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name), policy=CollectionPolicy(max_review_bytes_per_game=100000))
        self.raw = _raw('unique1', '原始中文🙂', 1787500000)

    def run_task(self, run_id='run', pages=None, settings=None, **kwargs):
        return collect_reviews(settings or self.settings,
            CollectionRequest(run_id=run_id, appids=[APPID_A], segment_indexes=[0]),
            client=FakeClient({APPID_A: pages or [_page([self.raw], 'c1'), _page([], None)]}), **kwargs)

    def test_bad_response_is_not_an_empty_page(self):
        req = ReviewPageRequest(APPID_A, self.settings.segment(0))
        with self.assertRaises(ResponseShapeError):
            parse_response({'response': {'error': 'failure'}}, req, page_index=0, fetched_at=1)
        valid = parse_response({'response': {'query_summary': {'num_reviews': 0}}}, req, page_index=0, fetched_at=1)
        self.assertTrue(valid.is_terminal)

    def test_new_game_can_finish_with_empty_older_segments(self):
        settings = dataclasses.replace(self.settings, segment_order='desc')
        review = _raw('new-release', '发行三天内的评测', settings.window_end_ts - 3 * 86400)
        pages = [_page([review], 'more'), *[_page([], None) for _ in settings.segments()]]
        result = collect_reviews(settings, CollectionRequest(run_id='recent-release', appids=[APPID_A]),
                                 client=FakeClient({APPID_A: pages}))
        self.assertTrue(result.completed)
        self.assertTrue(all(segment.status == 'completed' for segment in result.segments))
        with closing(sqlite3.connect(settings.paths.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 1)

    def test_resume_restores_frozen_quota(self):
        self.run_task(should_stop=lambda: True)
        changed = dataclasses.replace(self.settings, policy=CollectionPolicy(max_review_bytes_per_game=200000))
        result = self.run_task(settings=changed)
        self.assertEqual(result.capacity['games'][str(APPID_A)]['quota_bytes'], 100000)

    def test_changed_filters_reject_old_cursor(self):
        self.run_task(should_stop=lambda: True)
        changed = dataclasses.replace(self.settings, request=dataclasses.replace(self.settings.request, review_type=2))
        with self.assertRaises(ConfigError):
            self.run_task(settings=changed)

    def test_explicit_global_budget_beats_config_override(self):
        p = build_policy(config={'per_game_overrides': {APPID_A: 200}}, cli={'max_review_bytes_per_game': 300})
        self.assertEqual(p.quota_for(APPID_A).max_review_bytes, 300)

    def test_second_task_updates_existing_monthly_part(self):
        self.run_task('first')
        first_files = list(self.settings.review_dir(APPID_A).glob('*/*.jsonl'))
        self.raw['review'] = '更新后的正文'
        self.run_task('second')
        files = list(self.settings.review_dir(APPID_A).glob('*/*.jsonl'))
        self.assertEqual(files, first_files)
        records = [r for f in files for r in review_files.read_records(f)]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['content'], '更新后的正文')
        self.assertEqual(files[0].parent.name, '2026-08')

    def test_failed_file_commit_recovers_without_double_charge(self):
        original = review_files.atomic_bytes
        failed = False

        def fail_after_file(path, data):
            nonlocal failed
            original(path, data)
            if path.suffix == '.jsonl' and not failed:
                failed = True
                raise OSError('simulated interruption after file replace')

        with patch('steam_reviews.storage.review_files.atomic_bytes', side_effect=fail_after_file):
            with self.assertRaises(OSError):
                self.run_task()
        journal = self.settings.task_dir('run')/'pending_page.json'
        self.assertTrue(journal.exists())
        result = self.run_task(pages=[_page([], None)])
        self.assertTrue(result.completed)
        self.assertFalse(journal.exists())
        files = list(self.settings.review_dir(APPID_A).glob('*/*.jsonl'))
        self.assertEqual(sum(len(review_files.read_records(f)) for f in files), 1)
        self.assertEqual(result.review_bytes, sum(f.stat().st_size for f in files))
        conn = sqlite3.connect(self.settings.paths.database)
        self.assertEqual(conn.execute('SELECT count(*) FROM reviews').fetchone()[0], 1)
        conn.close()

    def test_status_uses_actual_dates_not_month_boundaries(self):
        self.run_task()
        status = get_collection_status(self.settings, 'run')
        game = status.totals['capacity']['games'][str(APPID_A)]
        self.assertEqual(game['covered_start_ts'], self.raw['timestamp_created'])
        self.assertEqual(game['covered_end_ts'], self.raw['timestamp_created'])

    def test_task_name_cannot_escape_tasks_directory(self):
        with self.assertRaises(ConfigError):
            self.run_task('../escape')


if __name__ == '__main__':
    unittest.main()

"""Offline regression tests for the durable review queue and overview membership."""

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from steam_reviews.contracts import ConfigError
from steam_reviews.services import review_jobs
from steam_reviews.settings import load_settings


class ReviewJobsTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / 'reviews.db'
        now = datetime(2026, 11, 23, 14, tzinfo=timezone.utc)
        self.settings = load_settings(ROOT, now=now)
        self.later_settings = load_settings(ROOT, now=now + timedelta(days=2))

    def enqueue(self, appid):
        return review_jobs.enqueue(self.database, appid, f'游戏 {appid}')

    def claim(self, settings=None):
        return review_jobs.claim_job(self.database, settings or self.settings)

    def test_duplicate_concurrent_submissions_create_one_active_job(self):
        with ThreadPoolExecutor(max_workers=6) as threads:
            job_ids = list(threads.map(self.enqueue, [123] * 6))
        self.assertEqual(len(set(job_ids)), 1)
        self.assertEqual([job['id'] for job in review_jobs.list_jobs(self.database)], job_ids[:1])
        self.assertIsNone(review_jobs.get_job(self.database, job_ids[0])['window_end'])

    def test_single_running_job_and_fifo_order(self):
        first = self.enqueue(123)
        second = self.enqueue(234)
        with ThreadPoolExecutor(max_workers=2) as threads:
            jobs = list(threads.map(self.claim, [self.settings, self.settings]))
        claimed = [job for job in jobs if job is not None]
        self.assertEqual([job['id'] for job in claimed], [first])
        review_jobs.finish_job(self.database, claimed[0], 'completed')
        self.assertEqual(self.claim()['id'], second)

    def test_paused_job_keeps_slot_and_resumes_its_original_window(self):
        first = self.enqueue(123)
        next_id = self.enqueue(234)
        job = self.claim()
        review_jobs.change_job(self.database, first, 'pause')
        self.assertEqual(review_jobs.get_job(self.database, first)['status'], 'pausing')
        self.assertIsNone(self.claim())
        review_jobs.finish_job(self.database, job, 'paused')
        self.assertIsNone(self.claim())
        self.assertEqual(self.enqueue(123), first)

        review_jobs.change_job(self.database, first, 'resume')
        resumed = self.claim(self.later_settings)
        self.assertEqual(resumed['id'], first)
        for field in ('window_start', 'window_end', 'segments', 'started'):
            self.assertEqual(resumed[field], job[field])
        review_jobs.finish_job(self.database, resumed, 'completed')
        next_job = self.claim(self.later_settings)
        self.assertEqual(next_job['id'], next_id)
        self.assertEqual(next_job['window_end'], self.later_settings.window_end_ts)

    def test_worker_restart_resumes_interrupted_work_and_honors_requested_pause(self):
        job_id = self.enqueue(123)
        before = self.claim()
        review_jobs.recover_jobs(self.database)
        self.assertEqual(review_jobs.get_job(self.database, job_id)['status'], 'queued')
        after = self.claim(self.later_settings)
        self.assertEqual(after['id'], job_id)
        self.assertEqual(after['window_end'], before['window_end'])

        review_jobs.change_job(self.database, job_id, 'pause')
        review_jobs.recover_jobs(self.database)
        self.assertEqual(review_jobs.get_job(self.database, job_id)['status'], 'paused')
        self.assertIsNone(self.claim(self.later_settings))

    def test_waiting_for_writer_does_not_freeze_window_until_actual_start(self):
        job_id = self.enqueue(123)
        self.assertEqual(review_jobs.queue_head(self.database)['id'], job_id)
        review_jobs.note_writer_wait(self.database, job_id)
        waiting = review_jobs.get_job(self.database, job_id)
        self.assertEqual(waiting['status'], 'queued')
        self.assertIsNone(waiting['window_end'])
        self.assertTrue(waiting['message'])
        self.assertEqual(self.claim(self.later_settings)['window_end'], self.later_settings.window_end_ts)

    def test_failed_removed_and_stopped_jobs_never_enter_overview(self):
        failed = self.enqueue(123)
        removed = self.enqueue(234)
        stopped = self.enqueue(345)
        review_jobs.change_job(self.database, removed, 'remove')
        failed_job = self.claim()
        self.assertEqual(failed_job['id'], failed)
        review_jobs.finish_job(self.database, failed_job, 'failed', accepted=12)
        stopped_job = self.claim()
        self.assertEqual(stopped_job['id'], stopped)
        review_jobs.change_job(self.database, stopped, 'pause')
        review_jobs.finish_job(self.database, stopped_job, 'paused')
        review_jobs.change_job(self.database, stopped, 'stop')
        self.assertEqual(review_jobs.overview_ids(self.database), [])
        self.assertCountEqual([job['status'] for job in review_jobs.list_jobs(self.database)],
                              ['failed', 'cancelled', 'cancelled'])

    def test_completion_adds_membership_once_including_when_window_is_empty(self):
        for accepted in (0, 12):
            self.enqueue(123)
            review_jobs.finish_job(self.database, self.claim(), 'completed', accepted=accepted)
        self.assertEqual(review_jobs.overview_ids(self.database), [123])
        self.assertCountEqual([job['accepted'] for job in review_jobs.list_jobs(self.database)], [0, 12])

    def test_capacity_limited_publication_requires_the_users_choice_and_some_reviews(self):
        empty_id = self.enqueue(123)
        review_jobs.finish_job(self.database, self.claim(), 'limited')
        with self.assertRaises(ConfigError):
            review_jobs.change_job(self.database, empty_id, 'publish')

        usable_id = self.enqueue(234)
        review_jobs.finish_job(self.database, self.claim(), 'limited', accepted=12)
        self.assertEqual(review_jobs.overview_ids(self.database), [])
        review_jobs.change_job(self.database, usable_id, 'publish')
        review_jobs.change_job(self.database, usable_id, 'publish')
        self.assertEqual(review_jobs.overview_ids(self.database), [234])


if __name__ == '__main__':
    unittest.main()

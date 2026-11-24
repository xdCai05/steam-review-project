"""Offline tests straddling the queue, worker, and durable review collector."""
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

from test_capacity_policy import APPID_A, FakeClient, _page, _raw, make_settings
from scripts import review_worker
from steam_reviews.collection_policy import CollectionPolicy
from steam_reviews.contracts import CollectionRequest
from steam_reviews.services import review_jobs
from steam_reviews.services.collection import collect_reviews, collect_reviews_locked
from steam_reviews.storage.lease import collection_write_lock


class ReviewWorkerTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = make_settings(Path(folder.name), policy=CollectionPolicy(max_review_bytes_per_game=100000))
        self.database = self.settings.paths.database
        self.first_review = _raw('fixture-1', '本地测试数据', self.settings.window_start_ts + 10)

    def run_job(self, settings, job, client):
        def collect(config, request, **kwargs):
            return collect_reviews_locked(config, request, client=client, **kwargs)

        with patch.object(review_worker, 'collect_reviews_locked', side_effect=collect), collection_write_lock(settings):
            review_worker.run_claimed(settings, job, Event())
        return review_jobs.get_job(self.database, job['id'])

    def pausing_client(self, job_id, payload):
        database = self.database

        class PausingClient(FakeClient):
            def get_json(self, *args, **kwargs):
                response = super().get_json(*args, **kwargs)
                review_jobs.change_job(database, job_id, 'pause')
                return response

        return PausingClient({APPID_A: [payload]})

    def test_pause_commits_current_page_and_resume_preserves_frozen_window(self):
        job_id = review_jobs.enqueue(self.database, APPID_A, '样例')
        original = review_jobs.claim_job(self.database, self.settings)
        page = _page([self.first_review], 'next-cursor')
        paused = self.run_job(self.settings, original, self.pausing_client(job_id, page))
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(review_jobs.overview_ids(self.database), [])
        log = (self.settings.task_dir(job_id) / 'review_job.log').read_text(encoding='utf-8')
        self.assertIn('第 1 页已提交', log)
        self.assertNotIn('本地测试数据', log)

        review_jobs.change_job(self.database, job_id, 'resume')
        later = replace(self.settings, window_start=self.settings.window_start + timedelta(days=5),
                        window_end=self.settings.window_end + timedelta(days=5),
                        request=replace(self.settings.request, num_per_page=10))
        resumed = review_jobs.claim_job(self.database, later)
        client = FakeClient({APPID_A: [_page([], None) for _ in self.settings.segments()]})
        done = self.run_job(later, resumed, client)
        self.assertEqual(client.calls[0], (APPID_A, 'next-cursor'))
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['accepted'], 1)
        self.assertEqual(done['window_end'], original['window_end'])
        self.assertEqual(review_jobs.overview_ids(self.database), [APPID_A])

    def test_pause_after_one_empty_segment_does_not_publish(self):
        job_id = review_jobs.enqueue(self.database, APPID_A, '样例')
        job = review_jobs.claim_job(self.database, self.settings)
        paused = self.run_job(self.settings, job, self.pausing_client(job_id, _page([], None)))
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(review_jobs.overview_ids(self.database), [])

    def test_worker_stop_requeues_running_job_after_saving_current_page(self):
        job_id = review_jobs.enqueue(self.database, APPID_A, '样例')
        job = review_jobs.claim_job(self.database, self.settings)
        stopped = Event()

        class StoppingClient(FakeClient):
            def get_json(self, *args, **kwargs):
                reply = super().get_json(*args, **kwargs)
                stopped.set()
                return reply

        client = StoppingClient({APPID_A: [_page([self.first_review], 'continue-here')]})

        def collect(config, request, **kwargs):
            return collect_reviews_locked(config, request, client=client, **kwargs)

        with patch.object(review_worker, 'collect_reviews_locked', side_effect=collect), collection_write_lock(self.settings):
            review_worker.run_claimed(self.settings, job, stopped)

        self.assertEqual(review_jobs.get_job(self.database, job_id)['status'], 'queued')
        resumed = review_jobs.claim_job(self.database, self.settings)
        next_client = FakeClient({APPID_A: [_page([], None) for _ in self.settings.segments()]})
        result = self.run_job(self.settings, resumed, next_client)
        self.assertEqual(next_client.calls[0], (APPID_A, 'continue-here'))
        self.assertEqual(result['accepted'], 1)

    def test_capacity_limit_can_publish_review_already_in_window(self):
        collect_reviews(self.settings, CollectionRequest(run_id='previous', appids=[APPID_A], segment_indexes=[0]),
                        client=FakeClient({APPID_A: [_page([self.first_review], 'cursor'), _page([], None)]}))
        constrained = replace(self.settings, policy=CollectionPolicy(max_review_bytes_per_game=10))
        job_id = review_jobs.enqueue(self.database, APPID_A, '样例')
        job = review_jobs.claim_job(self.database, constrained)
        client = FakeClient({APPID_A: []})
        limited = self.run_job(constrained, job, client)
        self.assertEqual(client.calls, [])
        self.assertEqual(limited['status'], 'limited')
        self.assertEqual(limited['accepted'], 1)
        self.assertEqual(review_jobs.overview_ids(self.database), [])
        review_jobs.change_job(self.database, job_id, 'publish')
        self.assertEqual(review_jobs.overview_ids(self.database), [APPID_A])

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from steam_reviews.services.worker_control import WorkerController, WorkerRuntime, read_record
from steam_reviews.storage.lease import exclusive_file


class WorkerControlTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.runtime = WorkerRuntime(self.root / 'tasks', self.root / 'reviews.db')
        self.controller = self.make_controller(self.runtime)

    def make_controller(self, runtime):
        return WorkerController(runtime, self.root, self.root / 'review_worker.py', self.root / 'worker.log')

    def test_deduplicates_start_and_reports_a_failed_launch(self):
        with patch('steam_reviews.services.worker_control.subprocess.Popen') as start_process:
            start_process.return_value.poll.return_value = None
            self.assertEqual(self.controller.change('start')['state'], 'starting')
            ticket = read_record(self.runtime.request)['session']
            self.assertTrue(self.runtime.accepts_start(ticket))
            self.assertFalse(self.runtime.accepts_start('other'))
            self.assertEqual(self.make_controller(self.runtime).change('start')['state'], 'starting')
            start_process.assert_called_once()
            start_process.return_value.poll.return_value = 1
            failed = self.controller.status()
            self.assertEqual(failed['state'], 'offline')
            self.assertTrue(failed['message'])

    def test_stop_targets_only_the_current_worker_session_and_database(self):
        stopped = Event()
        with exclusive_file(self.runtime.worker_lock):
            with self.runtime.publish(stopped, 'first'):
                self.assertEqual(self.controller.status()['state'], 'online')
                foreign = self.make_controller(WorkerRuntime(self.root / 'tasks', self.root / 'other.db'))
                self.assertEqual(foreign.change('stop')['state'], 'unavailable')
                self.assertEqual(self.controller.change('stop')['state'], 'stopping')
                self.assertTrue(stopped.wait(3))
                self.assertEqual(read_record(self.runtime.request)['session'], 'first')
        self.assertEqual(self.controller.status()['state'], 'offline')
        with exclusive_file(self.runtime.worker_lock):
            with self.runtime.publish(stopped := Event(), 'second'):
                self.assertEqual(self.controller.status()['state'], 'online')
                self.assertFalse(stopped.wait(.5))

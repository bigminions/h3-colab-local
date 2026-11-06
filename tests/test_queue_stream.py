"""Tests for non-blocking queueing, queue worker, and cancellation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from tests.test_app import FakeTransport


class QueueStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = app.Controller(self.root, self.root)
        self.fake = FakeTransport(self.control)
        self.control.transport = self.fake

    def tearDown(self):
        self.temp.cleanup()

    def test_queue_submission_while_active_enqueues_and_runs_fifo(self):
        with patch.object(app.threading, 'Thread'):
            # Submissions stay queued until the consumer claims them.
            id1 = self.control.start(app.H3JobConfig(prompt='job 1'))
            self.assertIsNone(self.control.active)
            self.assertEqual(self.control.jobs[id1]['status'], 'queued')

            # 2. Start job 2 while active is running
            id2 = self.control.start(app.H3JobConfig(prompt='job 2'))
            self.assertEqual(self.control.jobs[id2]['status'], 'queued')

            # 3. Start job 3 while active is running
            id3 = self.control.start(app.H3JobConfig(prompt='job 3'))
            self.assertEqual(self.control.jobs[id3]['status'], 'queued')

            # 4. Cancel job 2 (queued job cancel test)
            res = self.control.cancel(id2)
            self.assertTrue(res['ok'])
            self.assertEqual(self.control.jobs[id2]['status'], 'cancelled')
            self.assertFalse(self.control.public_job(self.control.jobs[id2])['can_cancel'])
            # Cancelling an entry does not claim or interrupt another task.
            self.assertIsNone(self.control.active)

            # 5. Let job 1 finish
            self.control.jobs[id1]['status'] = 'running'
            self.control.active = id1
            self.control.run(self.control.jobs[id1])
            self.assertEqual(self.control.jobs[id1]['status'], 'done')

            # Check that job 3 is the next queued job
            queued = [j for j in self.control.jobs.values() if j.get('status') == 'queued' and not j.get('cancel_requested')]
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]['id'], id3)


if __name__ == '__main__':
    unittest.main()

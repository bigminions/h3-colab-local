"""Single-user scheduling and persistence; no account or network access."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
from job_queue import JobStore, waiting_jobs
from tests.test_app import FakeTransport


class UnifiedQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.root = Path(self.temp.name)
        self.c = app.Controller(self.root, self.root)
        self.fake = FakeTransport(self.c)
        self.c.transport = self.fake

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, name, app_id='h3', gpu='A100', front=False, count=1, release_mode='idle'):
        model = app.applications.get(app_id).config_model
        with patch.object(app.threading, 'Thread'):
            key = self.c.start(model(prompt=name, gpu=gpu, count=count, release_mode=release_mode), placement='front' if front else 'back')
        return key

    def test_front_overrides_affinity_once_then_affinity_resumes(self):
        self.c.session = dict(endpoint='owned-test', app_id='h3', accelerator='A100')
        oldest = self.enqueue('other', 'qwen_image_2_1')
        same = self.enqueue('same')
        front = self.enqueue('front', 'qwen_image_2_1', front=True)
        executed = []
        def run(job):
            executed.append(job['id'])
            self.c.session = dict(endpoint='owned-test', app_id=job['config']['app_id'], accelerator='A100')
            job['status'] = 'done'
        self.c.run = run
        predicted = [j['id'] for j in self.c.queue_snapshot()]
        self.c._run_queue()
        self.assertEqual(executed, [front, oldest, same])
        self.assertEqual(predicted, executed)
        self.assertFalse(self.c.jobs[front]['queue_front'])

    def test_front_insert_preserves_existing_order_and_survives_restart(self):
        first = self.enqueue('first')
        second = self.enqueue('second')
        front = self.enqueue('front', front=True)
        self.c.event(self.c.jobs[first], 'connect', 'waiting')
        restored = app.Controller(self.root, self.root)
        self.assertEqual([j['id'] for j in waiting_jobs(restored.jobs)], [front, first, second])
        self.assertEqual(restored.jobs[first]['events'][-1]['message'], 'waiting')
        self.assertFalse((self.root / 'jobs.json').exists())
        self.assertTrue((self.root / 'studio.sqlite3').is_file())

    def test_bad_download_in_worker_preserves_runtime_and_blocks_switch(self):
        first = self.enqueue('bad', release_mode='always')
        other = self.enqueue('other', 'qwen_image_2_1')
        self.fake.bad_hash = True
        self.c.release_policy = app.ReleasePolicy(mode='always')
        self.c._run_queue()
        self.assertEqual(self.c.jobs[first]['status'], 'error')
        self.assertEqual(self.c.jobs[other]['status'], 'queued')
        self.assertIsNotNone(self.c.session)
        self.assertNotIn('release', self.fake.calls)

    def test_empty_queue_does_not_release_failed_download(self):
        key = self.enqueue('bad', release_mode='always')
        self.fake.bad_hash = True
        self.c._run_queue()
        self.assertEqual(self.c.jobs[key]['status'], 'error')
        self.assertIsNotNone(self.c.session)
        self.assertNotIn('release', self.fake.calls)

    def test_failed_switch_keeps_session_and_front_priority(self):
        self.c.session = dict(endpoint='owned-test', app_id='h3', accelerator='A100')
        front = self.enqueue('other', 'qwen_image_2_1', front=True)
        self.fake.release_failure = True
        self.c._run_queue()
        self.assertEqual(self.c.session['app_id'], 'h3')
        self.assertEqual(self.c.jobs[front]['status'], 'queued')
        self.assertTrue(self.c.jobs[front]['queue_front'])
        self.assertIn('release unavailable', self.c.release_error)
        self.assertNotIn('execute', self.fake.calls)

    def test_idle_gpu_switch_uses_dispatcher(self):
        self.c.session = dict(endpoint='owned-test', app_id='h3', accelerator='G4')
        key = self.enqueue('switch', gpu='A100')
        self.c._run_queue()
        self.assertEqual(self.c.jobs[key]['status'], 'done')
        self.assertEqual(self.fake.calls[:2], ['release', 'connect'])

    def test_cancel_running_keeps_environment_for_next_job(self):
        first = self.enqueue('cancel me')
        second = self.enqueue('next')
        execute = self.fake.execute
        def cancel_first(job):
            if job['id'] == first:
                self.c.cancel(first)
                raise app.TaskCancelled()
            execute(job)
        self.fake.execute = cancel_first
        self.c._run_queue()
        self.assertEqual(self.c.jobs[first]['status'], 'cancelled')
        self.assertEqual(self.c.jobs[second]['status'], 'done')
        self.assertNotIn('release', self.fake.calls)
        self.assertIn('cancel', self.fake.calls)

    def test_unconfirmed_cancel_blocks_next_job(self):
        first = self.enqueue('cancel me')
        second = self.enqueue('next')
        self.fake.cancel_failure = True
        def cancel(job):
            self.c.cancel(first)
            raise app.TaskCancelled()
        self.fake.execute = cancel
        self.c._run_queue()
        self.assertEqual(self.c.jobs[first]['status'], 'error')
        self.assertEqual(self.c.jobs[second]['status'], 'queued')
        self.assertIsNotNone(self.c.session)

    def test_unknown_remote_state_does_not_dispatch_or_lose_front(self):
        self.c.session = dict(endpoint='owned-test', app_id='h3', accelerator='A100')
        key = self.enqueue('wait', front=True)
        self.fake.consumer_status = lambda: {'state': 'unknown'}
        self.c._run_queue()
        self.assertEqual(self.c.jobs[key]['status'], 'queued')
        self.assertTrue(self.c.jobs[key]['queue_front'])
        self.assertNotIn('execute', self.fake.calls)

    def test_front_job_can_run_between_batch_children(self):
        batch = self.enqueue('batch', count=3)
        executed = []
        front_id = None
        def run(job):
            nonlocal front_id
            executed.append(job['id'])
            job['status'] = 'done'
            if job['id'] == batch:
                front_id = self.enqueue('front', front=True)
        self.c.run = run
        self.c._run_queue()
        self.assertEqual(executed[1], front_id)
        self.assertEqual(len(executed), 4)
        self.assertTrue(all(j.get('batch_complete') for j in self.c.jobs.values() if j.get('batch_id') == batch))

    def test_store_deletes_jobs_and_their_events_transactionally(self):
        key = self.enqueue('first')
        self.c.event(self.c.jobs[key], 'test', 'persisted')
        self.c.jobs.clear()
        self.c.save_jobs()
        store = JobStore(self.root / 'studio.sqlite3')
        self.assertEqual(store.load_jobs(), {})
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_events').fetchone()[0], 0)

    def test_single_worker_drains_concurrent_submissions(self):
        entered, resume = threading.Event(), threading.Event()
        executed = []
        def run(job):
            executed.append(job['id'])
            entered.set()
            if not resume.wait(3):
                raise RuntimeError('test timed out')
            job['status'] = 'done'
        self.c.run = run
        first = self.c.start(app.H3JobConfig(prompt='first'))
        self.assertTrue(entered.wait(3))
        worker = self.c.worker_thread
        second = self.c.start(app.H3JobConfig(prompt='second'))
        self.assertIs(self.c.worker_thread, worker)
        resume.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(executed, [first, second])

    def test_submission_during_worker_exit_gets_a_new_worker(self):
        executed, done = [], threading.Event()
        def run(job):
            executed.append(job['id'])
            job['status'] = 'done'
            done.set()
        self.c.run = run
        save = self.c.save_release_state
        injected = False
        def enqueue_at_exit():
            nonlocal injected
            save()
            if not injected:
                injected = True
                self.c.start(app.H3JobConfig(prompt='arrived at exit'))
        self.c.save_release_state = enqueue_at_exit
        # Simulate the outgoing worker still being alive to the submitter.
        self.c.worker_thread = threading.current_thread()
        self.c._run_queue()
        self.assertTrue(done.wait(3))
        worker = self.c.worker_thread
        if worker:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(executed), 1)

    def test_recovery_reattaches_only_to_matching_busy_execution(self):
        key = self.enqueue('recover')
        self.c.session = dict(endpoint='owned-test', app_id='h3', accelerator='A100')
        self.c.jobs[key].update(endpoint='owned-test', status='interrupted', consumer_run_id='owned-run')
        self.fake.consumer_status = lambda: {'state': 'busy', 'job_id': key, 'run_id': 'owned-run'}
        with patch.object(app.threading, 'Thread'):
            self.c.start(None, retry=key)
        self.c._run_queue()
        self.assertEqual(self.c.jobs[key]['status'], 'done')
        self.assertNotIn('release', self.fake.calls)

    def test_cancelling_saved_batch_root_targets_the_failed_child(self):
        root = self.enqueue('batch', count=2)
        first, child = sorted(self.c.jobs.values(), key=lambda j: j.get('batch_index', 0))
        first.update(status='done', file='kept.mp4')
        child.update(status='error', execution_started=True, consumer_run_id='child-run', endpoint='owned-test')
        self.c.session = dict(endpoint='owned-test', app_id='h3', accelerator='A100')
        with patch.object(self.fake, 'cancel_job') as cancel:
            self.c.cancel(root)
        cancel.assert_called_once_with(child)
        self.assertEqual(first['status'], 'done')
        self.assertEqual(child['status'], 'cancelled')

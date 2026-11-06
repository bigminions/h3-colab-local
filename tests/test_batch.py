"""Independent sampling and queue dispatch between batch items; no Colab GPU allocation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pydantic import ValidationError
import app
from apps.h3.remote_h3 import build_graph
from tests.test_release_policy import CheckedTransport


class BatchTransport(CheckedTransport):
    def __init__(self, control):
        super().__init__(control)
        self.allocations = 0
        self.executed = []
        self.fail_index = None
        self.fail_upload_index = None
        self.before_execute = lambda job: None

    def connect(self, config):
        self.allocations += self.control.session is None
        super().connect(config)

    def execute(self, job):
        self.before_execute(job)
        self.executed.append((job['id'], job['config']['seed']))
        if job['batch_index'] == self.fail_index:
            job['safe_release'] = True
            raise RuntimeError('definite test failure')
        self.data = str(job['config']['seed']).encode()
        super().execute(job)

    def upload_drive(self, job):
        self.calls.append('upload')
        if job['batch_index'] == self.fail_upload_index:
            raise RuntimeError('test upload failure')
        return {'provider': 'drive', 'file_id': job['id'], 'job_id': job['id'], 'size': len(self.data),
                'sha256': job['remote_result']['sha256']}


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.path = Path(self.temp.name)
        self.clock = 1000
        self.c = app.Controller(self.path, self.path, now=lambda: self.clock)
        self.fake = BatchTransport(self.c)
        self.c.transport = self.fake

    def tearDown(self):
        self.temp.cleanup()

    def start(self, count=2, mode='always', seed=17):
        with patch.object(app.threading, 'Thread'):
            first = self.c.start(app.H3JobConfig(prompt='test', count=count, seed=seed, release_mode=mode))
        return sorted((j for j in self.c.jobs.values() if j.get('batch_id') == first), key=lambda j: j['batch_index'])

    def resume(self, job):
        with patch.object(app.threading, 'Thread'):
            first = self.c.start(None, retry=job['id'])
        batch = sorted((j for j in self.c.jobs.values() if j.get('batch_id') == job['batch_id']
                        and not (j.get('file') or j.get('drive'))), key=lambda j: j['batch_index'])
        self.assertEqual(first, batch[0]['id'])
        self.c._run_queue()

    def test_distinct_outputs_reuse_runtime_release_once_after_all_verified(self):
        batch = self.start()
        def observe(job):
            self.assertIsNotNone(self.c.active)
            self.c.idle_tick()
            self.assertNotIn('release', self.fake.calls)
            with self.assertRaises(app.HTTPException): self.c.check_and_release()
            if job['batch_index'] == 2:
                self.assertTrue((self.path / batch[0]['file']).exists())
                self.assertFalse(self.c.public_job(batch[0])['batch']['finished'])
        self.fake.before_execute = observe
        self.c._run_queue()
        self.assertEqual(self.fake.allocations, 1)
        self.assertEqual(self.fake.calls, ['connect','execute','download','connect','execute','download','release'])
        self.assertEqual(len({j['id'] for j in batch}), 2)
        self.assertEqual(len({(self.path / j['file']).read_bytes() for j in batch}), 2)
        for job in batch:
            graph = build_graph({**job['config'], 'job_id': job['id']})
            self.assertEqual(graph['8']['inputs']['noise_seed'], job['config']['seed'])
            self.assertIn(job['id'], graph['16']['inputs']['filename_prefix'])
            self.assertTrue(job['batch_complete'])
        self.assertEqual(self.c.public_job(batch[0])['batch']['completed'], 2)
        self.assertIsNone(self.c.active)

    def test_manual_seed_wrap_stays_distinct_and_count_validates(self):
        batch = self.start(count=8, seed=2**53-1)
        self.assertEqual([j['config']['seed'] for j in batch], [2**53-1,0,1,2,3,4,5,6])
        for value in (0,9,1.5,True,'2'):
            with self.assertRaises(ValidationError): app.H3JobConfig(prompt='test', count=value)
        self.assertEqual(app.H3JobConfig(prompt='test').count, 1)

    def test_idle_timer_begins_after_last_variant_and_manual_keeps_gpu(self):
        for mode in ('idle','manual'):
            with self.subTest(mode=mode):
                self.c.jobs.clear(); self.c.active = None; self.c.session = None; self.fake.calls.clear()
                batch = self.start(mode=mode)
                def observe(job):
                    self.clock += 100
                    self.assertIsNone(self.c.release_status()['deadline'])
                    self.c.idle_tick()
                self.fake.before_execute = observe
                self.c._run_queue()
                self.assertNotIn('release', self.fake.calls)
                if mode == 'idle':
                    self.assertEqual(self.c.release_status()['deadline'], self.clock + 600)
                    self.clock += 600; self.c.idle_tick()
                    self.assertEqual(self.fake.calls[-2:], ['check','release'])
                else:
                    self.clock += 10000; self.c.idle_tick()
                    self.assertNotIn('release', self.fake.calls)

    def test_failure_pauses_remaining_and_retry_preserves_saved_variants(self):
        batch = self.start(count=3)
        self.fake.fail_index = 2
        self.c._run_queue()
        self.assertEqual([j['status'] for j in batch], ['done','error','interrupted'])
        self.assertNotIn('release', self.fake.calls)
        self.c.idle_tick()
        self.assertNotIn('release', self.fake.calls)
        with patch.object(app.threading, 'Thread'):
            extra = self.c.start(app.H3JobConfig(prompt='new', release_mode='always'))
        self.assertEqual(self.c.jobs[extra]['status'], 'queued')
        self.c.cancel(extra)
        self.fake.fail_index = None
        self.resume(batch[2])  # Picking a later waiting variant still resumes the earliest missing one.
        self.assertEqual([seed for _,seed in self.fake.executed], [17,18,18,19])
        self.assertEqual(self.fake.calls.count('release'), 1)
        self.assertTrue(all(j['batch_complete'] for j in batch))

    def test_drive_upload_failure_retries_existing_result_then_releases_after_all_backups(self):
        class Storage:
            enabled = True
            def preflight(self): pass
            def download_link(self, stored): return stored
        self.c.drive_storage = Storage()
        self.c.history_recycle.storage = self.c.drive_storage
        def open_download(stored):
            response = MagicMock(status_code=200)
            response.iter_content.return_value = [str(self.c.jobs[stored['job_id']]['config']['seed']).encode()]
            return response
        self.c.drive_storage.open_download = open_download
        batch = self.start(count=3)
        self.fake.fail_upload_index = 2
        self.c._run_queue()
        self.assertIn('drive', batch[0]); self.assertNotIn('drive', batch[1])
        self.assertNotIn('release', self.fake.calls)
        self.fake.fail_upload_index = None
        self.resume(batch[1])
        self.assertEqual([seed for _,seed in self.fake.executed], [17,18,19])
        self.assertEqual(len({j['drive']['file_id'] for j in batch}), 3)
        self.assertEqual(self.fake.calls.count('release'), 1)
        self.assertTrue(all(j['batch_complete'] for j in batch))

    def test_release_failure_keeps_saved_batch_complete(self):
        batch = self.start()
        self.fake.release_failure = True
        self.c._run_queue()
        self.assertEqual(batch[-1]['status'], 'warning')
        self.assertTrue(all(j.get('file') and j['batch_complete'] for j in batch))
        self.assertIsNotNone(self.c.session)
        self.assertIsNone(self.c.active)

    def test_local_download_failure_does_not_hold_gpu_or_regenerate_batch(self):
        storage = MagicMock(enabled=True)
        storage.download_link.side_effect = lambda stored: stored
        self.c.drive_storage = storage
        self.c.history_recycle.storage = storage
        batch = self.start(count=2)
        fail_first = True
        def download(stored):
            if stored['job_id'] == batch[0]['id'] and fail_first:
                raise OSError('local transfer interrupted')
            if stored['job_id'] == batch[-1]['id']:
                self.assertIsNone(self.c.session)
            response = MagicMock(status_code=200)
            response.iter_content.return_value = [str(self.c.jobs[stored['job_id']]['config']['seed']).encode()]
            return response
        storage.open_download.side_effect = download
        self.c._run_queue()
        self.assertIsNone(self.c.session)
        self.assertEqual(self.fake.calls.count('release'), 1)
        self.assertEqual([job['status'] for job in batch], ['warning', 'done'])
        self.assertEqual(self.c.public_job(batch[0])['batch']['completed'], 1)
        fail_first = False
        self.fake.calls.clear()
        with patch.object(app.threading, 'Thread'):
            self.c.start(None, retry=batch[0]['id'])
        self.c.run(batch[0])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.c.public_job(batch[0])['batch']['completed'], 2)
        self.assertEqual(len(self.fake.executed), 2)

    def test_restart_preserves_batch_members_and_distinct_seeds(self):
        batch = self.start(count=3)
        self.c.session = {'endpoint':'owned-test', 'app_id': 'h3', 'accelerator': 'A100'}
        self.c.save_session()
        for j in batch: j['endpoint'] = 'owned-test'
        self.c.save_jobs()
        restored = app.Controller(self.path, self.path)
        self.assertTrue(all(j['status'] == 'queued' for j in restored.jobs.values()))
        self.assertEqual(len(restored.pending_jobs()), 3)
        restored.transport = BatchTransport(restored)
        with patch.object(app.threading, 'Thread'): restored.start(None, retry=batch[-1]['id'])
        restored._run_queue()
        self.assertEqual([seed for _,seed in restored.transport.executed], [17,18,19])
        self.assertEqual(restored.transport.calls.count('release'), 1)

    def test_restart_after_all_backups_does_not_offer_regeneration(self):
        batch = self.start()
        self.c._run_queue()
        for j in batch: j['batch_complete'] = False
        batch[-1]['status'] = 'running'  # Interrupted while finalizing release/completion.
        self.c.save_jobs()
        restored = app.Controller(self.path, self.path)
        self.assertTrue(all(j['batch_complete'] and j['status'] == 'done' for j in restored.jobs.values()))
        self.assertEqual(restored.pending_jobs(), [])

    def test_history_boundary_keeps_every_variant_selectable(self):
        batch = self.start(count=3)
        for index in range(19):
            self.c.jobs[str(index)] = dict(id=str(index), config={}, events=[], status='done', created=10**12+index)
        with patch.object(app, 'controller', self.c):
            public = app.status()['jobs']
        self.assertTrue({j['id'] for j in batch}.issubset({j['id'] for j in public}))


    def test_drive_download_is_decoupled_from_gpu_execution(self):
        import threading
        storage = MagicMock(enabled=True)
        storage.download_link.side_effect = lambda stored: stored
        self.c.drive_storage = storage
        self.c.history_recycle.storage = storage

        first_download_started = threading.Event()
        second_gpu_started = threading.Event()
        concurrently_executed = []

        def download(stored):
            if stored['job_id'] == batch[0]['id']:
                first_download_started.set()
                second_gpu_started.wait(timeout=5)
            response = MagicMock(status_code=200)
            response.iter_content.return_value = [str(self.c.jobs[stored['job_id']]['config']['seed']).encode()]
            return response

        storage.open_download.side_effect = download
        batch = self.start(count=2)

        def observe(job):
            if job['batch_index'] == 2:
                concurrently_executed.append(first_download_started.is_set())
                second_gpu_started.set()

        self.fake.before_execute = observe
        self.c._run_queue()

        self.assertEqual(concurrently_executed, [True])
        self.assertEqual([j['status'] for j in batch], ['done', 'done'])
        self.assertEqual(len({(self.path / j['file']).read_bytes() for j in batch}), 2)
        self.assertEqual(self.fake.calls.count('release'), 1)


if __name__ == '__main__':
    unittest.main()

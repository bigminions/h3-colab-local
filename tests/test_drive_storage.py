"""Isolated Drive result, failure, recovery and playback checks; no cloud writes."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from starlette.requests import Request
import app
from drive_storage import DriveStorage
from tests.test_app import FakeTransport


class DriveStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = MagicMock(token_path=self.root / 'drive-token.dat', enabled=False, backend='native')
        self.auth.token_path.write_bytes(b'test only')
        self.store = DriveStorage(self.auth)
        self.data = b'video'
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.md5 = hashlib.md5(self.data).hexdigest()
        self.job = dict(id='a'*32, created=1, config=app.JobConfig(prompt='test').model_dump(),
                        status='running', events=[], remote_result=dict(path='/content/ComfyUI/output/test.mp4',
                        size=5, sha256=self.sha, width=640, height=480, duration=5, audio=1))
        self.stored = dict(provider='drive', file_id='file1', job_id=self.job['id'], size=5,
                           sha256=self.sha, md5=self.md5)
        self.info = dict(size='5', md5Checksum=self.md5, sha256Checksum=self.sha,
                         appProperties={'sha256': self.sha}, trashed=False)
        self.c = app.Controller(self.root, self.root, result_drive=self.store)
        self.c.jobs[self.job['id']] = self.job
        self.fake = FakeTransport(self.c); self.c.transport = self.fake
        response = MagicMock(status_code=200)
        response.iter_content.return_value = [self.data]
        stream = patch.object(self.store, 'open_download', return_value=response)
        self.download = stream.start()
        self.addCleanup(stream.stop)

    def test_authorization_selects_drive_independent_of_cache_backend_and_switch(self):
        self.assertTrue(self.store.enabled)
        with patch.object(app, 'controller', self.c):
            self.assertEqual(app.generation_settings()['result_storage'], 'drive')
        self.auth.token_path.unlink()
        self.assertFalse(self.store.enabled)

    def test_preflight_full_drive_blocks_before_allocation(self):
        self.auth.quota.return_value = {'limit': '10', 'usage': '10'}
        with patch.object(app, 'drive_cache', enabled=False):
            with self.assertRaises(app.HTTPException):
                self.c.start(app.JobConfig(prompt='new'))
        self.assertEqual(self.fake.calls, [])

    def test_resume_reuses_id_without_creating_another_file(self):
        self.job['drive_upload_id'] = 'file1'
        with patch.object(self.store, 'request') as request:
            self.assertEqual(self.store.ensure_file(self.job), 'file1')
            request.assert_not_called()
        self.job.pop('drive_upload_id')
        with patch.object(self.store, 'request') as request:
            request.return_value.json.return_value = {'files': [{'id': 'file1'}]}
            self.assertEqual(self.store.ensure_file(self.job), 'file1')
            self.assertEqual(request.call_count, 1)

    def test_create_private_folder_and_file(self):
        with patch.object(self.store, 'request') as request:
            request.side_effect = [MagicMock(json=lambda: {'files': []}),
                                   MagicMock(json=lambda: {'files': []}),
                                   MagicMock(json=lambda: {'id': 'folder1'}),
                                   MagicMock(json=lambda: {'id': 'file1'})]
            self.assertEqual(self.store.ensure_file(self.job), 'file1')
            self.assertEqual(request.call_args.kwargs['json']['parents'], ['folder1'])
            self.assertNotIn('permissions', str(request.call_args_list))

    def test_target_uses_session_uri_without_oauth_token(self):
        self.job['drive_upload_id'] = 'file1'
        with patch.object(self.store, 'request') as request:
            request.return_value.headers = {'Location': 'https://www.googleapis.com/upload/session?upload_id=test'}
            target = self.store.upload_target(self.job)
            self.assertEqual(target['headers']['Content-Length'], '5')
            self.assertNotIn('Authorization', target['headers'])
            self.assertEqual(request.call_args.args[0], 'PATCH')
            request.return_value.headers = {'Location': 'https://attacker.invalid/upload'}
            with self.assertRaises(RuntimeError): self.store.upload_target(self.job)

    def test_integrity_verification_rejects_wrong_size_hash_or_trash(self):
        with patch.object(self.store, 'request') as request:
            request.return_value.json.return_value = self.info
            self.store.confirm_stored(self.stored)
            for field, value in [('size', '0'), ('md5Checksum', 'bad'), ('sha256Checksum', 'bad'),
                                 ('appProperties', {}), ('trashed', True)]:
                with self.subTest(field=field):
                    request.return_value.json.return_value = {**self.info, field: value}
                    with self.assertRaises(RuntimeError): self.store.confirm_stored(self.stored)

    def test_direct_upload_checks_probe_and_confirmation(self):
        transport = app.GoogleTransport(self.c)
        probe = {'size': 5, 'sha256': self.sha, 'md5': self.md5}
        with patch.object(self.store, 'ensure_file', return_value='file1'), \
             patch.object(self.store, 'upload_target', return_value={'file_id': 'file1',
                 'url': 'https://www.googleapis.com/upload/test', 'headers': {'Content-Type': 'video/mp4'}}), \
             patch.object(self.store, 'confirm_stored'):
            with patch.object(transport, 'remote_json', side_effect=[probe, {'ok': True}]) as remote:
                self.assertEqual(transport.upload_drive(self.job)['file_id'], 'file1')
                code = remote.call_args.args[0]
                compile(code, '<remote upload>', 'exec')
                self.assertIn('requests.put', code)
                self.assertNotIn('Authorization', code)
                self.assertNotIn('refresh_token', code)
            with patch.object(transport, 'remote_json', side_effect=[probe, {'ok': False}]):
                with self.assertRaises(RuntimeError): transport.upload_drive(self.job)
            with patch.object(transport, 'remote_json', return_value={**probe, 'sha256': 'bad'}):
                with self.assertRaises(RuntimeError): transport.upload_drive(self.job)

    def test_upload_releases_gpu_before_automatic_local_download(self):
        self.c.configure_release(app.ReleasePolicy(mode='always'))
        self.fake.upload_drive = lambda job: self.fake.calls.append('upload_drive') or self.stored
        def download(stored, target):
            self.assertIsNone(self.c.session)
            self.assertEqual(self.fake.calls[-1], 'release')
            self.fake.calls.append('drive_download')
            target.write_bytes(self.data)
        self.c.history_recycle.download = download
        self.c.run(self.job)
        self.assertEqual(self.fake.calls, ['connect', 'upload_drive', 'release', 'drive_download'])
        self.assertEqual(self.job['status'], 'done')
        self.assertEqual((self.root / self.job['file']).read_bytes(), self.data)
        self.assertEqual(self.c.pending_jobs(), [])
        self.assertFalse(self.c.public_job(self.job)['can_retry'])
        self.assertNotIn('drive_upload_id', self.c.public_job(self.job))

    def test_failure_preserves_gpu_and_retry_reuses_generated_video(self):
        self.c.configure_release(app.ReleasePolicy(mode='always'))
        self.fake.upload_drive = MagicMock(side_effect=RuntimeError('upload unconfirmed'))
        self.c.run(self.job)
        self.assertEqual(self.job['status'], 'error')
        self.assertIsNotNone(self.c.session)
        self.assertEqual(self.c.pending_jobs(), [self.job])
        self.fake.upload_drive = lambda job: self.stored
        self.c.run(self.job)
        self.assertEqual(self.job['status'], 'done')
        self.assertNotIn('execute', self.fake.calls)
        self.assertEqual(self.fake.calls.count('release'), 1)

    def test_history_download_checks_content_and_closes_stream(self):
        self.job.update(status='done', drive=self.stored)
        response = MagicMock(status_code=200)
        response.iter_content.return_value = [self.data]
        with patch.object(self.store, 'open_download', return_value=response):
            result = self.c.load_history_video(self.job['id'])
        self.assertEqual((self.root / result['file']).read_bytes(), self.data)
        response.close.assert_called_once()

    def test_history_corruption_preserves_record(self):
        self.job.update(status='done', drive=self.stored)
        response = MagicMock(status_code=200)
        response.iter_content.return_value = [b'wrong']
        with patch.object(self.store, 'open_download', return_value=response):
            with self.assertRaises(app.HTTPException): self.c.load_history_video(self.job['id'])
        self.assertNotIn('file', self.job)
        self.assertEqual(self.job['drive'], self.stored)
        response.close.assert_called_once()

    def test_manual_release_rechecks_drive_integrity(self):
        self.job.update(status='done', drive=self.stored, endpoint='owned-test')
        self.c.session = {'endpoint': 'owned-test'}
        self.fake.check_idle = lambda: True
        with patch.object(self.store, 'confirm_stored', side_effect=RuntimeError('checksum mismatch')):
            with self.assertRaises(app.HTTPException): self.c.check_and_release()
        self.assertNotIn('release', self.fake.calls)

    def test_recycle_prepares_verified_local_copy_without_deleting_drive(self):
        self.job.update(status='done', drive=self.stored)
        response = MagicMock(status_code=200)
        response.iter_content.return_value = [self.data]
        with patch.object(self.store, 'open_download', return_value=response):
            folder, manifest = self.c.history_recycle.prepare(self.job)
        self.assertEqual((folder / manifest['video_filename']).read_bytes(), self.data)
        self.assertEqual(manifest['job']['drive'], self.stored)

    def test_download_failure_retries_without_gpu_or_regeneration(self):
        self.c.configure_release(app.ReleasePolicy(mode='always'))
        self.fake.upload_drive = lambda job: self.stored
        self.download.side_effect = OSError('download interrupted')
        self.c.run(self.job)
        self.assertIsNone(self.c.session)
        self.assertEqual(self.job['status'], 'warning')
        self.assertNotIn('file', self.job)
        self.assertTrue(self.c.public_job(self.job)['can_retry'])
        self.fake.calls.clear()
        self.download.side_effect = None
        with patch.object(app, 'controller', self.c), patch.object(app.threading, 'Thread'), \
             patch.object(self.store, 'preflight', side_effect=AssertionError('No GPU preflight on download retry')):
            self.assertEqual(app.retry(self.job['id'])['id'], self.job['id'])
        self.c.run(self.job)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.job['status'], 'done')
        self.assertNotIn('local_download_error', self.job)
        self.assertEqual((self.root / self.job['file']).read_bytes(), self.data)

    def test_released_gpu_does_not_block_download_when_release_confirmation_failed(self):
        self.c.configure_release(app.ReleasePolicy(mode='always'))
        self.fake.upload_drive = lambda job: self.stored
        self.fake.release_failure = True
        self.c.run(self.job)
        self.assertEqual(self.job['status'], 'warning')
        self.assertIn('gpu_release_error', self.job)
        self.assertEqual((self.root / self.job['file']).read_bytes(), self.data)

    def test_corrupt_local_copy_is_repaired_and_repeated_reads_stay_local(self):
        self.job.update(status='done', drive=self.stored, file=self.job['id'] + '.mp4')
        target = self.root / self.job['file']
        target.write_bytes(b'wrong')
        self.c.load_history_video(self.job['id'])
        self.assertEqual(target.read_bytes(), self.data)
        self.download.assert_called_once()
        self.c.load_history_video(self.job['id'])
        self.download.assert_called_once()
        # Detect later same-length changes after an earlier successful verification.
        target.write_bytes(b'other')
        self.c.load_history_video(self.job['id'])
        self.assertEqual(self.download.call_count, 2)
        self.assertEqual(target.read_bytes(), self.data)

    def test_failed_repair_never_replaces_existing_file_with_partial_data(self):
        self.job.update(status='done', drive=self.stored, file=self.job['id'] + '.mp4')
        target = self.root / self.job['file']
        target.write_bytes(b'broken')
        self.download.return_value.iter_content.return_value = [b'partial']
        with self.assertRaises(app.HTTPException): self.c.load_history_video(self.job['id'])
        self.assertEqual(target.read_bytes(), b'broken')
        self.assertEqual(list(self.root.glob('*.partial')), [])
        self.assertEqual(self.job['drive'], self.stored)


    def test_drive_download_in_progress_exposes_downloading_state_and_allows_active_gpu_retry(self):
        self.job.update(status='warning', drive=self.stored, local_download_error='network timeout')
        self.c.active = 'another-job-on-gpu'
        public = self.c.public_job(self.job)
        self.assertTrue(public['can_retry'])
        self.assertFalse(public['is_preparing'])
        self.assertFalse(public['is_downloading'])

        # While downloading is in flight:
        self.c.preparing_videos.add(self.job['id'])
        try:
            public_downloading = self.c.public_job(self.job)
            self.assertTrue(public_downloading['is_preparing'])
            self.assertTrue(public_downloading['is_downloading'])
            self.assertFalse(public_downloading['can_retry'])
        finally:
            self.c.preparing_videos.discard(self.job['id'])

    def test_load_history_video_waits_for_active_download_thread(self):
        self.job.update(status='done', drive=self.stored)
        target = self.root / (self.job['id'] + '.mp4')
        download_barrier = app.threading.Event()

        def slow_download(stored, partial):
            download_barrier.wait(5)
            partial.write_bytes(self.data)

        with patch.object(self.c.history_recycle, 'download', side_effect=slow_download):
            thread = self.c.start_drive_download(self.job)
            app.time.sleep(0.05)
            download_barrier.set()
            result = self.c.load_history_video(self.job['id'])
            self.assertTrue(result['ok'])
            self.assertEqual(result['file'], self.job['id'] + '.mp4')
            self.assertEqual(target.read_bytes(), self.data)
            self.c.wait_drive_downloads([self.job['id']])


if __name__ == '__main__':
    unittest.main()

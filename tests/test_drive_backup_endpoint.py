import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

import app


class DriveBackupEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_drive_backup_requires_google_and_drive_auth(self):
        with patch.object(app, 'PRIVATE', self.root):
            with self.assertRaises(HTTPException) as cm:
                app.trigger_drive_backup()
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn('Google', cm.exception.detail)

            (self.root / 'google-token.dat').write_bytes(b'google-token')
            mock_cache = MagicMock()
            mock_cache.token_path = self.root / 'nonexistent-token.dat'
            mock_cache.backend = 'api'
            with patch.object(app, 'drive_cache', mock_cache):
                with self.assertRaises(HTTPException) as cm2:
                    app.trigger_drive_backup()
                self.assertEqual(cm2.exception.status_code, 400)
                self.assertIn('Drive', cm2.exception.detail)

    def test_drive_backup_rejects_when_busy_or_already_running(self):
        (self.root / 'google-token.dat').write_bytes(b'google-token')
        mock_cache = MagicMock()
        mock_cache.token_path = self.root / 'drive-token.dat'
        mock_cache.token_path.write_bytes(b'drive-token')
        mock_cache.backend = 'api'

        with patch.object(app, 'PRIVATE', self.root), patch.object(app, 'drive_cache', mock_cache):
            with patch.object(app.controller, 'active', 'job-123'):
                with self.assertRaises(HTTPException) as cm:
                    app.trigger_drive_backup()
                self.assertEqual(cm.exception.status_code, 400)

            (self.root / 'drive-cache-progress.json').write_text(json.dumps({
                'running': True, 'message': 'backup in progress', 'updated_at': app.time.time()
            }))
            with patch.object(app.controller, 'active', None):
                with self.assertRaises(HTTPException) as cm:
                    app.trigger_drive_backup()
                self.assertEqual(cm.exception.status_code, 400)

    def test_job_failure_flags_drive_backup_needed(self):
        c = app.Controller(self.root, self.root)
        c.configure_release(app.ReleasePolicy(mode='always'))
        job = {
            'id': 'test-fail-job',
            'created': 1000,
            'status': 'running',
            'config': {'prompt': 'test'},
            'events': []
        }
        c.jobs[job['id']] = job

        class FailingTransport:
            def connect(self, config):
                c.session = {'endpoint': 'owned-test'}
            def execute(self, j):
                raise RuntimeError('模型 model.safetensors 从 Hugging Face 下载失败，且 Drive 备用缓存未命中。可在“账号与连接”中点击【自动备份模型到 Drive】申请低成本算力完成离线备份。')
            def release(self): pass

        with patch.object(c, 'transport', FailingTransport()):
            c.run(job)

        self.assertEqual(job['status'], 'error')
        self.assertTrue(job.get('drive_backup_needed'))

    def test_drive_backup_dispatches_to_h3_application(self):
        (self.root / 'google-token.dat').touch()
        mock_cache = MagicMock(backend='native')
        with patch.object(app, 'PRIVATE', self.root), patch.object(app, 'drive_cache', mock_cache), \
                patch.object(app.controller, 'active', None), \
                patch.object(app.threading, 'Thread') as thread, \
                patch('apps.h3.seed_drive_cache.run_backup') as backup:
            result = app.trigger_drive_backup()
            self.assertTrue(result['ok'])
            thread.return_value.start.assert_called_once_with()
            thread.call_args.kwargs['target']()
            backup.assert_called_once_with(gpu_type='T4')


if __name__ == '__main__':
    unittest.main()

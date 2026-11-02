"""Folder opening uses isolated files and a mocked Explorer launcher."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

import app


class HistoryFolderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.root = Path(self.temporary.name)
        self.videos = self.root / 'videos'; self.videos.mkdir()
        self.storage = MagicMock(enabled=False)
        self.storage.download_link.side_effect = lambda stored: stored
        self.control = app.Controller(self.root,self.videos,result_drive=self.storage)
        self.control.open_video_directory = MagicMock()
        self.job = dict(id='a'*32,status='done',created=1,events=[],config={'prompt':'test'},file='saved.mp4')
        self.control.jobs[self.job['id']] = self.job
        (self.videos / 'saved.mp4').write_bytes(b'video')

    def tearDown(self):
        self.temporary.cleanup()

    def cloud_only(self, content=b'cloud video'):
        self.job.pop('file',None)
        self.job['drive'] = dict(file_id='video',job_id=self.job['id'],size=len(content),sha256=hashlib.sha256(content).hexdigest())
        self.storage.open_download.return_value = MagicMock(status_code=200)
        self.storage.open_download.return_value.iter_content.return_value = [content]

    def test_local_opens_its_directory_without_cloud_access(self):
        self.assertTrue(self.control.open_history_folder(self.job['id'])['ok'])
        self.control.open_video_directory.assert_called_once_with(self.videos.resolve())
        self.storage.open_download.assert_not_called()
        self.assertEqual((self.videos / 'saved.mp4').read_bytes(),b'video')

    def test_load_history_video_downloads_without_opening_explorer(self):
        self.cloud_only()
        res = self.control.load_history_video(self.job['id'])
        filename = self.job['id'] + '.mp4'
        self.assertTrue(res['ok'])
        self.assertEqual(res['file'], filename)
        self.assertEqual((self.videos / filename).read_bytes(), b'cloud video')
        self.control.open_video_directory.assert_not_called()
        self.assertTrue(self.control.public_job(self.job)['local_file'])

    def test_public_job_reflects_local_and_cloud_states(self):
        self.assertTrue(self.control.public_job(self.job)['local_file'])
        self.cloud_only()
        self.assertFalse(self.control.public_job(self.job)['local_file'])

    def test_cloud_download_is_verified_saved_and_reused(self):
        self.cloud_only()
        self.control.open_history_folder(self.job['id'])
        filename = self.job['id'] + '.mp4'
        self.assertEqual((self.videos / filename).read_bytes(),b'cloud video')
        self.assertEqual(json.loads((self.root / 'jobs.json').read_text(encoding='utf-8'))[self.job['id']]['file'],filename)
        self.control.open_history_folder(self.job['id'])
        self.storage.open_download.assert_called_once_with(self.job['drive'])
        self.assertEqual(self.control.open_video_directory.call_count,2)
        self.assertEqual(list(self.videos.glob('*.partial')),[])

    def test_failed_verification_keeps_record_and_cleans_partial_file(self):
        self.cloud_only(); self.job['drive']['sha256'] = '0'*64
        with self.assertRaises(app.HTTPException): self.control.open_history_folder(self.job['id'])
        self.assertNotIn('file',self.job)
        self.assertFalse(self.control.preparing_videos)
        self.assertEqual(list(self.videos.glob('*.partial')),[])
        self.control.open_video_directory.assert_not_called()

    def test_existing_different_file_is_never_overwritten(self):
        self.cloud_only()
        target = self.videos / (self.job['id'] + '.mp4')
        target.write_bytes(b'other')
        with self.assertRaises(app.HTTPException): self.control.open_history_folder(self.job['id'])
        self.assertEqual(target.read_bytes(),b'other')
        self.control.open_video_directory.assert_not_called()

    def test_missing_local_copy_can_be_retrieved_from_cloud(self):
        self.cloud_only(); self.job['file'] = 'missing.mp4'
        self.control.open_history_folder(self.job['id'])
        self.assertEqual((self.videos / self.job['file']).read_bytes(),b'cloud video')

    def test_cloud_download_blocks_removal_and_duplicate_open_until_finished(self):
        self.cloud_only()
        download = self.control.history_recycle.download
        def guarded_download(stored,target):
            with self.assertRaises(app.HTTPException): self.control.recycle_history(self.job['id'])
            with self.assertRaises(app.HTTPException): self.control.clear_history(self.job['id'])
            with self.assertRaises(app.HTTPException): self.control.open_history_folder(self.job['id'])
            download(stored,target)
        self.control.history_recycle.download = guarded_download
        self.control.open_history_folder(self.job['id'])
        self.assertTrue(self.control.history_clearable(self.job))

    def test_missing_hidden_recycling_and_unsaved_records_do_not_open(self):
        with self.assertRaises(app.HTTPException): self.control.open_history_folder('missing')
        self.job['history_hidden'] = True
        with self.assertRaises(app.HTTPException): self.control.open_history_folder(self.job['id'])
        self.job.pop('history_hidden'); self.control.recycling.add(self.job['id'])
        with self.assertRaises(app.HTTPException): self.control.open_history_folder(self.job['id'])
        self.control.recycling.clear(); self.job.pop('file')
        with self.assertRaises(app.HTTPException): self.control.open_history_folder(self.job['id'])
        self.control.open_video_directory.assert_not_called()

    def test_invalid_local_path_is_rejected(self):
        self.job['file'] = '../outside.mp4'
        with self.assertRaises(app.HTTPException): self.control.open_history_folder(self.job['id'])
        self.control.open_video_directory.assert_not_called()

    def test_explorer_failure_retains_downloaded_video_for_retry(self):
        self.cloud_only()
        self.control.open_video_directory.side_effect = OSError('Explorer unavailable')
        with self.assertRaises(app.HTTPException): self.control.open_history_folder(self.job['id'])
        self.assertEqual((self.videos / self.job['file']).read_bytes(),b'cloud video')
        self.assertFalse(self.control.preparing_videos)
        self.control.open_video_directory.side_effect = None
        self.control.open_history_folder(self.job['id'])
        self.storage.open_download.assert_called_once()


if __name__ == '__main__': unittest.main()

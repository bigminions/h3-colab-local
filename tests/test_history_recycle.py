"""Work recycling, isolated fake bin and cloud streams; never clears user data."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import app


class HistoryRecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.root = Path(self.temporary.name)
        self.videos = self.root / 'videos'; self.videos.mkdir()
        self.bin = self.root / 'fake-bin'; self.bin.mkdir()
        self.storage = MagicMock(enabled=False)
        self.storage.download_link.side_effect = lambda stored: stored
        self.control = app.Controller(self.root,self.videos,result_drive=self.storage)
        self.control.history_recycle.sender = lambda folder:folder.replace(self.bin / folder.name)
        self.job = dict(id='a'*32,created=1,status='done',events=[],file='saved.mp4',
            config=dict(prompt='原始画面与对白',seed=15,aspect_ratio='9:16'))
        self.control.jobs[self.job['id']] = self.job
        (self.videos / 'saved.mp4').write_bytes(b'original-video')

    def tearDown(self):
        self.temporary.cleanup()

    def test_recycle_all_keeps_unsaved_task_records(self):
        failed = dict(id='failed',created=2,status='error',safe_release=True,events=[],config={})
        self.control.jobs['failed'] = failed
        result = self.control.recycle_history()
        self.assertEqual(result['ids'],[self.job['id']])
        self.assertFalse(failed.get('history_hidden'))

    def restored_folder(self):
        folder = next(self.bin.iterdir())
        return folder.replace(self.control.history_recycle.directory / folder.name)

    def test_local_video_and_configuration_restore_after_restart(self):
        result = self.control.recycle_history(self.job['id'])
        self.assertEqual(result['ids'],[self.job['id']]); self.assertFalse(result['errors'])
        self.assertFalse((self.videos / 'saved.mp4').exists())
        folder = next(self.bin.iterdir())
        manifest = json.loads((folder / 'work.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['job']['config'],self.job['config'])
        self.assertEqual((folder / 'saved.mp4').read_bytes(),b'original-video')
        self.assertTrue(self.job['history_hidden'])
        self.restored_folder()
        restarted = app.Controller(self.root,self.videos,result_drive=self.storage)
        restarted.recover_history()
        self.assertFalse(restarted.jobs[self.job['id']].get('history_hidden'))
        self.assertEqual(restarted.jobs[self.job['id']]['config']['seed'],15)
        self.assertEqual((self.videos / 'saved.mp4').read_bytes(),b'original-video')

    def test_failed_bin_operation_returns_original_without_hiding_it(self):
        self.control.history_recycle.sender = MagicMock(side_effect=OSError('bin unavailable'))
        result = self.control.recycle_history(self.job['id'])
        self.assertEqual(result['count'],0); self.assertEqual(len(result['errors']),1)
        self.assertFalse(self.job.get('history_hidden')); self.assertFalse(self.job.get('history_recycle_pending'))
        self.assertEqual((self.videos / 'saved.mp4').read_bytes(),b'original-video')
        self.assertEqual(list(self.control.history_recycle.directory.iterdir()),[])

    def test_cloud_only_video_is_downloaded_verified_and_restored_as_local(self):
        self.job.pop('file'); data = b'cloud-only-video'
        self.job['drive'] = dict(file_id='video',job_id=self.job['id'],size=len(data),sha256=hashlib.sha256(data).hexdigest())
        response = MagicMock(status_code=200)
        response.iter_content.return_value = [data]
        self.storage.open_download.return_value = response
        result = self.control.recycle_history(self.job['id'])
        self.assertEqual(result['count'],1); response.close.assert_called_once()
        self.assertEqual((next(self.bin.iterdir()) / (self.job['id']+'.mp4')).read_bytes(),data)
        self.storage.request.assert_not_called()
        self.restored_folder(); self.control.recover_history()
        self.assertEqual((self.videos / self.control.jobs[self.job['id']]['file']).read_bytes(),data)

    def test_corrupt_cloud_download_never_hides_or_recycles(self):
        self.job.pop('file')
        self.job['drive'] = dict(file_id='video',job_id=self.job['id'],size=9,sha256='a'*64)
        self.storage.open_download.return_value = MagicMock(status_code=200)
        self.storage.open_download.return_value.iter_content.return_value = [b'bad']
        sender = MagicMock(); self.control.history_recycle.sender = sender
        result = self.control.recycle_history(self.job['id'])
        self.assertEqual(result['count'],0); sender.assert_not_called()
        self.assertFalse(self.job.get('history_hidden'))

    def test_shared_local_video_survives_until_last_record_is_recycled(self):
        other = {**self.job,'id':'b'*32}; self.control.jobs[other['id']] = other
        self.assertEqual(self.control.recycle_history(self.job['id'])['count'],1)
        self.assertTrue((self.videos / 'saved.mp4').exists())
        self.assertEqual(self.control.recycle_history(other['id'])['count'],1)
        self.assertFalse((self.videos / 'saved.mp4').exists())
        self.assertEqual(len(list(self.bin.iterdir())),2)

    def test_active_pending_jobs_and_duplicate_requests_are_protected(self):
        self.control.active = self.job['id']
        with self.assertRaises(app.HTTPException): self.control.recycle_history(self.job['id'])
        self.assertTrue((self.videos / 'saved.mp4').exists())
        self.control.active = None
        self.control.recycle_history(self.job['id'])
        self.assertEqual(self.control.recycle_history(self.job['id'])['count'],0)
        self.assertEqual(len(list(self.bin.iterdir())),1)

    def test_pending_journal_recovers_both_sides_of_system_operation(self):
        folder,manifest = self.control.history_recycle.prepare(self.job)
        self.job['history_recycle_pending'] = dict(folder=folder.name,token=manifest['token'])
        self.control.save_jobs(); self.control.recover_history()
        self.assertTrue((self.videos / 'saved.mp4').exists()); self.assertFalse(self.job.get('history_hidden'))
        folder,manifest = self.control.history_recycle.prepare(self.job)
        self.job['history_recycle_pending'] = dict(folder=folder.name,token=manifest['token'])
        self.control.history_recycle.send(folder)
        self.control.recover_history()
        self.assertTrue(self.job['history_hidden'])
        self.restored_folder(); self.control.recover_history()
        self.assertTrue((self.videos / 'saved.mp4').exists())

    def test_restore_does_not_overwrite_an_existing_different_video(self):
        self.control.recycle_history(self.job['id']); folder = self.restored_folder()
        (self.videos / 'saved.mp4').write_bytes(b'new-unrelated-video')
        self.control.recover_history()
        self.assertTrue(self.job['history_hidden']); self.assertTrue(folder.exists())
        self.assertEqual((self.videos / 'saved.mp4').read_bytes(),b'new-unrelated-video')

    def test_recovery_rejects_paths_outside_work_directories(self):
        with self.assertRaises(ValueError): self.control.history_recycle.video('../secret.mp4')
        with self.assertRaises(ValueError): self.control.history_recycle.folder('../H3-secret')

    def test_new_route_uses_recycling_and_status_detects_restore(self):
        with patch.object(app,'controller',self.control):
            result = app.recycle_history(app.HistoryRecycleRequest(job_id=self.job['id']))
            self.assertEqual(result['count'],1); self.assertEqual(app.status()['jobs'],[])
            self.restored_folder()
            self.assertEqual(len(app.status()['jobs']),1)

    def test_bulk_partial_failure_retains_failed_work_and_protects_legacy_restore(self):
        other = {**self.job,'id':'b'*32}; self.control.jobs[other['id']] = other
        sender = self.control.history_recycle.sender
        def partial(folder):
            manifest = json.loads((folder/'work.json').read_text(encoding='utf-8'))
            if manifest['job']['id'] == other['id']: raise OSError('test failure')
            sender(folder)
        self.control.history_recycle.sender = partial
        result = self.control.recycle_history()
        self.assertEqual(result['ids'],[self.job['id']]); self.assertEqual(len(result['errors']),1)
        self.assertFalse(other.get('history_hidden'))
        self.assertEqual((self.videos/'saved.mp4').read_bytes(),b'original-video')
        with self.assertRaises(app.HTTPException): self.control.restore_history([self.job['id']])

    def test_damaged_restored_video_is_kept_for_inspection(self):
        self.control.recycle_history(self.job['id']); folder = self.restored_folder()
        (folder/'saved.mp4').write_bytes(b'damaged')
        self.control.recover_history()
        self.assertTrue(self.job['history_hidden']); self.assertTrue(folder.exists())
        self.assertFalse((self.videos/'saved.mp4').exists())

    def test_interrupted_prepare_can_return_local_video_without_a_pending_journal(self):
        self.control.history_recycle.prepare(self.job)
        self.assertFalse((self.videos/'saved.mp4').exists())
        self.control.recover_history()
        self.assertEqual((self.videos/'saved.mp4').read_bytes(),b'original-video')
        self.assertFalse(self.job.get('history_hidden'))



    def test_recycle_batch_clears_all_siblings(self):
        batch_id = 'batch-1'
        child1 = dict(id='child-1', created=2, status='done', events=[], file='v1.mp4', batch_id=batch_id, batch_index=1, batch_total=2, config={})
        child2 = dict(id='child-2', created=3, status='done', events=[], file='v2.mp4', batch_id=batch_id, batch_index=2, batch_total=2, config={})
        (self.videos / 'v1.mp4').write_bytes(b'v1')
        (self.videos / 'v2.mp4').write_bytes(b'v2')
        self.control.jobs['child-1'] = child1
        self.control.jobs['child-2'] = child2

        # Recycle using batch_id
        result = self.control.recycle_history(batch_id)
        self.assertEqual(set(result['ids']), {'child-1', 'child-2'})
        self.assertTrue(child1.get('history_hidden'))
        self.assertTrue(child2.get('history_hidden'))

        # Also verify recycling via one of the sibling IDs recycles the entire batch
        child3 = dict(id='child-3', created=4, status='done', events=[], file='v3.mp4', batch_id='batch-2', batch_index=1, batch_total=2, config={})
        child4 = dict(id='child-4', created=5, status='done', events=[], file='v4.mp4', batch_id='batch-2', batch_index=2, batch_total=2, config={})
        (self.videos / 'v3.mp4').write_bytes(b'v3')
        (self.videos / 'v4.mp4').write_bytes(b'v4')
        self.control.jobs['child-3'] = child3
        self.control.jobs['child-4'] = child4
        res2 = self.control.recycle_history('child-3')
        self.assertEqual(set(res2['ids']), {'child-3', 'child-4'})
        self.assertTrue(child3.get('history_hidden'))
        self.assertTrue(child4.get('history_hidden'))


if __name__ == '__main__': unittest.main()

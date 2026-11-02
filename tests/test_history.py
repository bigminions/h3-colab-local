import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError
import app


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.root = Path(self.temp.name)
        self.assertTrue(self.root.resolve().is_relative_to(app.PRIVATE.resolve()))
        self.videos = self.root / 'videos'; self.videos.mkdir()
        self.control = app.Controller(self.root, self.videos)
        self.job = dict(id='finished', created=1, status='done', events=[], config={},
                        file='saved.mp4', drive=dict(file_id='saved-video',job_id='finished',sha256='hash'))
        self.control.jobs['finished'] = copy.deepcopy(self.job)
        (self.videos / 'saved.mp4').write_bytes(b'preserved video')

    def tearDown(self):
        self.temp.cleanup()

    def test_single_clear_is_persistent_and_keeps_saved_assets(self):
        result = self.control.clear_history('finished')
        self.assertEqual(result['ids'], ['finished'])
        self.assertEqual(self.control.jobs['finished']['drive'], self.job['drive'])
        self.assertEqual((self.videos / 'saved.mp4').read_bytes(), b'preserved video')
        restarted = app.Controller(self.root, self.videos)
        self.assertTrue(restarted.jobs['finished']['history_hidden'])
        self.assertEqual(restarted.restore_history(['finished','finished'])['count'], 1)
        self.assertFalse(restarted.jobs['finished'].get('history_hidden'))

    def test_clear_all_hides_older_records_before_recent_limit(self):
        self.control.jobs = {str(i):dict(id=str(i),created=i,status='done',events=[],config={},file='saved.mp4') for i in range(25)}
        with patch.object(app,'controller',self.control):
            self.assertEqual(len(app.status()['jobs']),20)
            self.assertEqual(app.clear_history()['count'],25)
            self.assertEqual(app.status()['jobs'],[])
            self.assertEqual(app.status()['history'],dict(count=0,clearable=0))
            app.restore_history(app.HistoryRestore(ids=['0','1']))
            self.assertEqual([j['id'] for j in app.status()['jobs']],['1','0'])

    def test_processing_pending_and_entire_active_batch_are_protected(self):
        for key,status in [('busy','running'),('next','queued'),('unsaved','error'),('interrupted','interrupted')]:
            self.control.jobs[key] = dict(id=key,created=2,status=status,events=[],config={})
        self.control.jobs['safe-error'] = dict(id='safe-error',created=3,status='error',safe_release=True,events=[],config={})
        self.control.jobs['finished'].update(batch_id='batch',batch_index=1,batch_total=2)
        self.control.jobs['busy'].update(batch_id='batch',batch_index=2,batch_total=2)
        self.control.active='busy'
        before_pending = [j['id'] for j in self.control.pending_jobs()]
        for key in ('finished','busy','next','unsaved','interrupted'):
            with self.assertRaises(app.HTTPException) as caught:self.control.clear_history(key)
            self.assertEqual(caught.exception.status_code,409)
            self.assertFalse(self.control.public_job(self.control.jobs[key])['can_clear_history'])
        self.assertEqual(self.control.clear_history()['ids'],['safe-error'])
        self.assertEqual([j['id'] for j in self.control.pending_jobs()],before_pending)

    def test_missing_id_and_restore_are_atomic(self):
        with self.assertRaises(app.HTTPException):self.control.clear_history('unknown')
        self.control.clear_history('finished')
        with self.assertRaises(app.HTTPException):self.control.restore_history(['finished','unknown'])
        self.assertTrue(self.control.jobs['finished']['history_hidden'])

    def test_unsaved_tasks_do_not_displace_saved_history_or_count_as_works(self):
        for i in range(25):
            self.control.jobs[str(i)] = dict(id=str(i),created=i+2,status='error',
                                            safe_release=True,events=[],config={})
        with patch.object(app,'controller',self.control):
            state = app.status()
        self.assertEqual(state['history'],dict(count=1,clearable=1))
        ids = {job['id'] for job in state['jobs']}
        self.assertIn('finished',ids)
        self.assertIn('24',ids)
        self.assertEqual(len(ids),21)

    def test_routes_and_empty_restore_validation(self):
        with patch.object(app,'controller',self.control):
            self.assertEqual(app.clear_history_item('finished')['count'],1)
            self.assertEqual(app.status()['jobs'],[])
            with self.assertRaises(ValidationError):app.HistoryRestore(ids=[])
            self.assertEqual(app.restore_history(app.HistoryRestore(ids=['finished']))['count'],1)
            self.assertEqual(app.clear_history()['count'],1)


if __name__ == '__main__':
    unittest.main()

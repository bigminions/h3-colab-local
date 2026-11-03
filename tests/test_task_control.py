"""Automatic reconnect and cancellation; isolated transports, no cloud calls."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import app
from tests.test_app import FakeTransport


class TaskControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.control = app.Controller(Path(self.temp.name),Path(self.temp.name))
        self.control.cancel_wakeup.wait = MagicMock()
        self.job = dict(id='a'*32,config=app.H3JobConfig(prompt='test').model_dump(),
                        status='running',events=[],created=0)
        self.control.jobs[self.job['id']] = self.job
        self.control.active = self.job['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_disconnect_reconnects_same_job_then_saves_and_releases(self):
        self.control.configure_release(app.ReleasePolicy(mode='always'))
        fake = FakeTransport(self.control)
        original = fake.execute
        calls = []
        def execute(job):
            calls.append(job['id'])
            if len(calls)==1:raise RuntimeError('Connection was lost.')
            original(job)
        fake.execute=execute
        self.control.transport=fake
        self.control.run(self.job)
        self.assertEqual(calls,[self.job['id']]*2)
        self.assertEqual(self.job['status'],'done')
        self.assertEqual(self.job['reconnect_attempts'],1)
        self.assertEqual(fake.calls.count('connect'),1)
        self.assertEqual(fake.calls[-1],'release')

    def test_three_failed_reconnects_cancel_and_free_runtime(self):
        fake=FakeTransport(self.control)
        fake.execute=MagicMock(side_effect=RuntimeError('Connection was lost.'))
        self.control.transport=fake
        self.control.run(self.job)
        self.assertEqual(fake.execute.call_count,4)
        self.assertEqual(self.job['status'],'cancelled')
        self.assertEqual(self.job['reconnect_attempts'],3)
        self.assertIsNone(self.control.active)
        self.assertIsNone(self.control.session)
        self.assertEqual(self.control.pending_jobs(),[])

    def test_non_connection_error_is_not_retried_or_auto_cancelled(self):
        fake=FakeTransport(self.control,failure=True)
        fake.execute=MagicMock(side_effect=ValueError('invalid prompt'))
        self.control.transport=fake
        self.control.run(self.job)
        fake.execute.assert_called_once()
        self.assertEqual(self.job['status'],'error')
        self.assertNotIn('cancel_requested',self.job)

    def test_user_cancels_during_connect_before_any_execution(self):
        fake=FakeTransport(self.control)
        def connect(config):
            self.control.cancel(self.job['id'])
            fake.calls.append('connect')
            self.control.session={'endpoint':'owned-test'}
        fake.connect=connect
        fake.stop_running=MagicMock()
        self.control.transport=fake
        self.control.run(self.job)
        self.assertEqual(fake.calls,['connect','release'])
        self.assertEqual(self.job['status'],'cancelled')
        fake.stop_running.assert_called_once()

    def test_cancel_stops_progress_client_then_confirms_release(self):
        fake=FakeTransport(self.control)
        stopped=threading.Event()
        fake.stop_running=stopped.set
        def execute(job):
            self.assertTrue(self.control.public_job(job)['can_cancel'])
            self.control.cancel(job['id'])
            self.assertTrue(stopped.is_set())
            raise RuntimeError('Connection was lost.')
        fake.execute=execute
        self.control.transport=fake
        self.control.run(self.job)
        self.assertEqual(self.job['status'],'cancelled')
        self.assertIsNone(self.control.active)
        self.assertNotIn('download',fake.calls)
        self.assertEqual(fake.calls.count('release'),1)

    def test_cancel_release_failure_stays_blocked_with_retry_cancel(self):
        fake=FakeTransport(self.control,release_failure=True)
        self.control.transport=fake
        self.control.session={'endpoint':'owned-test'}
        self.job.update(endpoint='owned-test',status='error')
        self.control.active=None
        self.control.cancel(self.job['id'])
        self.assertEqual(self.job['status'],'error')
        self.assertTrue(self.job['cancel_error'])
        self.assertEqual(self.control.pending_jobs(),[self.job])
        public=self.control.public_job(self.job)
        self.assertTrue(public['can_cancel']);self.assertFalse(public['can_retry'])
        fake.release_failure=False
        self.control.cancel(self.job['id'])
        self.assertEqual(self.job['status'],'cancelled')
        self.assertEqual(self.control.pending_jobs(),[])

    def test_cancel_batch_keeps_saved_video_and_cancels_remaining(self):
        fake=FakeTransport(self.control)
        self.control.transport=fake
        self.control.session={'endpoint':'owned-test'}
        self.control.active=None
        self.job.update(endpoint='owned-test',status='error',batch_id='batch')
        saved=dict(id='b'*32,status='done',file='kept.mp4',batch_id='batch',events=[],config={})
        queued=dict(id='c'*32,status='queued',batch_id='batch',events=[],config={})
        self.control.jobs.update({saved['id']:saved,queued['id']:queued})
        self.control.cancel(self.job['id'])
        self.assertEqual(saved['status'],'done');self.assertEqual(saved['file'],'kept.mp4')
        self.assertEqual(queued['status'],'cancelled')
        self.assertTrue(queued['batch_complete'])

    def test_cancel_of_another_task_cannot_release_active_gpu(self):
        self.control.active='other-task'
        with self.assertRaises(app.HTTPException):self.control.cancel(self.job['id'])

    def test_cancel_unknown_assignment_probes_without_allocating(self):
        self.control.active=None
        self.control.transport=app.ColabProvider(self.control, app.runtime_services)
        self.job.update(status='error',assignment_pending=True,assignment_hash=self.job['id'])
        client=MagicMock()
        client._get_assignment.return_value=type('Proposal',(),{'endpoint':None})()
        self.control.transport.client=lambda:client
        self.control.cancel(self.job['id'])
        client.assign.assert_not_called();client.unassign.assert_not_called()
        self.assertEqual(self.job['status'],'cancelled')
        self.assertFalse(self.job['assignment_pending'])

    def test_cancel_unconfirmed_assignment_releases_only_its_notebook(self):
        self.control.active=None
        self.control.transport=app.ColabProvider(self.control, app.runtime_services)
        self.job.update(status='error',assignment_pending=True,assignment_hash=self.job['id'])
        endpoint=type('Assigned',(),{'endpoint':'owned-test'})()
        client=MagicMock();client._get_assignment.return_value=endpoint
        client.list_assignments.side_effect=[[endpoint],[]]
        self.control.transport.client=lambda:client
        self.control.cancel(self.job['id'])
        client.assign.assert_not_called();client.unassign.assert_called_once_with('owned-test')
        self.assertEqual(self.job['status'],'cancelled')

    def test_connect_retry_reuses_same_notebook_identity(self):
        import requests
        transport=app.ColabProvider(self.control, app.runtime_services)
        client=MagicMock();client.list_assignments.return_value=[]
        assignment=type('Assigned',(),{'endpoint':'owned-test','runtime_proxy_info':
            type('Proxy',(),{'url':'https://example.invalid','token':'test-only'})()})()
        client.assign.side_effect=[requests.ConnectionError('offline'),assignment]
        transport.client=lambda:client
        self.control.connected_operation(self.job,lambda:transport.connect(app.H3JobConfig(prompt='test',gpu='G4')))
        self.assertEqual(client.assign.call_args_list[0].args[0],client.assign.call_args_list[1].args[0])
        self.assertFalse(self.job['assignment_pending'])

    def test_resubmit_unfinished_task_requeues_and_validates(self):
        self.control.active = None
        self.job.update(status='error', stage='connect', message='连接失败')
        self.assertTrue(self.control.public_job(self.job)['can_resubmit'])

        # Resubmit updates job state and adds queue event
        with patch.object(threading, 'Thread') as mock_thread:
            res = self.control.resubmit(self.job['id'])
            self.assertEqual(res, {'ok': True, 'id': self.job['id']})
            mock_thread.assert_called_once()

        self.assertEqual(self.job['status'], 'queued')
        self.assertEqual(self.job['stage'], 'connect')
        self.assertFalse(self.control.public_job(self.job)['can_resubmit'])
        self.assertEqual(self.job['events'][-1]['message'], '已重新提交任务，加入排队')

        # Already queued -> 409
        with self.assertRaises(app.HTTPException) as ctx:
            self.control.resubmit(self.job['id'])
        self.assertEqual(ctx.exception.status_code, 409)

        # Non-existent job -> 404
        with self.assertRaises(app.HTTPException) as ctx:
            self.control.resubmit('non_existent_id')
        self.assertEqual(ctx.exception.status_code, 404)

        # Completed job -> 400
        self.job['status'] = 'done'
        self.job['file'] = 'test.mp4'
        with self.assertRaises(app.HTTPException) as ctx:
            self.control.resubmit(self.job['id'])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_connect_failure_marks_safe_release_and_does_not_block_new_jobs(self):
        fake = FakeTransport(self.control)
        fake.connect = MagicMock(side_effect=RuntimeError('OAuth connection timeout'))
        self.control.transport = fake
        self.control.session = None
        self.job.update(status='running', endpoint=None)
        self.control.active = self.job['id']
        self.control.run(self.job)

        self.assertEqual(self.job['status'], 'error')
        self.assertTrue(self.job['safe_release'])
        self.assertNotIn(self.job, self.control.pending_jobs())
        self.assertIn('尚未连接显卡', self.job['message'])

        # Submitting a new job should not be blocked by this failed job
        with patch.object(threading, 'Thread'):
            new_id = self.control.start(app.H3JobConfig(prompt='new job'))
            self.assertIsNotNone(new_id)

    def test_remote_runtime_recycled_clears_pending_and_releases_session(self):
        self.control.session = {'endpoint': 'stale-endpoint'}
        self.job.update(status='error', endpoint='stale-endpoint', safe_release=False)
        self.assertIn(self.job, self.control.pending_jobs())

        self.control.on_runtime_recycled('stale-endpoint')
        self.assertIsNone(self.control.session)
        self.assertTrue(self.job['safe_release'])
        self.assertNotIn(self.job, self.control.pending_jobs())
        self.assertIn('原 Colab 运行时已离线', self.job['message'])


if __name__ == '__main__':
    unittest.main()

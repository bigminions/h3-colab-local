"""Release-policy safety checks with an injected clock and fake cloud transport."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pydantic import ValidationError
import app
from tests.test_app import FakeTransport


class CheckedTransport(FakeTransport):
    def __init__(self, control, **kwargs):
        super().__init__(control, **kwargs)
        self.busy = False
        self.check_failure = False
    def check_idle(self):
        self.calls.append('check')
        if self.check_failure:
            raise RuntimeError('Cannot confirm remote state')
        return not self.busy


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.path = Path(self.temp.name)
        self.clock = 1000
        self.c = app.Controller(self.path,self.path,now=lambda:self.clock)
        self.fake = CheckedTransport(self.c)
        self.c.transport = self.fake
    def tearDown(self):
        self.c.monitor_stop.set()
        self.temp.cleanup()
    def finish(self, mode='idle', minutes=5):
        self.c.configure_release(app.ReleasePolicy(mode=mode,idle_minutes=minutes))
        config = app.JobConfig(prompt='test',release_mode=mode,idle_minutes=minutes).model_dump()
        job = dict(id='a'*32,config=config,events=[],status='running',created=self.clock)
        self.c.jobs[job['id']] = job; self.c.active = job['id']; self.c.run(job)
        return job
    def test_default_releases_only_after_verified_download(self):
        job = self.finish('always')
        self.assertEqual(self.fake.calls,['connect','execute','download','release'])
        self.assertEqual(job['status'],'done'); self.assertIsNone(self.c.session)
    def test_idle_deadline_not_before_5_minutes_then_checks_and_releases(self):
        job = self.finish()
        self.assertEqual(job['status'],'done'); self.assertIsNotNone(self.c.session)
        self.assertEqual(self.c.release_status()['deadline'],1300)
        self.clock=1299; self.c.idle_tick(); self.assertNotIn('release',self.fake.calls)
        self.clock=1300; self.c.idle_tick(); self.assertEqual(self.fake.calls[-2:],['check','release'])
        self.assertIsNone(self.c.session); self.assertIsNone(self.c.release_status()['deadline'])
    def test_running_job_cancels_timeout_and_completion_restarts_it(self):
        self.finish(); self.clock=1290
        with patch.object(app.threading,'Thread'):
            job_id = self.c.start(app.JobConfig(prompt='next',release_mode='idle'))
        self.assertIsNone(self.c.release_status()['deadline'])
        self.clock=1400; self.c.idle_tick(); self.assertNotIn('release',self.fake.calls)
        self.c.run(self.c.jobs[job_id]); self.assertEqual(self.c.release_status()['deadline'],2000)
    def test_manual_never_times_out_and_button_checks_before_release(self):
        self.finish('manual'); self.clock += 100000
        self.c.idle_tick(); self.assertNotIn('release',self.fake.calls)
        self.c.check_and_release(); self.assertEqual(self.fake.calls[-2:],['check','release'])
    def test_remote_busy_keeps_gpu_and_avoids_rapid_retries(self):
        self.finish(); self.fake.busy=True; self.clock=1300
        self.c.idle_tick(); self.assertNotIn('release',self.fake.calls)
        count=self.fake.calls.count('check'); self.clock=1310; self.c.idle_tick()
        self.assertEqual(self.fake.calls.count('check'),count)
        self.fake.busy=False; self.clock=1360; self.c.idle_tick(); self.assertIsNone(self.c.session)
    def test_manual_busy_and_unconfirmed_network_state_keep_gpu(self):
        self.finish('manual'); self.fake.busy=True
        with self.assertRaises(app.HTTPException): self.c.check_and_release()
        self.fake.busy=False; self.fake.check_failure=True
        with self.assertRaises(app.HTTPException): self.c.check_and_release()
        self.assertNotIn('release',self.fake.calls); self.assertIsNotNone(self.c.session)
    def test_unsaved_and_corrupt_outputs_block_release_before_cloud_check(self):
        job=self.finish('manual'); path=self.path/job['file']; original=path.read_bytes()
        path.write_bytes(b'corrupted')
        with self.assertRaises(app.HTTPException): self.c.check_and_release()
        self.assertNotIn('check',self.fake.calls)
        path.write_bytes(original); self.c.jobs['pending']={'status':'interrupted'}
        with self.assertRaises(app.HTTPException): self.c.check_and_release()
        self.assertNotIn('check',self.fake.calls); self.assertNotIn('release',self.fake.calls)
    def test_failed_download_never_times_out_unsaved_result(self):
        self.fake.bad_hash=True; self.finish(); self.clock=1300; self.c.idle_tick()
        self.assertNotIn('release',self.fake.calls); self.assertIsNotNone(self.c.session)
    def test_policy_changes_apply_to_existing_idle_gpu_and_persist(self):
        self.finish(); self.clock=1120
        self.c.configure_release(app.ReleasePolicy(mode='idle',idle_minutes=1))
        self.assertEqual(self.c.release_status()['deadline'],1060)
        # Save a DPAPI session exactly as the real controller does.
        self.c.save_session()
        restored=app.Controller(self.path,self.path,now=lambda:self.clock)
        self.assertEqual(restored.release_policy.mode,'idle'); self.assertEqual(restored.release_status()['deadline'],1060)
        self.c.idle_tick(); self.assertIsNone(self.c.session)
    def test_running_local_task_cannot_be_manually_released(self):
        self.c.active='running'; self.c.session={'endpoint':'owned-test'}
        with self.assertRaises(app.HTTPException): self.c.check_and_release()
        self.assertEqual(self.fake.calls,[])
    def test_no_managed_gpu_check_is_read_only(self):
        result=self.c.check_and_release()
        self.assertTrue(result['ok']); self.assertIn('无需释放',result['message']); self.assertEqual(self.fake.calls,[])
    def test_explicit_legacy_discard_clears_pending_after_runtime_is_gone(self):
        self.c.jobs['pending']={'status':'interrupted'}
        self.c.check_and_release(discard_pending=True)
        self.assertEqual(self.c.jobs['pending']['status'],'discarded')
    def test_legacy_flag_and_validation(self):
        self.assertEqual(app.ReleasePolicy().mode, 'idle')
        self.assertEqual(app.ReleasePolicy().idle_minutes, 10)
        self.assertEqual(self.c.release_policy.mode, 'idle')
        self.assertEqual(self.c.release_policy.idle_minutes, 10)
        default_job = app.JobConfig(prompt='test')
        self.assertEqual(default_job.release_mode, 'idle')
        self.assertEqual(default_job.idle_minutes, 10)
        self.assertFalse(default_job.auto_release)
        self.assertTrue(app.JobConfig(prompt='test',release_mode='always',auto_release=False).auto_release)
        self.assertFalse(app.JobConfig(prompt='test',release_mode='idle').auto_release)
        for minutes in (0,121):
            with self.assertRaises(ValidationError): app.ReleasePolicy(mode='idle',idle_minutes=minutes)


class RemoteCheckTests(unittest.TestCase):
    def transport(self):
        control=MagicMock(session={'endpoint':'owned','url':'https://example.invalid','token':'test-only','name':'test','variant':'GPU','accelerator':'G4','machine_shape':'HIGH_RAM','kernel_id':'k','session_id':None})
        transport=app.GoogleTransport(control); client=MagicMock()
        assignment=MagicMock(endpoint='owned'); assignment.runtime_proxy_info.url='https://example.invalid'; assignment.runtime_proxy_info.token='test-only'
        client.list_assignments.return_value=[assignment]; transport.client=lambda:client
        return control,transport,client
    def test_busy_kernel_does_not_execute_probe_or_release(self):
        control,t,client=self.transport()
        with patch('requests.get') as request, patch.object(app,'ColabRuntime') as runtime:
            request.return_value.json.return_value=[{'id':'k','execution_state':'busy'}]
            self.assertFalse(t.check_idle()); runtime.assert_not_called(); client.unassign.assert_not_called()
    def test_queue_result_is_required_and_connection_is_closed(self):
        control,t,client=self.transport()
        with patch('requests.get') as request, patch.object(app,'ColabRuntime') as runtime:
            request.return_value.json.return_value=[{'id':'k','execution_state':'idle'}]
            runtime.return_value.execute_code.return_value=[{'text':'H3_IDLE_CHECK '+json.dumps({'idle':True})}]
            self.assertTrue(t.check_idle()); client.assign.assert_not_called(); client.unassign.assert_not_called()
            compile(runtime.return_value.execute_code.call_args.args[0],'idle-probe','exec')
            runtime.return_value.stop.assert_called_once()
            runtime.return_value.execute_code.return_value=[{'output_type':'error','evalue':'unknown'}]
            with self.assertRaises(RuntimeError): t.check_idle()
    def test_gone_runtime_clears_owned_session_without_allocating(self):
        control,t,client=self.transport(); client.list_assignments.return_value=[]
        self.assertTrue(t.check_idle()); self.assertIsNone(control.session); client.assign.assert_not_called()


if __name__ == '__main__': unittest.main()

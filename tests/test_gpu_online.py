import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import app


def assignment(endpoint='gpu-test', gpu=app.Accelerator.G4):
    return SimpleNamespace(endpoint=endpoint,accelerator=gpu,
        runtime_proxy_info=SimpleNamespace(url='https://example.invalid',token='test-only'))


class TransportGpuTests(unittest.TestCase):
    def setUp(self):
        self.control=MagicMock(session={'endpoint':'owned','url':'https://example.invalid','token':'test-only',
            'name':'test','variant':'GPU','accelerator':'G4','machine_shape':'HIGH_RAM','kernel_id':'k','session_id':None})
        self.transport=app.GoogleTransport(self.control)
        self.client=MagicMock(); self.transport.client=lambda:self.client

    def test_detection_is_fresh_and_filters_cpu_tpu(self):
        self.client.list_assignments.return_value=[assignment('owned'),assignment('other',app.Accelerator.A100),
            assignment('cpu',app.Accelerator.NONE),assignment('tpu',app.Accelerator.V5E1)]
        for _ in range(2):
            result=self.transport.detect_online_gpus()
            self.assertEqual(result['runtimes'],[dict(endpoint='owned',gpu='G4',managed=True),dict(endpoint='other',gpu='A100',managed=False)])
        self.assertEqual(self.client.list_assignments.call_count,2)
        self.client.assign.assert_not_called();self.client.unassign.assert_not_called()
        self.control.save_session.assert_not_called()

    def test_release_only_selected_external_gpu_and_keeps_owned_session(self):
        original=copy.deepcopy(self.control.session)
        other=assignment('other')
        self.client.list_assignments.side_effect=[[other],[other],[]]
        with patch.object(self.transport,'check_idle',return_value=True) as idle:
            self.assertTrue(self.transport.release_detected_gpu('other'))
        idle.assert_called_once_with('other');self.client.unassign.assert_called_once_with('other')
        self.assertEqual(self.control.session,original)
        self.control.save_session.assert_not_called();self.client.assign.assert_not_called()

    def test_cpu_busy_and_unknown_status_are_never_unassigned(self):
        for value,idle_result,error in [(assignment('other',app.Accelerator.NONE),True,None),
                                        (assignment('other'),False,None),(assignment('other'),True,RuntimeError('network unknown'))]:
            self.client.list_assignments.side_effect=None;self.client.list_assignments.return_value=[value]
            with patch.object(self.transport,'check_idle',return_value=idle_result,side_effect=error):
                with self.assertRaises((app.HTTPException,RuntimeError)):self.transport.release_detected_gpu('other')
            self.client.unassign.assert_not_called();self.client.assign.assert_not_called()

    def test_already_offline_does_not_allocate_or_release(self):
        self.client.list_assignments.return_value=[]
        with patch.object(self.transport,'check_idle') as idle:
            self.assertFalse(self.transport.release_detected_gpu('missing'));idle.assert_not_called()
        self.client.assign.assert_not_called();self.client.unassign.assert_not_called()

    def test_external_idle_probe_keeps_existing_session(self):
        self.client.list_assignments.return_value=[assignment('other')]
        original=copy.deepcopy(self.control.session)
        with patch('requests.get') as get,patch.object(app,'ColabRuntime') as runtime:
            get.return_value.json.return_value=[dict(id='external-kernel',execution_state='idle')]
            runtime.return_value.execute_code.return_value=[dict(text='H3_IDLE_CHECK {"idle": true}')]
            self.assertTrue(self.transport.check_idle('other'))
            self.assertEqual(runtime.call_args.kwargs['kernel_id'],None)
            runtime.return_value.stop.assert_called_once()
        self.assertEqual(self.control.session,original);self.control.save_session.assert_not_called()

    def test_release_requires_confirmed_disappearance(self):
        self.client.list_assignments.return_value=[assignment('other')]
        with patch.object(self.transport,'check_idle',return_value=True),patch.object(app.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'尚未确认'):self.transport.release_detected_gpu('other')
        self.client.unassign.assert_called_once_with('other')


class ControllerGpuTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.root=Path(self.temp.name)
        assert self.root.resolve().is_relative_to(app.PRIVATE.resolve())
        self.control=app.Controller(self.root,self.root)
        self.transport=MagicMock();self.control.transport=self.transport

    def tearDown(self):self.temp.cleanup()

    def test_active_or_pending_result_blocks_release(self):
        self.control.active='active'
        with self.assertRaises(app.HTTPException):self.control.release_online_gpu('other')
        self.control.active=None
        self.control.jobs['pending']=dict(endpoint='other',status='interrupted')
        with self.assertRaises(app.HTTPException):self.control.release_online_gpu('other')
        self.transport.release_detected_gpu.assert_not_called()

    def test_missing_saved_file_blocks_external_release(self):
        self.control.jobs['saved']=dict(endpoint='other',status='done',file='missing.mp4')
        with self.assertRaises(app.HTTPException):self.control.release_online_gpu('other')
        self.transport.release_detected_gpu.assert_not_called()

    def test_owned_runtime_uses_existing_release_checks(self):
        self.control.session={'endpoint':'owned'}
        with patch.object(self.control,'check_and_release',return_value={'ok':True}) as checked:
            self.assertTrue(self.control.release_online_gpu('owned')['ok']);checked.assert_called_once_with()
        self.transport.release_detected_gpu.assert_not_called()

    def test_external_release_and_route_detection_failure(self):
        self.transport.release_detected_gpu.return_value=True
        self.assertTrue(self.control.release_online_gpu('other')['ok'])
        self.assertFalse(self.control.release_checking)
        with patch.object(app,'controller',self.control):
            self.transport.detect_online_gpus.side_effect=RuntimeError('connection failed')
            with self.assertRaises(app.HTTPException) as error:app.detect_online_gpus()
            self.assertEqual(error.exception.status_code,400)
            self.assertIn('connection failed',error.exception.detail)


if __name__=='__main__':unittest.main()

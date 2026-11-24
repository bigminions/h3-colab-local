"""Remote HTTP observation and cancellation are independent of busy execution."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from requests import ReadTimeout

import app
import remote_consumer


class ConsumerProviderTests(unittest.TestCase):
    def setUp(self):
        self.control = MagicMock(session={'endpoint': 'owned', 'url': 'https://example.invalid', 'token': 'test'})
        self.provider = app.ColabProvider(self.control, app.runtime_services)

    def response(self, data):
        response = MagicMock(status_code=200)
        response.json.return_value = data
        return response

    def test_busy_kernel_reads_status_without_executing_probe(self):
        record = dict(job_id='job', run_id='run', stage='generate', prompt_id='prompt', heartbeat=time.time())
        self.provider._proxy_request = MagicMock(side_effect=[
            self.response([{'execution_state': 'busy'}]), self.response({'content': json.dumps(record)})])
        with patch.object(self.provider, 'check_idle') as idle:
            result = self.provider.consumer_status()
        self.assertEqual(result['state'], 'busy')
        self.assertEqual(result['prompt_id'], 'prompt')
        idle.assert_not_called()

    def test_stale_record_is_unknown_while_kernel_busy(self):
        self.provider._proxy_request = MagicMock(side_effect=[
            self.response([{'execution_state': 'busy'}]),
            self.response({'content': json.dumps({'run_id': 'old', 'heartbeat': 0})})])
        self.assertEqual(self.provider.consumer_status()['state'], 'unknown')

    def test_idle_kernel_still_checks_generation_subprocess(self):
        self.provider._proxy_request = MagicMock(side_effect=[
            self.response([{'execution_state': 'idle'}]),
            self.response({'content': json.dumps({'run_id': 'run', 'heartbeat': time.time()})})])
        with patch.object(self.provider, 'check_idle', return_value=False) as idle:
            self.assertEqual(self.provider.consumer_status()['state'], 'busy')
        idle.assert_called_once()

    def test_confirmed_idle_does_not_read_previous_task_record(self):
        self.provider._proxy_request = MagicMock(return_value=self.response([{'execution_state': 'idle'}]))
        with patch.object(self.provider, 'check_idle', return_value=True) as idle, \
                patch.object(self.provider, '_consumer_record') as record:
            self.assertEqual(self.provider.consumer_status(), {'state': 'idle'})
        idle.assert_called_once()
        record.assert_not_called()

    def test_stalled_extra_kernels_do_not_block_reuse_of_idle_managed_kernel(self):
        session = self.control.session
        session.update(name='test', variant='GPU', accelerator='L4', machine_shape='STANDARD',
                       kernel_id='managed', session_id='session', app_id='qwen_image_2_1')
        last_activity = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')
        kernels = [dict(id='stalled', execution_state='starting', connections=0, last_activity=last_activity),
                   dict(id='managed', execution_state='idle', connections=1)]
        assignment = SimpleNamespace(endpoint=session['endpoint'], runtime_proxy_info=SimpleNamespace(
            url=session['url'], token=session['token']))
        self.provider._proxy_request = MagicMock(return_value=self.response(kernels))
        with patch.object(self.provider, 'client') as client, patch('requests.get') as get, \
                patch.object(app, 'ColabRuntime') as runtime, patch.object(self.provider, '_consumer_record') as record:
            client.return_value.list_assignments.return_value = [assignment]
            get.return_value = self.response(kernels)
            runtime.return_value.execute_code.return_value = [dict(text='QWEN_IMAGE_IDLE_CHECK {"idle": true}')]
            self.assertEqual(self.provider.consumer_status(), {'state': 'idle'})
            self.assertFalse(self.provider.check_idle())
        self.assertEqual(runtime.call_args.kwargs['kernel_id'], 'managed')
        runtime.return_value.execute_code.assert_called_once()
        record.assert_not_called()

    def test_unconfirmed_or_active_kernels_still_block_reuse(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        current = datetime.now(timezone.utc).isoformat()
        managed = dict(id='managed', execution_state='idle')
        other = dict(id='other', execution_state='starting', connections=0, last_activity=old)
        self.control.session['kernel_id'] = 'managed'
        for kernels in ([dict(other, execution_state='busy'), managed],
                        [dict(other, connections=1), managed],
                        [dict(other, last_activity=current), managed],
                        [dict(other, last_activity=None), managed],
                        [other, dict(managed, execution_state='starting', connections=0, last_activity=old)],
                        [other]):
            with self.subTest(kernels=kernels), patch.object(self.provider, 'check_idle') as idle, \
                    patch.object(self.provider, '_proxy_request', return_value=self.response(kernels)), \
                    patch.object(self.provider, '_consumer_record', return_value={'heartbeat': 0}):
                self.assertEqual(self.provider.consumer_status()['state'], 'unknown')
                idle.assert_not_called()

    def test_background_probe_leaves_initial_kernel_creation_to_active_task(self):
        self.control.active = 'starting-image-job'
        self.control.session['app_id'] = 'qwen_image_2_1'
        self.provider._proxy_request = MagicMock(return_value=self.response([]))
        with patch.object(self.provider, 'check_idle') as idle:
            self.assertEqual(self.provider.observe_consumer(), {'state': 'connecting'})
        idle.assert_not_called()

    def test_proxy_failure_reconciles_a_released_assignment(self):
        self.provider._proxy_request = MagicMock(side_effect=ReadTimeout('offline'))
        with patch.object(self.provider, 'client') as client:
            client.return_value.list_assignments.return_value = []
            self.assertEqual(self.provider.consumer_status(), {'state': 'disconnected'})
        self.assertIsNone(self.control.session)
        self.control.on_runtime_recycled.assert_called_once_with('owned')

    def test_proxy_failure_with_unconfirmed_assignment_keeps_session(self):
        self.provider._proxy_request = MagicMock(side_effect=ReadTimeout('proxy offline'))
        with patch.object(self.provider, 'client') as client:
            client.return_value.list_assignments.side_effect = ReadTimeout('Colab unavailable')
            with self.assertRaisesRegex(ReadTimeout, 'proxy offline'):
                self.provider.consumer_status()
        self.assertEqual(self.control.session['endpoint'], 'owned')
        self.control.on_runtime_recycled.assert_not_called()

    def test_cancel_rejects_a_different_remote_execution(self):
        self.provider._consumer_record = lambda: {'job_id': 'other', 'run_id': 'other'}
        with patch.object(self.provider, '_proxy_request') as request:
            with self.assertRaises(RuntimeError):
                self.provider.request_cancel({'id': 'mine', 'consumer_run_id': 'mine', 'execution_started': True})
        request.assert_not_called()

    def test_cancel_writes_scoped_request_and_waits_for_remote_idle(self):
        self.provider._consumer_record = lambda: {'job_id': 'mine', 'run_id': 'run'}
        self.provider._proxy_request = MagicMock(return_value=self.response({}))
        self.provider.consumer_status = MagicMock(side_effect=[{'state': 'cancelling'}, {'state': 'idle'}])
        with patch('compute_colab.time.sleep'):
            self.provider.cancel_job({'id': 'mine', 'consumer_run_id': 'run'})
        call = self.provider._proxy_request.call_args
        self.assertEqual(call.args[0], 'PUT')
        self.assertEqual(json.loads(call.kwargs['json']['content']), {'job_id': 'mine', 'run_id': 'run'})
        self.assertEqual(self.provider.consumer_status.call_count, 2)


class RemoteConsumerTests(unittest.TestCase):
    def test_cancel_targets_owned_prompt_and_keeps_generation_service(self):
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as root:
            interrupted = threading.Event()
            running = ['owned-prompt']
            posts = []
            def get(*args, **kwargs):
                return self.response({'queue_running': [[0, key] for key in running], 'queue_pending': []})
            def post(url, **kwargs):
                posts.append((url, kwargs['json']))
                running.clear()
                return self.response({})
            def operation(config):
                remote_consumer.event('generate', 'started', prompt_id='owned-prompt')
                Path(root, 'lstudio-cancel.json').write_text(json.dumps({'job_id': 'job', 'run_id': 'run'}))
                self.assertTrue(interrupted.wait(3))
                raise KeyboardInterrupt()
            config = {'job_id': 'job', 'app_id': 'h3', '_consumer_run_id': 'run'}
            with patch.object(remote_consumer, 'event', MagicMock(), create=True), \
                    patch('requests.get', side_effect=get), patch('requests.post', side_effect=post):
                with self.assertRaises(KeyboardInterrupt):
                    remote_consumer.consumer_run(operation, config, root, .01, interrupted.set)
            record = json.loads(Path(root, 'lstudio-consumer.json').read_text())
            self.assertEqual(record['state'], 'cancelled')
            self.assertEqual(posts, [('http://127.0.0.1:8188/interrupt', {'prompt_id': 'owned-prompt'})])

    def test_old_cancel_request_does_not_cancel_new_execution(self):
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as root:
            Path(root, 'lstudio-cancel.json').write_text(json.dumps({'job_id': 'job', 'run_id': 'old'}))
            interrupt = MagicMock()
            def operation(config):
                time.sleep(.03)
            with patch.object(remote_consumer, 'event', MagicMock(), create=True), \
                    patch('requests.get', return_value=self.response({'queue_running': [], 'queue_pending': []})):
                remote_consumer.consumer_run(operation, {'job_id': 'job', '_consumer_run_id': 'new'}, root, .01, interrupt)
            interrupt.assert_not_called()
            self.assertEqual(json.loads(Path(root, 'lstudio-consumer.json').read_text())['state'], 'done')

    def test_remote_deadline_interrupts_without_a_local_cancel_request(self):
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as root:
            interrupted = threading.Event()

            def operation(config):
                self.assertTrue(interrupted.wait(3))
                raise KeyboardInterrupt()

            with patch.object(remote_consumer, 'event', MagicMock(), create=True), \
                    patch('requests.get', return_value=self.response({'queue_running': [], 'queue_pending': []})):
                with self.assertRaises(KeyboardInterrupt):
                    remote_consumer.consumer_run(
                        operation, {'job_id': 'job', '_consumer_run_id': 'run'},
                        root, .01, interrupted.set, deadline=0,
                    )
            self.assertFalse(Path(root, 'lstudio-cancel.json').exists())
            self.assertEqual(json.loads(Path(root, 'lstudio-consumer.json').read_text())['state'], 'cancelled')

    @staticmethod
    def response(data):
        result = MagicMock()
        result.json.return_value = data
        return result

"""Remote HTTP observation and cancellation are independent of busy execution."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

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

    @staticmethod
    def response(data):
        result = MagicMock()
        result.json.return_value = data
        return result

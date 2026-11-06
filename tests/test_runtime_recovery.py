"""Passive recovery tests; no network, GPU allocation or kernel execution."""
import json
import queue
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import app
from runtime_recovery import observe_execution, enable_cancellation, TaskCancelled, CancellableWait, cancellable_receive
from jupyter_kernel_client.wsclient import KernelWebSocketClient
from tests.test_app import FakeTransport


class RecoveryTests(unittest.TestCase):
    def test_real_client_quiet_wait_cancels_and_keeps_environment_without_another_message(self):
        # Actual dependency loop, queues and events; only the network send is mocked.
        with tempfile.TemporaryDirectory() as temp:
            control = app.Controller(Path(temp), Path(temp))
            job = dict(id='quiet', config=app.H3JobConfig(prompt='test').model_dump(),
                       status='running', events=[], created=0)
            control.jobs[job['id']] = job
            control.active = job['id']
            transport = FakeTransport(control)
            client = KernelWebSocketClient('wss://example.invalid/api/kernels/test/channels')
            client.kernel_socket = MagicMock()
            client.connection_ready.set()
            client.execute = MagicMock(return_value='request')
            entered = threading.Event()
            wait = client._message_received.wait
            def waiting(timeout=None):
                entered.set()
                return wait(timeout)
            client._message_received.wait = waiting
            runtime = SimpleNamespace(kernel_client=SimpleNamespace(_manager=SimpleNamespace(client=client)))
            def execute(current):
                enable_cancellation(runtime, lambda: control.check_cancelled(current))
                client.execute_interactive('no network send', allow_stdin=False, timeout=4500)
            transport.execute = execute
            control.transport = transport
            thread = threading.Thread(target=control.run, args=(job,), daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                control.cancel(job['id'])
                thread.join(2)
                self.assertFalse(thread.is_alive(), 'Cancellation must not wait for another output or the 4500 s deadline')
                self.assertEqual(job['status'], 'cancelled')
                self.assertIsNone(control.active)
                self.assertIsNotNone(control.session)
                self.assertEqual(transport.calls, ['connect', 'cancel'])
                client.execute.assert_called_once()
            finally:
                job['cancel_requested'] = True
                client._message_received.set()
                thread.join(2)
                client.stop_channels()

    def test_real_client_shell_reply_and_recovery_waits_are_cancellable(self):
        for mode in ('reply', 'recovery'):
            client = KernelWebSocketClient('wss://example.invalid/api/kernels/test/channels')
            client.kernel_socket = MagicMock()
            client.connection_ready.set()
            cancelled, entered = threading.Event(), threading.Event()
            def check():
                if cancelled.is_set():
                    raise TaskCancelled('cancelled')
            channel = client.shell_channel if mode == 'reply' else client.iopub_channel
            receive = channel.get_msg
            def receiving(timeout=None):
                entered.set()
                return receive(timeout=timeout)
            channel.get_msg = receiving
            runtime = SimpleNamespace(kernel_client=SimpleNamespace(_manager=SimpleNamespace(client=client)))
            enable_cancellation(runtime, check)
            errors = []
            def run():
                try:
                    if mode == 'reply':
                        client._recv_reply('request', timeout=4500)
                    else:
                        observe_execution(runtime, 'owned', MagicMock())
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                cancelled.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], TaskCancelled)
            finally:
                cancelled.set()
                thread.join(2)
                client.stop_channels()

    def test_cancellable_waits_preserve_ready_empty_and_message_semantics(self):
        event = threading.Event()
        wrapped = CancellableWait(event, lambda: None)
        self.assertFalse(wrapped.wait(0))
        wrapped.set()
        self.assertTrue(wrapped.wait(0))
        wrapped.clear()
        self.assertFalse(wrapped.is_set())
        messages = queue.Queue()
        receive = cancellable_receive(messages.get, lambda: None)
        with self.assertRaises(queue.Empty):
            receive(timeout=0)
        messages.put({'content': 'unchanged'})
        self.assertEqual(receive(timeout=1), {'content': 'unchanged'})

    def test_pre_cancelled_job_does_not_initialize_a_runtime_client(self):
        runtime = MagicMock()
        def check():
            raise TaskCancelled('cancelled')
        with self.assertRaises(TaskCancelled):
            enable_cancellation(runtime, check)
        self.assertEqual(runtime.mock_calls, [])

    def test_only_original_session_streams_are_followed_without_execute(self):
        runtime = MagicMock()
        messages = [dict(msg_type='stream',parent_header={'session':'other'},content={'text':'foreign'}),
                    dict(msg_type='status',parent_header={'session':'other'},content={'execution_state':'idle'}),
                    dict(msg_type='stream',parent_header={'session':'owned'},content={'text':'progress'}),
                    dict(msg_type='status',parent_header={'session':'owned'},content={'execution_state':'idle'})]
        runtime.kernel_client._manager.client.get_iopub_msg.side_effect = messages
        hook = MagicMock()
        result = observe_execution(runtime,'owned',hook)
        self.assertEqual([o['text'] for o in result],['progress'])
        hook.assert_called_once()
        runtime.execute_code.assert_not_called()
        runtime.restart.assert_not_called()

    def test_remote_error_is_returned_and_disconnect_is_not_reexecuted(self):
        runtime = MagicMock()
        runtime.kernel_client._manager.client.get_iopub_msg.side_effect = [
            dict(msg_type='error',parent_header={'session':'owned'},content={'evalue':'failed'}),
            dict(msg_type='status',parent_header={'session':'owned'},content={'execution_state':'idle'})]
        self.assertEqual(observe_execution(runtime,'owned',MagicMock())[0]['evalue'],'failed')
        runtime.kernel_client._manager.client.get_iopub_msg.side_effect = RuntimeError('Connection was lost.')
        with self.assertRaisesRegex(RuntimeError,'Connection was lost'):
            observe_execution(runtime,'owned',MagicMock())
        runtime.execute_code.assert_not_called()

    def test_busy_original_job_result_does_not_submit_code_again(self):
        with tempfile.TemporaryDirectory() as temp:
            control = app.Controller(Path(temp),Path(temp))
            control.session = dict(endpoint='owned-test')
            transport = app.ColabProvider(control, app.runtime_services)
            transport.refresh = lambda:SimpleNamespace(url='https://example.invalid',token='test-only',
                                                       kernel_id='kernel',session_id='session')
            job = dict(id='test',config=app.H3JobConfig(prompt='test').model_dump(),events=[],status='running')
            control.jobs['test'] = job
            result = {'path':'/content/ComfyUI/output/H3/test.mp4','size':1}
            def observe(runtime, session, hook):
                hook({'text':'H3_EVENT '+json.dumps({'stage':'result','message':'done','result':result})})
                return []
            response = MagicMock();response.json.return_value=[{'id':'kernel','execution_state':'busy'}]
            with patch('requests.get',return_value=response),patch('app.ColabRuntime') as runtime,patch('app.observe_execution',side_effect=observe):
                transport.execute(job)
                runtime.return_value.execute_code.assert_not_called()
                runtime.return_value.stop.assert_called_once()
            self.assertEqual(job['remote_result'],result)
            self.assertTrue(any(e['stage']=='recover' for e in job['events']))

    def test_resume_label_does_not_claim_a_video_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            control = app.Controller(Path(temp),Path(temp))
            control.session={'endpoint':'owned-test'}
            job=dict(id='test',endpoint='owned-test',config={},status='error',events=[])
            self.assertTrue(control.public_job(job)['can_retry'])
            self.assertEqual(control.public_job(job)['recovery_kind'],'resume')
            job['remote_result']={'path':'test.mp4'}
            self.assertEqual(control.public_job(job)['recovery_kind'],'retrieve')


if __name__ == '__main__':
    unittest.main()

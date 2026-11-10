"""Connection diagnostics and allocation waits; no network or GPU requests."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests
from google.oauth2.credentials import Credentials
from colab_cli.client import ColabRequestError

import app
from runtime_recovery import connection_failure
from tests.test_app import FakeTransport


class ConnectionDiagnosticsTests(unittest.TestCase):
    def test_allocation_has_longer_response_wait_without_changing_other_requests(self):
        session = app.TimedSession(Credentials(token='test-only'))
        with patch.object(app.AuthorizedSession, 'request', return_value=object()) as request:
            for method, path, expected in [('POST', '/tun/m/assign', (15, 180)),
                                           ('GET', '/tun/m/assign', (15, 60)),
                                           ('GET', '/tun/m/assignments', (15, 60))]:
                session.request(method, 'https://example.invalid' + path)
                self.assertEqual(request.call_args.kwargs['timeout'], expected)
            session.request('POST', 'https://example.invalid/tun/m/assign', timeout=(3, 9))
            self.assertEqual(request.call_args.kwargs['timeout'], (3, 9))

    def test_timeout_diagnostics_include_operation_but_no_authenticated_url(self):
        session = app.TimedSession(Credentials(token='test-only'))
        secret_url = 'https://example.invalid/tun/m/assign?token=must-never-appear'
        error = requests.ReadTimeout('request failed: ' + secret_url)
        with patch.object(app.AuthorizedSession, 'request', side_effect=error):
            with self.assertRaises(requests.ReadTimeout):
                session.request('POST', secret_url)
        detail = app.connection_diagnostic(error, '连接 G4')
        self.assertIn('POST 分配显卡', detail['phase'])
        self.assertIn('ReadTimeout', detail['reason'])
        self.assertIn('seconds', detail)
        self.assertNotIn('must-never-appear', json.dumps(detail))
        self.assertNotIn('example.invalid', json.dumps(detail))

    def test_sdk_service_failure_retries_but_auth_failure_does_not(self):
        for code, expected in [(503, True), (429, True), (401, False), (400, False)]:
            response = requests.Response(); response.status_code = code
            error = ColabRequestError('error', request=None, response=response)
            self.assertEqual(connection_failure(error), expected)
            self.assertIn('HTTP ' + str(code), app.connection_diagnostic(error, '查询运行时')['reason'])
        resp_503 = requests.Response(); resp_503.status_code = 503
        err_503 = ColabRequestError('error', request=None, response=resp_503)
        self.assertIn('建议切换显卡', app.connection_diagnostic(err_503, '申请并启动 A100 显卡')['reason'])

    def test_socket_permission_error_is_identifiable(self):
        error = requests.ConnectionError('[WinError 10013] socket access forbidden')
        self.assertIn('本机网络访问被拒绝', app.connection_diagnostic(error, '查询运行时')['reason'])

    def test_allocation_failure_is_persisted_and_survives_auto_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = app.Controller(Path(tmp), Path(tmp))
            control.cancel_wakeup.wait = MagicMock()
            job = dict(id='d'*32, created=0, status='running', events=[],
                       config=app.H3JobConfig(prompt='test', gpu='G4').model_dump())
            control.jobs[job['id']] = job; control.active = job['id']
            transport = app.ColabProvider(control, app.runtime_services)
            client = MagicMock(); client.list_assignments.return_value = []
            client.assign.side_effect = requests.ReadTimeout('token=must-never-appear')
            transport.client = lambda: client
            transport.cancel_pending_assignment = MagicMock()
            control.transport = transport
            control.run(job)
            self.assertEqual(job['status'], 'cancelled')
            self.assertEqual(client.assign.call_count, 4)
            transport.cancel_pending_assignment.assert_called_once_with(job)
            self.assertEqual(len(job['connection_errors']), 4)
            self.assertTrue(all('申请并启动 G4' in row['phase'] for row in job['connection_errors']))
            messages = '\n'.join(e['message'] for e in job['events'])
            self.assertIn('ReadTimeout', messages)
            self.assertNotIn('进度连接中断', messages)
            self.assertNotIn('must-never-appear', messages)
            self.assertIn('ReadTimeout', job['message'])
            restarted = app.Controller(Path(tmp), Path(tmp))
            self.assertEqual(restarted.jobs[job['id']]['connection_errors'], job['connection_errors'])
            self.assertIn('申请并启动 G4', restarted.jobs[job['id']]['auto_cancel_reason'])

    def test_transfer_failure_keeps_its_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = app.Controller(Path(tmp), Path(tmp))
            control.cancel_wakeup.wait = MagicMock()
            job = dict(id='e'*32, created=0, status='running', events=[],
                       config=app.H3JobConfig(prompt='test').model_dump())
            control.jobs[job['id']] = job; control.active = job['id']
            fake = FakeTransport(control)
            fake.download = MagicMock(side_effect=requests.ConnectionError('offline'))
            control.transport = fake
            control.run(job)
            self.assertEqual(job['connection_errors'][-1]['phase'], '下载视频到本机')
            self.assertIn('下载视频到本机', job['message'])

    def test_long_connection_step_reports_waiting_and_stops_reporting_after_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = app.Controller(Path(tmp), Path(tmp))
            job = dict(id='f'*32, created=0, status='running', events=[], config={})
            control.jobs[job['id']] = job; control.active = job['id']
            progress_seen = threading.Event()
            original = control.event
            def event(job, stage, message):
                original(job, stage, message)
                if '已等待' in message:
                    progress_seen.set()
            control.event = event
            with patch.object(app, 'CONNECT_PROGRESS_INTERVAL', .01):
                result = app.ColabProvider(control, app.runtime_services).connection_step('申请并启动 G4 显卡',
                                                                      lambda: progress_seen.wait(2))
            self.assertTrue(result)
            self.assertIn('完成', job['events'][-1]['message'])
            count = len(job['events'])
            threading.Event().wait(.04)
            self.assertEqual(len(job['events']), count)


if __name__ == '__main__':
    unittest.main()

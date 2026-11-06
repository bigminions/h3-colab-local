"""Runtime ownership, application affinity and independent GPU release."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

import app
from tests.test_app import FakeTransport


class QueueRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.control = app.Controller(self.path, self.path)
        self.fake = FakeTransport(self.control)
        self.control.transport = self.fake

    def tearDown(self):
        self.temp.cleanup()

    def test_occupying_app_info_empty(self):
        app_id, app_name = self.control.occupying_app_info(app.applications)
        self.assertIsNone(app_id)
        self.assertIsNone(app_name)

    def test_occupying_app_info_from_session(self):
        self.control.session = {'endpoint': 'test-endpoint', 'accelerator': 'G4', 'app_id': 'h3'}
        app_id, app_name = self.control.occupying_app_info(app.applications)
        self.assertEqual(app_id, 'h3')
        self.assertEqual(app_name, 'H3 视频')

        self.control.session['app_id'] = 'qwen_image_2_1'
        app_id, app_name = self.control.occupying_app_info(app.applications)
        self.assertEqual(app_id, 'qwen_image_2_1')
        self.assertEqual(app_name, 'Qwen-Image-2.1')

    def test_occupying_app_info_deduces_from_recent_job_when_session_app_id_missing(self):
        self.control.session = {'endpoint': 'ep-l4-test', 'accelerator': 'L4'}
        job_id = 'qwen-recent-job'
        self.control.jobs[job_id] = {
            'id': job_id,
            'app_id': 'qwen_image_2_1',
            'status': 'done',
            'endpoint': 'ep-l4-test',
            'created': 1000
        }
        app_id, app_name = self.control.occupying_app_info(app.applications)
        self.assertEqual(app_id, 'qwen_image_2_1')
        self.assertEqual(app_name, 'Qwen-Image-2.1')
        self.assertEqual(self.control.session.get('app_id'), 'qwen_image_2_1')

    def test_occupying_app_info_from_active_job(self):
        self.control.session = {'endpoint': 'test-endpoint', 'accelerator': 'G4', 'app_id': 'h3'}
        job_id = 'job-active-123'
        self.control.jobs[job_id] = {
            'id': job_id,
            'status': 'running',
            'config': {'app_id': 'qwen_image_2_1', 'gpu': 'T4'}
        }
        self.control.active = job_id
        app_id, app_name = self.control.occupying_app_info(app.applications)
        self.assertEqual(app_id, 'qwen_image_2_1')
        self.assertEqual(app_name, 'Qwen-Image-2.1')

    def test_check_and_release_force_false_blocks_when_active(self):
        self.control.session = {'endpoint': 'test-endpoint', 'accelerator': 'G4', 'app_id': 'h3'}
        self.control.active = 'running-job'
        with self.assertRaises(app.HTTPException) as ctx:
            self.control.check_and_release(force=False)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('任务执行中', ctx.exception.detail)

    def test_force_release_cannot_terminate_an_active_task(self):
        self.control.session = {'endpoint': 'test-endpoint', 'accelerator': 'G4', 'app_id': 'h3'}
        active_id = 'job-active-1'
        queued_id = 'job-queued-2'
        self.control.jobs[active_id] = {
            'id': active_id,
            'status': 'running',
            'events': [],
            'config': {'app_id': 'h3', 'gpu': 'G4'}
        }
        self.control.jobs[queued_id] = {
            'id': queued_id,
            'status': 'queued',
            'events': [],
            'config': {'app_id': 'h3', 'gpu': 'G4'}
        }
        self.control.active = active_id

        with self.assertRaises(app.HTTPException) as raised:
            self.control.check_and_release(force=True)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.control.active, active_id)
        self.assertEqual(self.control.jobs[queued_id]['status'], 'queued')
        self.assertNotIn('release', self.fake.calls)

    def test_run_queue_application_affinity(self):
        # Current session is on qwen_image_2_1
        self.control.session = {'endpoint': 'ep-l4', 'accelerator': 'L4', 'app_id': 'qwen_image_2_1'}

        # Earlier job is h3 (created=100), later job is qwen (created=200)
        job_h3 = {'id': 'h3-job', 'app_id': 'h3', 'status': 'queued', 'created': 100,
                  'config': {'app_id': 'h3', 'gpu': 'G4'}, 'events': []}
        job_qwen = {'id': 'qwen-job', 'app_id': 'qwen_image_2_1', 'status': 'queued', 'created': 200,
                    'config': {'app_id': 'qwen_image_2_1', 'gpu': 'L4'}, 'events': []}
        self.control.jobs['h3-job'] = job_h3
        self.control.jobs['qwen-job'] = job_qwen

        executed = []
        def mock_run(job):
            executed.append(job['id'])
            job['status'] = 'done'
        self.control.run = mock_run

        self.control._run_queue()
        # Qwen should execute FIRST because session is currently on Qwen, then H3 executes
        self.assertEqual(executed, ['qwen-job', 'h3-job'])
        self.assertIn('release', self.fake.calls)

    def test_compute_colab_switching_app_error(self):
        provider = app.ColabProvider(self.control, app.runtime_services)
        self.control.session = {
            'endpoint': 'ep-123',
            'accelerator': 'A100',
            'app_id': 'h3'
        }
        provider.client = MagicMock()
        config_different_app = MagicMock(endpoint=None, gpu='A100', app_id='qwen_image_2_1')
        with self.assertRaises(RuntimeError) as ctx:
            provider.connect(config_different_app)
        self.assertIn('请先释放 H3 视频 的显卡运行时', str(ctx.exception))
        self.assertIn('Qwen-Image-2.1', str(ctx.exception))

    def test_compute_colab_switching_gpu_model_error(self):
        provider = app.ColabProvider(self.control, app.runtime_services)
        self.control.session = {
            'endpoint': 'ep-123',
            'accelerator': 'G4',
            'app_id': 'h3'
        }
        provider.client = MagicMock()
        config_different_gpu = MagicMock(endpoint=None, gpu='A100', app_id='h3')
        with self.assertRaises(RuntimeError) as ctx:
            provider.connect(config_different_gpu)
        self.assertIn('请先释放当前运行时，再切换显卡型号', str(ctx.exception))

    def test_studio_html_modal_elements_present(self):
        studio_html = Path("studio.html").read_text(encoding="utf-8")
        self.assertIn('id="queuePlacementModal"', studio_html)
        self.assertIn('class="modal"', studio_html)
        self.assertIn('id="queuePlacementTitle"', studio_html)
        self.assertIn('id="queuePlacementSummary"', studio_html)
        self.assertIn('id="queuePlacementHelp"', studio_html)
        self.assertIn('id="queuePlacementHelpText"', studio_html)
        self.assertIn('id="queuePlacementBack"', studio_html)
        self.assertIn('id="queuePlacementFront"', studio_html)
        self.assertIn('id="cancelQueuePlacement"', studio_html)

    def test_cross_app_queue_drain_and_switch(self):
        # Current session is on h3
        self.control.session = {'endpoint': 'ep-h3', 'accelerator': 'G4', 'app_id': 'h3'}

        # Both applications wait in the unified queue; H3 already owns the environment.
        h3_job1 = {'id': 'h3-1', 'app_id': 'h3', 'status': 'queued', 'created': 10,
                   'config': {'app_id': 'h3', 'gpu': 'G4'}, 'events': []}
        # H3 job 2 (queued)
        h3_job2 = {'id': 'h3-2', 'app_id': 'h3', 'status': 'queued', 'created': 20,
                   'config': {'app_id': 'h3', 'gpu': 'G4'}, 'events': []}
        # Qwen job 1 (queued by user choosing "排队")
        qwen_job1 = {'id': 'qwen-1', 'app_id': 'qwen_image_2_1', 'status': 'queued', 'created': 30,
                     'config': {'app_id': 'qwen_image_2_1', 'gpu': 'L4'}, 'events': []}
        # Qwen job 2 (queued)
        qwen_job2 = {'id': 'qwen-2', 'app_id': 'qwen_image_2_1', 'status': 'queued', 'created': 40,
                     'config': {'app_id': 'qwen_image_2_1', 'gpu': 'L4'}, 'events': []}

        self.control.jobs = {
            'h3-1': h3_job1,
            'h3-2': h3_job2,
            'qwen-1': qwen_job1,
            'qwen-2': qwen_job2,
        }
        self.control.active = None

        executed = []
        def mock_run(job):
            executed.append(job['id'])
            job['status'] = 'done'
            # When qwen runs, simulate session updated to qwen
            if job['app_id'] == 'qwen_image_2_1':
                self.control.session = {'endpoint': 'ep-qwen', 'accelerator': 'L4', 'app_id': 'qwen_image_2_1'}
        self.control.run = mock_run

        self.control._run_queue()

        # h3-1 finishes, then h3-2 runs, draining all h3 tasks.
        # Then session switches to qwen: qwen-1 runs, then qwen-2 runs!
        self.assertEqual(executed, ['h3-1', 'h3-2', 'qwen-1', 'qwen-2'])

    def test_unretrieved_result_does_not_reject_new_queue_submissions(self):
        # Current session on h3 with an unretrieved error on endpoint
        self.control.session = {'endpoint': 'ep-error', 'accelerator': 'G4', 'app_id': 'h3'}
        self.control.jobs['h3-err'] = {
            'id': 'h3-err',
            'app_id': 'h3',
            'status': 'error',
            'endpoint': 'ep-error',
            'created': 10,
            'config': {'app_id': 'h3', 'gpu': 'G4'},
            'events': []
        }

        mock_worker = MagicMock()
        mock_worker.is_alive.return_value = True
        self.control.worker_thread = mock_worker
        h3_cfg = app.H3JobConfig(prompt='waiting', gpu='G4')
        h3_id = self.control.start(h3_cfg)
        self.assertEqual(self.control.jobs[h3_id]['status'], 'queued')

        # Submitting a Qwen job should NOT be blocked by H3's unretrieved error
        qwen_cfg = MagicMock(app_id='qwen_image_2_1', gpu='L4', provider_id='colab', release_mode='idle', idle_minutes=15, count=1, seed=1)
        qwen_cfg.model_dump.return_value = {'app_id': 'qwen_image_2_1', 'gpu': 'L4'}
        mock_worker = MagicMock()
        mock_worker.is_alive.return_value = True
        self.control.worker_thread = mock_worker
        job_id = self.control.start(qwen_cfg)
        self.assertIsNotNone(job_id)
        self.assertEqual(self.control.jobs[job_id]['status'], 'queued')

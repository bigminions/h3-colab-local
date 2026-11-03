import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
import app
from remote_h3 import build_graph, PDD, DIMENSIONS


class FakeTransport:
    def __init__(self, control, data=b'video', bad_hash=False, failure=False, release_failure=False):
        self.control = control
        self.data, self.bad_hash, self.failure, self.release_failure = data, bad_hash, failure, release_failure
        self.calls = []

    def connect(self, config):
        self.calls.append('connect')
        self.control.session = {'endpoint': 'owned-test'}

    def execute(self, job):
        self.calls.append('execute')
        if self.failure:
            job['safe_release'] = True
            raise RuntimeError('definite generation failure')
        job['remote_result'] = dict(path='/content/ComfyUI/output/test.mp4', size=len(self.data),
            sha256='wrong' if self.bad_hash else hashlib.sha256(self.data).hexdigest(), width=1344, height=768, duration=5.17, audio=1)

    def download(self, result, target):
        self.calls.append('download')
        target.write_bytes(self.data)

    def release(self):
        self.calls.append('release')
        if self.release_failure:
            raise RuntimeError('release unavailable')
        self.control.session = None


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.dir = Path(self.temp.name)
        self.c = app.Controller(self.dir, self.dir)
        self.c.configure_release(app.ReleasePolicy(mode='always'))

    def tearDown(self):
        self.temp.cleanup()

    def run_job(self, **fake_options):
        fake = FakeTransport(self.c, **fake_options)
        self.c.transport = fake
        job = dict(id='a'*32, config=app.H3JobConfig(prompt='test').model_dump(), events=[], status='running', created=0)
        self.c.jobs[job['id']] = job
        self.c.run(job)
        return fake, job

    def test_download_and_verify_before_release(self):
        fake, job = self.run_job()
        self.assertEqual(fake.calls, ['connect', 'execute', 'download', 'release'])
        self.assertEqual(job['status'], 'done')
        self.assertTrue((self.dir / job['file']).exists())
        self.assertTrue(all(isinstance(event['timestamp'], (int, float)) for event in job['events']))

    def test_hash_mismatch_preserves_runtime(self):
        fake, job = self.run_job(bad_hash=True)
        self.assertNotIn('release', fake.calls)
        self.assertEqual(job['status'], 'error')
        self.assertNotIn('file', job)
        self.assertIsNotNone(self.c.session)

    def test_definite_remote_failure_releases(self):
        fake, job = self.run_job(failure=True)
        self.assertIn('release', fake.calls)
        self.assertEqual(job['status'], 'error')

    def test_release_failure_does_not_claim_released(self):
        fake, job = self.run_job(release_failure=True)
        self.assertEqual(job['status'], 'warning')
        self.assertIsNotNone(self.c.session)
        self.assertIn('file', job)

    def test_retry_only_downloads_existing_result(self):
        fake, job = self.run_job(bad_hash=True)
        job['remote_result']['sha256'] = hashlib.sha256(fake.data).hexdigest()
        fake.calls.clear()
        self.c.run(job)
        self.assertEqual(fake.calls, ['connect', 'download', 'release'])
        self.assertEqual(job['status'], 'done')

    def test_retry_cannot_use_another_runtime(self):
        fake, job = self.run_job(bad_hash=True)
        self.c.session = {'endpoint': 'a-different-runtime'}
        with self.assertRaises(app.HTTPException) as raised:
            self.c.start(None, retry=job['id'])
        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(self.c.public_job(job)['can_retry'])

    def test_unretrieved_job_blocks_new_job(self):
        fake, job = self.run_job(bad_hash=True)
        with self.assertRaises(app.HTTPException) as raised:
            self.c.start(app.H3JobConfig(prompt='another job'))
        self.assertEqual(raised.exception.status_code, 409)

    def test_restart_marks_running_interrupted(self):
        fake, job = self.run_job()
        job.update(status='running')
        self.c.save_jobs()
        c2 = app.Controller(self.dir, self.dir)
        self.assertEqual(c2.jobs[job['id']]['status'], 'interrupted')


class ContractTests(unittest.TestCase):
    def test_reconnect_after_automatic_release(self):
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as temp:
            control = app.Controller(Path(temp), Path(temp))
            control.save_session = MagicMock()
            control.jobs['previous'] = {'endpoint': 'released-a100', 'status': 'done', 'config': {'release_mode': 'always'}}
            transport = app.ColabProvider(control, app.runtime_services)
            client = MagicMock()
            client.list_assignments.return_value = []
            client.assign.return_value.endpoint = 'new-a100'
            client.assign.return_value.runtime_proxy_info.url = 'https://example.invalid/'
            client.assign.return_value.runtime_proxy_info.token = 'test-only-token'
            transport.client = lambda: client
            result = transport.connect(app.H3JobConfig(prompt='test', endpoint='released-a100'))
            self.assertEqual(result.endpoint, 'new-a100')
            client.assign.assert_called_once()

    def test_missing_unmanaged_runtime_does_not_allocate(self):
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as temp:
            control = app.Controller(Path(temp), Path(temp))
            transport = app.ColabProvider(control, app.runtime_services)
            client = MagicMock()
            client.list_assignments.return_value = []
            transport.client = lambda: client
            with self.assertRaises(RuntimeError):
                transport.connect(app.H3JobConfig(prompt='test', endpoint='not-managed-here'))
            client.assign.assert_not_called()

    def test_g4_request_uses_g4_and_blocks_switching_owned_a100(self):
        control = MagicMock(session=None, jobs={})
        transport = app.ColabProvider(control, app.runtime_services)
        client = MagicMock()
        client.list_assignments.return_value = []
        client.assign.return_value.endpoint = 'new-g4'
        client.assign.return_value.runtime_proxy_info.url = 'https://example.invalid/'
        client.assign.return_value.runtime_proxy_info.token = 'test-only-token'
        transport.client = lambda: client
        transport.connect(app.H3JobConfig(prompt='test', gpu='G4'))
        self.assertEqual(client.assign.call_args.kwargs['accelerator'], app.Accelerator.G4)
        self.assertIsNone(client.assign.call_args.kwargs['shape'])
        self.assertEqual(control.session['accelerator'], 'G4')
        control.session['accelerator'] = 'A100'
        client.assign.reset_mock()
        with self.assertRaises(RuntimeError):
            transport.connect(app.H3JobConfig(prompt='test', gpu='G4'))
        client.assign.assert_not_called()

    def test_native_pdd_graph(self):
        c = app.H3JobConfig(prompt='test', veda=False).model_dump()
        graph = build_graph(dict(c, job_id='test'))
        self.assertEqual(graph['6']['inputs']['lora_name'], PDD)
        self.assertEqual(graph['16']['inputs']['format.codec'], 'h264')
        self.assertEqual(graph['11']['inputs']['model'], ['18', 0])
        self.assertEqual(graph['18']['inputs']['shift_video'], 12.0)
        self.assertEqual(graph['5']['inputs']['length'], 124)

    def test_base_without_pdd(self):
        c = app.H3JobConfig(prompt='test', mode='base', people=False).model_dump()
        graph = build_graph(dict(c, job_id='test'))
        self.assertNotIn('6', graph)
        self.assertNotIn('7', graph)
        self.assertEqual(graph['11']['inputs']['steps'], 20)

    def test_dpapi_roundtrip(self):
        secret = b'test-only-no-credentials'
        protected = app.protect(secret)
        self.assertNotIn(secret, protected)
        self.assertEqual(app.protect(protected, decrypt=True), secret)

    def test_unsafe_output_path_rejected(self):
        transport = app.ColabProvider(self, app.runtime_services)
        with self.assertRaises(RuntimeError):
            transport.download({'path': '/content/ComfyUI/output/../../secret'}, Path('ignored'))

    def test_validation(self):
        for valid_duration in (5, 10, 12, 15):
            self.assertEqual(app.H3JobConfig(prompt='x', duration=valid_duration).duration, valid_duration)
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            app.H3JobConfig(prompt='x', duration=100)
        with self.assertRaises(ValidationError):
            app.H3JobConfig(prompt='x', seed=-1)

    def test_creator_canvases_are_aligned_within_proven_budget(self):
        for resolution, formats in DIMENSIONS.items():
            budget = 1344 * 768 if resolution == '768p' else 864 * 480
            for ratio, (width, height) in formats.items():
                with self.subTest(resolution=resolution, ratio=ratio):
                    config = app.H3JobConfig(prompt='test', resolution=resolution, aspect_ratio=ratio).model_dump()
                    graph = build_graph(dict(config, job_id='test'))
                    actual = graph['5']['inputs']
                    self.assertEqual((actual['width'], actual['height']), (width, height))
                    self.assertEqual(width % 32, 0)
                    self.assertEqual(height % 32, 0)
                    self.assertLessEqual(width * height, budget)
                    rw, rh = map(int, ratio.split(':'))
                    self.assertLess(abs(width / height / (rw / rh) - 1), .02)

    def test_default_canvas_is_landscape_and_preserves_prompt(self):
        config = app.H3JobConfig(prompt='original', people=False).model_dump()
        inputs = build_graph(dict(config, job_id='test'))['5']['inputs']
        self.assertEqual((inputs['width'], inputs['height']), (1344, 768))
        self.assertEqual(inputs['prompt'], 'original')

    def test_image_crop_matches_selected_canvas(self):
        for ratio in DIMENSIONS['768p']:
            with self.subTest(ratio=ratio):
                config = app.H3JobConfig(prompt='test', aspect_ratio=ratio).model_dump()
                graph = build_graph(dict(config, job_id='test', references=[dict(slot=1,image_id='test.png',frame='first',role='other',description='')]))
                image = graph['40']['inputs']
                self.assertEqual(graph['5']['inputs']['first_frame'], ['40', 0])
                self.assertEqual(image['crop'], 'center')
                self.assertEqual(image['image'], ['30', 0])
                self.assertEqual((image['width'], image['height']), DIMENSIONS['768p'][ratio])

    def test_retired_request_fields_are_rejected(self):
        from pydantic import ValidationError
        for fields in ({'camera':'orbit'}, {'auto_release':True}, {'image_id':'a'*32+'.png'}, {'prompt_enhance':True}, {'generation_type':'reference'}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                app.H3JobConfig(prompt='test', **fields)

    def test_creator_validation_before_allocation(self):
        from pydantic import ValidationError
        for fields in ({'aspect_ratio':'2:1'}, {'title':'x' * 81}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                app.H3JobConfig(prompt='test', **fields)

    def test_public_creation_can_restore_settings_without_session_secrets(self):
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as temp:
            control = app.Controller(Path(temp), Path(temp))
            config = app.H3JobConfig(prompt='test', title='作品', aspect_ratio='9:16', endpoint='private-endpoint', references=[dict(slot=1,image_id='a'*32+'.png',frame='first')]).model_dump()
            job = control.public_job({'id':'test', 'status':'done', 'config':config})
            self.assertEqual(job['creation']['aspect_ratio'], '9:16')
            self.assertEqual(job['creation']['title'], '作品')
            self.assertEqual(job['creation']['references'][0]['frame'],'first')
            self.assertNotIn('endpoint', job['creation'])
            self.assertNotIn('image_id', job['creation'])


if __name__ == '__main__':
    unittest.main()

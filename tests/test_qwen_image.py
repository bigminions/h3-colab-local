"""Tests for Qwen-Image-2.1 application and remote orchestration."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from pydantic import ValidationError

import app
from apps.qwen_image import (
    DIFFUSION_MODELS,
    DIMENSIONS,
    ENCODER_MODELS,
    REPO_ID,
    VAE_MODEL,
    QwenImageApplication,
    QwenImageJobConfig,
    ReferenceImage,
    build_graph,
    files_for,
)
from asset_library import AssetLibrary


class QwenImageConfigTests(unittest.TestCase):
    def test_default_config_validates_successfully(self):
        cfg = QwenImageJobConfig(prompt='一只可爱的白色小猫在草地上玩耍')
        self.assertEqual(cfg.app_id, 'qwen_image_2_1')
        self.assertEqual(cfg.provider_id, 'colab')
        self.assertEqual(cfg.model_file, 'qwen-image-2.1-turbo-Q4_K_M.gguf')
        self.assertEqual(cfg.encoder, 'int8_convrot')
        self.assertEqual(cfg.aspect_ratio, '1:1')
        self.assertEqual(cfg.steps, 8)
        self.assertEqual(cfg.cfg, 1.0)
        self.assertEqual(cfg.count, 1)
        self.assertEqual(cfg.generation_type, 'text')
        self.assertEqual(cfg.gpu, 'T4')

    def test_legacy_model_file_alias_normalization(self):
        cfg = QwenImageJobConfig(prompt='test prompt', model_file='qwen-image-2.1-UC-Q4_K_M.gguf')
        self.assertEqual(cfg.model_file, 'qwen-image-2.1-turbo-Q4_K_M.gguf')
        files = files_for({'model_file': 'qwen-image-2.1-UC-Q4_K_M.gguf'})
        self.assertEqual(files[0][1], 'qwen-image-2.1-turbo-Q4_K_M.gguf')
        g = build_graph({'model_file': 'qwen-image-2.1-UC-Q4_K_M.gguf'})
        self.assertEqual(g['1']['inputs']['unet_name'], 'qwen-image-2.1-turbo-Q4_K_M.gguf')

    def test_prompt_validation(self):
        with self.assertRaises(ValidationError):
            QwenImageJobConfig(prompt='')
        with self.assertRaises(ValidationError):
            QwenImageJobConfig(prompt='   ')

    def test_aspect_ratios_and_dimensions(self):
        for ratio, (w, h) in DIMENSIONS.items():
            cfg = QwenImageJobConfig(prompt='test', aspect_ratio=ratio)
            self.assertEqual(cfg.aspect_ratio, ratio)
            graph = build_graph(cfg.model_dump())
            latent = graph['6']['inputs']
            self.assertEqual(latent['width'], w)
            self.assertEqual(latent['height'], h)

    def test_reference_images_set_image_generation_type(self):
        ref_id = 'a' * 32 + '.png'
        cfg = QwenImageJobConfig(
            prompt='根据参考图重新生成',
            references=[ReferenceImage(slot=1, image_id=ref_id)],
            denoise=0.6,
        )
        self.assertEqual(cfg.generation_type, 'image')
        self.assertEqual(cfg.denoise, 0.6)

    def test_duplicate_reference_slots_rejected(self):
        ref_id = 'a' * 32 + '.png'
        with self.assertRaises(ValueError):
            QwenImageJobConfig(
                prompt='test',
                references=[
                    ReferenceImage(slot=1, image_id=ref_id),
                    ReferenceImage(slot=1, image_id=ref_id),
                ],
            )


class QwenImageApplicationTests(unittest.TestCase):
    def setUp(self):
        self.application = QwenImageApplication()

    def test_application_spec(self):
        spec = self.application.spec
        self.assertEqual(spec.id, 'qwen_image_2_1')
        self.assertEqual(spec.name, 'Qwen-Image-2.1')
        self.assertEqual(spec.output_kind, 'image')
        self.assertIn('colab', spec.providers)
        purposes = [p['id'] for p in spec.asset_purposes]
        self.assertIn('references', purposes)
        self.assertIn('outputs', purposes)

    def test_source_and_invocation(self):
        source = self.application.source(app.BASE, {})
        self.assertIn('ComfyUI', source)
        self.assertIn('Qwen-Image', source)
        self.assertIn('UnetLoaderGGUF', source)

        job = {'id': 'test_job_123', 'config': {'app_id': 'qwen_image_2_1', 'prompt': 'test'}}
        invocation = self.application.invocation(source, job)
        self.assertIn('test_job_123', invocation)
        self.assertIn('run(', invocation)

    def test_input_assets_extracted(self):
        ref1 = 'a' * 32 + '.png'
        ref2 = 'b' * 32 + '.jpg'
        config = {'references': [{'slot': 1, 'image_id': ref1}, {'slot': 2, 'image_id': ref2}]}
        assets = self.application.input_assets(config)
        self.assertEqual(assets, [ref1, ref2])

    def test_idle_probe(self):
        self.assertEqual(self.application.idle_marker, 'QWEN_IMAGE_IDLE_CHECK ')
        self.assertIn('8188', self.application.idle_probe)
        self.assertIn('queue', self.application.idle_probe)


class QwenImageRemoteWorkflowTests(unittest.TestCase):
    def test_files_for_huggingface_repo(self):
        cfg = {'model_file': 'qwen-image-2.1-turbo-Q4_K_M.gguf', 'encoder': 'int8_convrot'}
        files = files_for(cfg)
        self.assertEqual(len(files), 3)
        self.assertTrue(all(item[0] == REPO_ID for item in files))
        self.assertEqual(files[0][1], 'qwen-image-2.1-turbo-Q4_K_M.gguf')
        self.assertEqual(files[0][2], 'diffusion_models')
        self.assertEqual(files[1][1], 'text_encoders/qwen3vl_8b_int8_convrot.safetensors')
        self.assertEqual(files[1][2], 'text_encoders')
        self.assertEqual(files[2][1], 'vae/qwen_image_2.1_vae_bf16.safetensors')
        self.assertEqual(files[2][2], 'vae')

    def test_build_graph_gguf_t2i(self):
        cfg = {
            'job_id': 'job_gguf_01',
            'prompt': 'a fantasy castle at sunset',
            'negative_prompt': 'blurry, distorted',
            'model_file': 'qwen-image-2.1-turbo-Q4_K_M.gguf',
            'encoder': 'int8_convrot',
            'aspect_ratio': '16:9',
            'steps': 30,
            'cfg': 4.5,
            'seed': 12345,
        }
        g = build_graph(cfg)
        self.assertEqual(g['1']['class_type'], 'UnetLoaderGGUF')
        self.assertEqual(g['1']['inputs']['unet_name'], 'qwen-image-2.1-turbo-Q4_K_M.gguf')
        self.assertEqual(g['2']['class_type'], 'CLIPLoader')
        self.assertEqual(g['2']['inputs']['type'], 'qwen_image')
        self.assertEqual(g['3']['class_type'], 'VAELoader')
        self.assertEqual(g['4']['inputs']['text'], 'a fantasy castle at sunset')
        self.assertEqual(g['5']['inputs']['text'], 'blurry, distorted')
        self.assertEqual(g['6']['class_type'], 'EmptyLatentImage')
        self.assertEqual(g['6']['inputs']['width'], 1344)
        self.assertEqual(g['6']['inputs']['height'], 768)
        self.assertEqual(g['7']['class_type'], 'SamplerCustomAdvanced')
        self.assertEqual(g['10']['class_type'], 'RandomNoise')
        self.assertEqual(g['10']['inputs']['noise_seed'], 12345)
        self.assertEqual(g['11']['class_type'], 'KSamplerSelect')
        self.assertEqual(g['12']['class_type'], 'BasicScheduler')
        self.assertEqual(g['12']['inputs']['steps'], 30)
        self.assertEqual(g['13']['class_type'], 'CFGGuider')
        self.assertEqual(g['13']['inputs']['cfg'], 4.5)
        self.assertEqual(g['8']['class_type'], 'VAEDecode')
        self.assertEqual(g['9']['class_type'], 'SaveImage')
        self.assertIn('job_gguf_01', g['9']['inputs']['filename_prefix'])

    def test_build_graph_safetensors_i2i(self):
        ref_id = 'c' * 32 + '.png'
        cfg = {
            'job_id': 'job_st_02',
            'prompt': 'cyberpunk transformation',
            'model_file': 'qwen-image-2.1-turbo-fp8.safetensors',
            'encoder': 'bf16',
            'aspect_ratio': '1:1',
            'references': [{'slot': 1, 'image_id': ref_id}],
            'denoise': 0.7,
        }
        g = build_graph(cfg)
        self.assertEqual(g['1']['class_type'], 'UNETLoader')
        self.assertEqual(g['1']['inputs']['unet_name'], 'qwen-image-2.1-turbo-fp8.safetensors')
        self.assertEqual(g['20']['class_type'], 'LoadImage')
        self.assertEqual(g['20']['inputs']['image'], ref_id)
        self.assertEqual(g['21']['class_type'], 'ImageScale')
        self.assertEqual(g['6']['class_type'], 'VAEEncode')
        self.assertEqual(g['7']['class_type'], 'SamplerCustomAdvanced')
        self.assertEqual(g['14']['class_type'], 'SplitSigmasDenoise')
        self.assertEqual(g['14']['inputs']['denoise'], 0.7)

    def test_build_graph_turbo_8step_manual_sigmas(self):
        cfg = {
            'job_id': 'job_turbo_8',
            'prompt': 'a beautiful cat',
            'steps': 8,
            'cfg': 1.0,
            'seed': 42,
        }
        g = build_graph(cfg)
        self.assertEqual(g['7']['class_type'], 'SamplerCustomAdvanced')
        self.assertEqual(g['12']['class_type'], 'ManualSigmas')
        self.assertIn('0.978453', g['12']['inputs']['sigmas'])
        self.assertEqual(g['13']['class_type'], 'BasicGuider')


class QwenImageWorkspaceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(app, 'PRIVATE', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.library = AssetLibrary(self.root / 'materials')
        patcher = patch.object(app, 'asset_library', self.library)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.control = app.Controller(self.root, self.root / 'materials' / 'h3' / 'outputs' / 'videos')
        patcher = patch.object(app, 'controller', self.control)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_verify_saved_results_supports_image_application(self):
        ep = 'test-endpoint-img'
        img_id = 'e' * 32 + '.png'
        out_folder = self.library.folder('images', 'qwen_image_2_1', 'outputs')
        out_folder.mkdir(parents=True, exist_ok=True)
        target_img = out_folder / img_id
        content = b'fake-png-content'
        target_img.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()

        self.control.jobs['img_job_1'] = {
            'id': 'img_job_1',
            'endpoint': ep,
            'config': {'app_id': 'qwen_image_2_1'},
            'file': img_id,
            'remote_result': {'size': len(content), 'sha256': digest},
        }

        self.control.verify_saved_results(ep)

        target_img.write_bytes(b'corrupted')
        with self.assertRaises(app.HTTPException) as ctx:
            self.control.verify_saved_results(ep)
        self.assertIn('图片', str(ctx.exception.detail))

    def test_qwen_image_reference_resolution_and_public_job_enrichment(self):
        ref_dir = self.library.folder('images', 'qwen_image_2_1', 'references')
        ref_dir.mkdir(parents=True, exist_ok=True)
        ref_img_id = 'c' * 32 + '.jpg'
        (ref_dir / ref_img_id).write_bytes(b'test-jpg-data')
        (ref_dir / (ref_img_id + '.json')).write_text(json.dumps({'name': 'my_custom_photo.jpg'}), encoding='utf-8')

        resolved = self.library.resolve(ref_img_id)
        self.assertEqual(resolved, ref_dir / ref_img_id)
        self.assertTrue(self.library.exists(ref_img_id))

        job = {
            'id': 'test_job_enrich',
            'status': 'done',
            'config': {
                'app_id': 'qwen_image_2_1',
                'prompt': 'a test prompt',
                'references': [{'slot': 1, 'image_id': ref_img_id}]
            }
        }
        pub = self.control.public_job(job)
        self.assertIn('references', pub['creation'])
        ref_info = pub['creation']['references'][0]
        self.assertEqual(ref_info['image_id'], ref_img_id)
        self.assertEqual(ref_info['name'], 'my_custom_photo.jpg')

    def test_workspace_endpoint_exposes_qwen_image(self):
        data = app.workspace()
        app_ids = [a['id'] for a in data['applications']]
        self.assertIn('h3', app_ids)
        self.assertIn('qwen_image_2_1', app_ids)

        scopes = data['library']['scopes']
        qwen_scopes = [s for s in scopes if s['app_id'] == 'qwen_image_2_1']
        self.assertEqual(len(qwen_scopes), 2)
        purposes = {s['purpose'] for s in qwen_scopes}
        self.assertEqual(purposes, {'references', 'outputs'})

    def test_job_submission_and_image_download_flow(self):
        (self.root / 'google-token.dat').touch()
        cfg = QwenImageJobConfig(prompt='a serene mountain lake at dawn')
        with patch.object(app.controller, 'start', return_value='test_image_job'), patch.object(app.controller, 'jobs', {'test_image_job': {}}):
            res = app.create_job(cfg)
            self.assertEqual(res['id'], 'test_image_job')

    def test_recycle_and_recover_qwen_image_job(self):
        fake_bin = self.root / 'fake-bin'
        fake_bin.mkdir(exist_ok=True)
        self.control.history_recycle.sender = lambda f: f.replace(fake_bin / f.name)

        job_id = 'e' * 32
        out_folder = self.library.folder('images', 'qwen_image_2_1', 'outputs')
        out_folder.mkdir(parents=True, exist_ok=True)
        img_file = out_folder / (job_id + '.png')
        img_file.write_bytes(b'TEST_PNG_BYTES')

        job = {
            'id': job_id,
            'config': {
                'app_id': 'qwen_image_2_1',
                'provider_id': 'colab',
                'prompt': 'a test image prompt',
            },
            'status': 'done',
            'created': 1000,
            'events': [],
            'file': job_id + '.png',
            'metadata': {'width': 1024, 'height': 1024},
        }
        self.control.jobs[job_id] = job

        # 1. Recycle the image job
        result = self.control.recycle_history(job_id)
        self.assertEqual(result['ids'], [job_id])
        self.assertFalse(result['errors'])
        self.assertTrue(job['history_hidden'])
        self.assertFalse(img_file.exists())

        # Verify staged folder in fake bin
        staged = next(fake_bin.iterdir())
        self.assertTrue((staged / (job_id + '.png')).is_file())

        # 2. Recover from bin
        staged.replace(self.control.history_recycle.directory / staged.name)
        self.control.recover_history()
        self.assertFalse(self.control.jobs[job_id].get('history_hidden'))
        self.assertTrue(img_file.is_file())
        self.assertEqual(img_file.read_bytes(), b'TEST_PNG_BYTES')

    def test_controller_saves_generated_image_to_asset_library_folder(self):
        # Create a synthetic image and simulate transport download
        img_buffer = io.BytesIO()
        Image.new('RGB', (1024, 1024), color='teal').save(img_buffer, format='PNG')
        img_bytes = img_buffer.getvalue()
        img_sha = hashlib.sha256(img_bytes).hexdigest()

        job_id = 'd' * 32
        job = {
            'id': job_id,
            'config': {
                'app_id': 'qwen_image_2_1',
                'provider_id': 'colab',
                'prompt': 'a beautiful garden',
                'gpu': 'A100',
            },
            'status': 'running',
            'created': 1000,
            'events': [],
            'remote_result': {
                'path': '/content/ComfyUI/output/QwenImage/local_' + job_id + '.png',
                'size': len(img_bytes),
                'sha256': img_sha,
                'width': 1024,
                'height': 1024,
            },
        }
        self.control.jobs[job_id] = job

        fake_transport = MagicMock()
        def fake_download(result, target):
            target.write_bytes(img_bytes)
        fake_transport.download = fake_download
        fake_transport.release = MagicMock()
        self.control.transport = fake_transport
        self.control.session = {'endpoint': 'test_endpoint', 'accelerator': 'A100'}

        self.control.run(job)

        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['file'], job_id + '.png')
        self.assertEqual(job['metadata']['width'], 1024)
        self.assertEqual(job['metadata']['height'], 1024)

        # Verify physical file in materials/qwen_image_2_1/outputs/images/
        expected_path = self.library.resolve(job_id + '.png')
        self.assertTrue(expected_path.is_file())
        self.assertEqual(expected_path.relative_to(self.library.root).parts[:3], ('qwen_image_2_1', 'outputs', 'images'))

        # Verify public_job local_file reporting
        pub = self.control.public_job(job)
        self.assertTrue(pub['local_file'])
        self.assertEqual(pub['app_id'], 'qwen_image_2_1')
        self.assertEqual(pub['creation']['prompt'], 'a beautiful garden')





class QwenImageFrontendModularityTests(unittest.TestCase):
    def test_modular_frontend_directories_and_assets_exist(self):
        h3_web = Path('apps/h3/web')
        self.assertTrue((h3_web / 'content.html').is_file())
        self.assertTrue((h3_web / 'workbench.css').is_file())
        self.assertTrue((h3_web / 'workbench.js').is_file())
        self.assertTrue((h3_web / 'characters.js').is_file())

        qi_web = Path('apps/qwen_image/web')
        self.assertTrue((qi_web / 'content.html').is_file())
        self.assertTrue((qi_web / 'qwen-image.css').is_file())
        self.assertTrue((qi_web / 'qwen-image.js').is_file())

    def test_render_studio_html_assembles_modular_apps(self):
        html = app.render_studio_html()
        self.assertNotIn('<!-- APP_CONTENTS -->', html)
        self.assertNotIn('<!-- APP_STYLES -->', html)
        self.assertNotIn('<!-- APP_SCRIPTS -->', html)

        self.assertIn('id="toolPage"', html)
        self.assertIn('id="characterPage"', html)
        self.assertIn('id="qwenImagePage"', html)

        self.assertIn('/apps/h3/web/workbench.css', html)
        self.assertIn('/apps/qwen_image_2_1/web/qwen-image.css', html)
        self.assertIn('/apps/h3/web/workbench.js', html)
        self.assertIn('/apps/h3/web/characters.js', html)
        self.assertIn('/apps/qwen_image_2_1/web/qwen-image.js', html)

    def test_application_assets_are_served_from_application_routes(self):
        from starlette.testclient import TestClient
        client = TestClient(app.app)
        headers = {'host': f'127.0.0.1:{app.PORT}'}
        html = client.get('/', headers=headers).text
        for application in app.applications:
            for asset in application.web_dir.iterdir():
                if asset.suffix not in ('.js', '.css'):
                    continue
                url = f'/apps/{application.spec.id}/web/{asset.name}'
                with self.subTest(url=url):
                    self.assertIn(url, html)
                    response = client.get(url, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, asset.read_bytes())
                    self.assertEqual(client.get('/static/' + asset.name, headers=headers).status_code, 404)

    def test_workspace_css_is_decoupled_from_app_specific_styles(self):
        ws_css = Path('static/workspace.css').read_text(encoding='utf-8')
        self.assertNotIn('/* Qwen-Image-2.1 styles */', ws_css)
        self.assertNotIn('#qwenImagePage', ws_css)
        self.assertNotIn('#qiGenerateForm', ws_css)
        self.assertIn('display: none !important', ws_css)

    def test_qwen_image_page_tabs_initially_hidden_to_prevent_collision(self):
        studio_html = Path('studio.html').read_text(encoding='utf-8')
        self.assertIn('id="qwenImagePageTabs"', studio_html)
        self.assertIn('style="display:none;"', studio_html)

    def test_csp_allows_inline_styles_and_scripts_for_tailwind(self):
        from starlette.testclient import TestClient
        client = TestClient(app.app)
        response = client.get('/', headers={'host': '127.0.0.1:7865'})
        self.assertEqual(response.status_code, 200)
        csp = response.headers.get('Content-Security-Policy', '')
        self.assertIn("style-src 'self' 'unsafe-inline'", csp)
        self.assertIn("script-src 'self' 'unsafe-inline'", csp)


    def test_dollar_helper_defined_in_shell_and_workspace(self):
        studio_html = Path('studio.html').read_text(encoding='utf-8')
        self.assertIn('window.$ = id => document.getElementById(id)', studio_html)
        ws_js = Path('static/workspace.js').read_text(encoding='utf-8')
        self.assertIn('const $ = window.$ || (id => document.getElementById(id));', ws_js)

    def test_qwen_image_frontend_interactions_and_actions(self):
        js = Path('apps/qwen_image/web/qwen-image.js').read_text(encoding='utf-8')

        css = Path('apps/qwen_image/web/qwen-image.css').read_text(encoding='utf-8')
        self.assertIn('cursor: zoom-in', css)

        # 1. Direct click on image opens lightbox viewer and backdrop closes modal
        self.assertIn('openLightboxViewer(galleryData, 0)', js)
        self.assertIn('openLightboxViewer(galleryData, galleryIdx', js)
        self.assertIn("e.target === modal || e.target.classList?.contains('qi-image-viewer-stage')", js)

        # 2. Card bottom action buttons do not include individual download or set-as-ref
        self.assertIn('function updateCardActionButtons(row, group, currentSibling)', js)
        self.assertIn('打开文件夹', js)
        self.assertIn('全部下载', js)
        update_card_body = js.split('function updateCardActionButtons(row, group, currentSibling) {')[1].split('function statusLabel(status)')[0]
        self.assertNotIn('下载图片', update_card_body)
        self.assertNotIn('设为参考图', update_card_body)

        # 3. applyCreation applies full configurations but excludes seed
        apply_creation_body = js.split('function applyCreation(creation, button) {')[1].split('async function saveImageToLocation')[0]
        self.assertNotIn('qiSeed', apply_creation_body)
        self.assertIn('qiPrompt', apply_creation_body)
        self.assertIn('qiNegativePrompt', apply_creation_body)
        self.assertIn('qiAspectRatio', apply_creation_body)
        self.assertIn('qiModelFile', apply_creation_body)
        self.assertIn('qiSteps', apply_creation_body)
        self.assertIn('qiCfg', apply_creation_body)

        # 4. '应用提示词' button exists and '复制提示词' is removed
        self.assertIn('应用提示词', js)
        self.assertNotIn('复制提示词', js)

        # 5. Reference preview blob fallback and error handling
        self.assertIn("fetch('/api/images/' + ref.image_id", js)
        self.assertIn("img.onerror", js)
        self.assertIn("ref.name || ref.image_id", js)


if __name__ == '__main__':
    unittest.main()

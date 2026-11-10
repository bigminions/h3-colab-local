"""Tests for Gemma chat workspace, Cloudflare tunnel handling, and streaming replies."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
import requests

import app
from apps.gemma_chat import remote_gemma
from apps.gemma_chat.application import APP_ID, GemmaJobConfig, OpenRequest, SendRequest
from apps.gemma_chat.workspace import GemmaWorkspace


class GemmaChatTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.temp_dir.name)
        self.controller = app.Controller(self.path, self.path)
        self.controller.ensure_worker = MagicMock()
        self.workspace = GemmaWorkspace(self.controller, authorized=lambda: True)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except OSError:
            pass

    def test_open_submits_job_with_generated_secret(self):
        result = self.workspace.open(OpenRequest(variant='native', gpu='L4'))
        self.assertIn('id', result)
        job = self.controller.jobs[result['id']]
        self.assertEqual(job['config']['operation'], 'open')
        self.assertEqual(job['config']['variant'], 'native')
        self.assertTrue(job['config'].get('secret'))
        self.assertEqual(len(job['config']['secret']), 32)

    def test_stream_requires_ready_deployment(self):
        with self.assertRaises(HTTPException) as cm:
            self.workspace.stream('0' * 32, SendRequest(prompt='hello'))
        self.assertEqual(cm.exception.status_code, 409)

    def test_stream_dispatches_to_tunnel_and_persists_job(self):
        deploy_job = dict(
            id='deploy1',
            config=dict(app_id=APP_ID, operation='open', variant='native', gpu='L4', secret='test-secret'),
            status='running',
            chat_session='ready',
            remote_result={'ready': True, 'endpoint': 'https://test-tunnel.trycloudflare.com', 'secret': 'test-secret'},
            created=100.0,
            safe_release=True,
            events=[]
        )
        self.controller.jobs['deploy1'] = deploy_job
        self.controller.session = dict(app_id=APP_ID, gemma_variant='native')

        fake_chunks = [
            b'data: {"choices":[{"delta":{"content":"Hi"}}]}',
            b'data: {"choices":[{"delta":{"content":" there!"}}],"timings":{"predicted_per_second":25.0}}',
            b'data: [DONE]',
        ]
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.iter_lines.return_value = fake_chunks
        mock_response.status_code = 200

        with patch('requests.post', return_value=mock_response) as post_mock:
            response = self.workspace.stream('a' * 32, SendRequest(prompt='How are you?'))

            async def read_chunks():
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk if isinstance(chunk, str) else chunk.decode('utf-8'))
                return chunks

            body_chunks = asyncio.run(read_chunks())

        post_mock.assert_called_once()
        call_url = post_mock.call_args[0][0]
        self.assertEqual(call_url, 'https://test-tunnel.trycloudflare.com/v1/chat/completions')
        headers = post_mock.call_args[1].get('headers', {})
        self.assertEqual(headers.get('Authorization'), 'Bearer test-secret')

        content = ''.join(body_chunks)
        self.assertIn('Hi', content)
        self.assertIn(' there!', content)
        self.assertIn('[DONE]', content)

        chat_jobs = self.workspace.chat_jobs('a' * 32)
        self.assertEqual(len(chat_jobs), 1)
        job = chat_jobs[0]
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['remote_result']['text'], 'Hi there!')
        self.assertEqual(job['remote_result']['metrics']['tokens_per_second'], 25.0)

    def test_ensure_tunnel_parses_url_and_probes_health(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            log_file = root / 'tunnel.log'

            def fake_popen(*args, **kwargs):
                log_file.write_text('INF Your quick Tunnel: https://my-cool-tunnel.trycloudflare.com\n', encoding='utf-8')
                proc = MagicMock()
                proc.poll.return_value = None
                return proc

            with patch.object(remote_gemma, 'ROOT', root), \
                    patch.object(remote_gemma, 'download_verified'), \
                    patch('subprocess.Popen', side_effect=fake_popen), \
                    patch('requests.get') as get_mock:
                get_mock.return_value.ok = True
                url = remote_gemma.ensure_tunnel(18829, secret='sec')
                self.assertEqual(url, 'https://my-cool-tunnel.trycloudflare.com')
                self.assertEqual((root / 'tunnel.url').read_text().strip(), 'https://my-cool-tunnel.trycloudflare.com')

    def test_snapshot_returns_full_events_without_truncation(self):
        many_events = [{'time': f'10:00:{i:02d}', 'message': f'Step {i}'} for i in range(25)]
        deploy_job = dict(
            id='deploy_long',
            config=dict(app_id=APP_ID, operation='open', variant='native', gpu='L4'),
            status='running',
            events=many_events,
            created=100.0,
            safe_release=False,
        )
        self.controller.jobs['deploy_long'] = deploy_job
        snap = self.workspace.snapshot()
        self.assertEqual(snap['state'], 'loading')
        self.assertEqual(len(snap['events']), 25)
        self.assertEqual(snap['events'][0]['message'], 'Step 0')
        self.assertEqual(snap['events'][-1]['message'], 'Step 24')

    def test_gemma_html_and_assets_integrated(self):
        html = app.render_studio_html()
        self.assertIn('/apps/gemma_chat/web/gemma.css', html)
        self.assertIn('/apps/gemma_chat/web/gemma.js', html)
        self.assertIn('/apps/gemma_chat/web/debug.js', html)
        required_ids = [
            'gemmaPage', 'gemmaPrompt', 'gemmaSend', 'gemmaStop',
            'gemmaEmpty', 'gemmaMessages', 'gemmaChatList', 'gemmaChatMenu',
            'gemmaSetup', 'gemmaOpenForm', 'gemmaGpuCard', 'gemmaGpu',
            'gemmaLoading', 'gemmaLoadLog', 'gemmaDebugPanel', 'gemmaStatusBadge',
            'gemmaCharacterGrid'
        ]
        for rid in required_ids:
            self.assertIn(f'id="{rid}"', html)

    def test_gemma_js_injects_local_api_key_header(self):
        js = (Path(__file__).parent.parent / 'apps' / 'gemma_chat' / 'web' / 'gemma.js').read_text(encoding='utf-8')
        self.assertIn("'X-LStudio-Key': localKey", js)

    def test_stream_api_requires_local_api_key(self):
        from starlette.testclient import TestClient
        client = TestClient(app.app, base_url='http://127.0.0.1:7865')
        # Without X-LStudio-Key header: 401
        res = client.post('/api/gemma/chats/0123456789abcdef0123456789abcdef/stream', json={'prompt': 'hi'})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json(), {'detail': '需要本地 API Key'})

        # With correct X-LStudio-Key header: passes middleware auth (proceeds to business logic)
        res_auth = client.post(
            '/api/gemma/chats/0123456789abcdef0123456789abcdef/stream',
            json={'prompt': 'hi'},
            headers={'X-LStudio-Key': app.API_KEY}
        )
        # Should not be 401; will be 409 (since model is not opened)
        self.assertNotEqual(res_auth.status_code, 401)

    def test_stream_injects_character_system_prompt_and_persists_config(self):
        deploy_job = dict(
            id='deploy_char',
            config=dict(app_id=APP_ID, operation='open', variant='native', gpu='L4', secret='sec'),
            status='running',
            chat_session='ready',
            remote_result={'ready': True, 'endpoint': 'https://tunnel.example.com', 'secret': 'sec'},
            created=100.0,
            safe_release=True,
            events=[]
        )
        self.controller.jobs['deploy_char'] = deploy_job
        self.controller.session = dict(app_id=APP_ID, gemma_variant='native')

        fake_chunks = [
            b'data: {"choices":[{"delta":{"content":"Case accepted."}}]}',
            b'data: [DONE]',
        ]
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.iter_lines.return_value = fake_chunks
        mock_response.status_code = 200

        with patch('requests.post', return_value=mock_response) as post_mock:
            req = SendRequest(
                prompt='今天天气真好！',
                character_id='scarlett',
                character_name='林薇',
                system_prompt='You are Scarlett, an enthusiastic young woman.'
            )
            response = self.workspace.stream('c' * 32, req)

            async def consume():
                async for _ in response.body_iterator:
                    pass
            asyncio.run(consume())

        post_mock.assert_called_once()
        sent_body = post_mock.call_args[1].get('json', {})
        self.assertEqual(len(sent_body.get('messages', [])), 2)
        self.assertEqual(sent_body['messages'][0], {
            'role': 'system',
            'content': 'You are Scarlett, an enthusiastic young woman.'
        })
        self.assertEqual(sent_body['messages'][1], {
            'role': 'user',
            'content': '今天天气真好！'
        })

        chat_jobs = self.workspace.chat_jobs('c' * 32)
        self.assertEqual(len(chat_jobs), 1)
        job = chat_jobs[0]
        self.assertEqual(job['config']['character_id'], 'scarlett')
        self.assertEqual(job['config']['character_name'], '林薇')
        self.assertEqual(job['config']['system_prompt'], 'You are Scarlett, an enthusiastic young woman.')

        snap = self.workspace.snapshot('c' * 32)
        self.assertEqual(len(snap['turns']), 1)
        self.assertEqual(snap['turns'][0]['character_id'], 'scarlett')
        self.assertEqual(snap['turns'][0]['character_name'], '林薇')
        self.assertEqual(len(snap['chats']), 1)
        self.assertEqual(snap['chats'][0]['character_id'], 'scarlett')
        self.assertEqual(snap['chats'][0]['character_name'], '林薇')

    def test_subsequent_stream_inherits_character_from_first_turn_in_chat(self):
        deploy_job = dict(
            id='deploy_inherit',
            config=dict(app_id=APP_ID, operation='open', variant='native', gpu='L4', secret='sec'),
            status='running',
            chat_session='ready',
            remote_result={'ready': True, 'endpoint': 'https://tunnel.example.com', 'secret': 'sec'},
            created=100.0,
            safe_release=True,
            events=[]
        )
        self.controller.jobs['deploy_inherit'] = deploy_job
        self.controller.session = dict(app_id=APP_ID, gemma_variant='native')

        fake_chunks = [
            b'data: {"choices":[{"delta":{"content":"Roger that."}}]}',
            b'data: [DONE]',
        ]
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.iter_lines.return_value = fake_chunks
        mock_response.status_code = 200

        async def consume(resp):
            async for _ in resp.body_iterator:
                pass

        # First message: leo
        with patch('requests.post', return_value=mock_response):
            req1 = SendRequest(
                prompt='Hello!',
                character_id='leo',
                character_name='陆扬',
                system_prompt='You are Leo, a cheerful young man.'
            )
            resp1 = self.workspace.stream('d' * 32, req1)
            asyncio.run(consume(resp1))

        # Second message: no character info sent (e.g. continuing conversation)
        with patch('requests.post', return_value=mock_response) as post_mock2:
            req2 = SendRequest(prompt='Can you check this python code?')
            resp2 = self.workspace.stream('d' * 32, req2)
            asyncio.run(consume(resp2))

        sent_body2 = post_mock2.call_args[1].get('json', {})
        # Should have: system, user 1, assistant 1, user 2
        self.assertEqual(sent_body2['messages'][0], {
            'role': 'system',
            'content': 'You are Leo, a cheerful young man.'
        })
        self.assertEqual(sent_body2['messages'][-1], {
            'role': 'user',
            'content': 'Can you check this python code?'
        })

        chat_jobs = self.workspace.chat_jobs('d' * 32)
        self.assertEqual(len(chat_jobs), 2)
        self.assertEqual(chat_jobs[1]['config']['character_id'], 'leo')
        self.assertEqual(chat_jobs[1]['config']['character_name'], '陆扬')

    def test_character_crud_and_persistence_in_store(self):
        # 1. Initially get_characters returns exactly 3 builtin characters
        chars = self.workspace.get_characters()
        ids = [c['id'] for c in chars]
        self.assertEqual(len(chars), 3)
        self.assertEqual(ids, ['assistant', 'scarlett', 'leo'])
        asst = next(c for c in chars if c['id'] == 'assistant')
        self.assertTrue(asst['avatar'].startswith('data:image/jpeg;base64,'))
        self.assertNotIn('persona', asst)

        # 2. Add custom character with compressed base64 avatar
        from apps.gemma_chat.application import CharacterPayload
        hero_avatar = 'data:image/jpeg;base64,' + 'A' * 100
        new_char = CharacterPayload(
            id='custom_hero',
            name='正义超人',
            avatar=hero_avatar,
            tag='守护正义',
            desc='维护和平与正义的伙伴',
            placeholder='呼叫超人！有什么紧急状况？',
            system_prompt='你是一位超级英雄，言行鼓舞人心。'
        )
        saved = self.workspace.save_character_route(new_char)
        self.assertEqual(saved['id'], 'custom_hero')
        self.assertEqual(saved['name'], '正义超人')
        self.assertEqual(saved['avatar'], hero_avatar)
        self.assertNotIn('persona', saved)

        # 3. Verify it is persisted in store and returned by get_characters
        chars_after = self.workspace.get_characters()
        self.assertTrue(any(c['id'] == 'custom_hero' for c in chars_after))

        # 4. Verify snapshot includes it
        snap = self.workspace.snapshot()
        self.assertTrue(any(c['id'] == 'custom_hero' for c in snap['characters']))

        # 5. Delete custom character
        res = self.workspace.delete_character_route('custom_hero')
        self.assertTrue(res['ok'])
        chars_after_del = self.workspace.get_characters()
        self.assertFalse(any(c['id'] == 'custom_hero' for c in chars_after_del))

        # 6. Builtin character cannot be deleted
        with self.assertRaises(HTTPException):
            self.workspace.delete_character_route('assistant')

    def test_edit_default_character_and_reset_to_factory_defaults(self):
        from apps.gemma_chat.application import CharacterPayload
        # 1. Edit default character 'assistant' (小A)
        custom_avatar = 'data:image/jpeg;base64,' + 'B' * 100
        edited_asst = CharacterPayload(
            id='assistant',
            name='小A Pro',
            avatar=custom_avatar,
            tag='高级数智专家',
            desc='拥有极高技术深度的专属智能架构师',
            placeholder='有任何架构与工程问题请问我...',
            system_prompt='你是小A Pro，顶级系统架构专家，风格严谨深刻。',
            builtin=True,
        )
        saved = self.workspace.save_character_route(edited_asst)
        self.assertEqual(saved['id'], 'assistant')
        self.assertEqual(saved['name'], '小A Pro')
        self.assertEqual(saved['avatar'], custom_avatar)
        self.assertTrue(saved['builtin'])
        self.assertTrue(saved['is_customized'])

        # 2. Check get_characters and snapshot
        chars = self.workspace.get_characters()
        asst = next(c for c in chars if c['id'] == 'assistant')
        self.assertEqual(asst['name'], '小A Pro')
        self.assertEqual(asst['avatar'], custom_avatar)
        self.assertEqual(asst['system_prompt'], '你是小A Pro，顶级系统架构专家，风格严谨深刻。')
        self.assertTrue(asst['is_customized'])

        snap = self.workspace.snapshot()
        snap_asst = next(c for c in snap['characters'] if c['id'] == 'assistant')
        self.assertEqual(snap_asst['name'], '小A Pro')

        # 3. Stream turn uses updated system prompt when not explicitly overridden
        self.controller.jobs['deploy_char'] = dict(
            id='deploy_char',
            config=dict(app_id=APP_ID, operation='open', variant='native', gpu='L4', secret='sec'),
            status='running',
            chat_session='ready',
            remote_result={'ready': True, 'endpoint': 'https://tunnel.example.com', 'secret': 'sec'},
            created=100.0,
            safe_release=True,
            events=[]
        )
        _, messages, char_id, char_name, sys_prompt = self.workspace._prepare_history(
            'chat_test_1',
            SendRequest(prompt='测试问题', character_id='assistant')
        )
        self.assertEqual(char_id, 'assistant')
        self.assertEqual(char_name, '小A Pro')
        self.assertEqual(sys_prompt, '你是小A Pro，顶级系统架构专家，风格严谨深刻。')
        self.assertEqual(messages[0], {'role': 'system', 'content': '你是小A Pro，顶级系统架构专家，风格严谨深刻。'})

        # 4. Reset character back to default
        restored = self.workspace.reset_character_route('assistant')
        self.assertEqual(restored['id'], 'assistant')
        self.assertEqual(restored['name'], '小A')
        self.assertFalse(restored.get('is_customized', False))

        chars_after_reset = self.workspace.get_characters()
        asst_after = next(c for c in chars_after_reset if c['id'] == 'assistant')
        self.assertEqual(asst_after['name'], '小A')
        self.assertTrue(asst_after['avatar'].startswith('data:image/jpeg;base64,'))
        self.assertFalse(asst_after['is_customized'])

    def test_gpt_oss_120b_requires_80g_gpu(self):
        # L4 (24GB) and T4 (16GB) are rejected with 400 because 120B requires >= 80GB
        with self.assertRaises(HTTPException) as ctx_l4:
            self.workspace.open(OpenRequest(variant='gpt_oss_120b', gpu='L4'))
        self.assertEqual(ctx_l4.exception.status_code, 400)
        self.assertIn('80GB', ctx_l4.exception.detail)

        with self.assertRaises(HTTPException) as ctx_t4:
            self.workspace.open(OpenRequest(variant='gpt_oss_120b', gpu='T4'))
        self.assertEqual(ctx_t4.exception.status_code, 400)
        self.assertIn('80GB', ctx_t4.exception.detail)

        # A100 (80GB) is accepted
        res = self.workspace.open(OpenRequest(variant='gpt_oss_120b', gpu='A100'))
        self.assertIn('id', res)
        job = self.controller.jobs[res['id']]
        self.assertEqual(job['config']['variant'], 'gpt_oss_120b')
        self.assertEqual(job['config']['gpu'], 'A100')

    def test_gpt_oss_120b_remote_model_spec(self):
        spec = remote_gemma.MODELS.get('gpt_oss_120b')
        self.assertIsNotNone(spec)
        self.assertEqual(spec['alias'], 'lstudio-gpt-oss-120b')
        self.assertEqual(spec['min_vram_gb'], 80)
        self.assertEqual(len(spec['files']), 2)
        self.assertEqual(spec['main_file'], 'gpt-oss-120b-Q4_K_M-00001-of-00002.gguf')

    def test_frontend_renders_gpt_oss_120b_and_80g_notice(self):
        html = app.render_studio_html()
        self.assertIn('value="gpt_oss_120b"', html)
        self.assertIn('GPT-OSS 120B', html)
        self.assertIn('需 80G 显存', html)
        self.assertNotIn('需 A100/H100', html)
        self.assertIn('大模型对话', html)
        self.assertIn('选择模型', html)
        self.assertIn('Gemma 4 31B 原生版', html)
        self.assertIn('Gemma 4 31B 破甲版', html)
        self.assertIn('多模态', html)
        self.assertIn('gemmaAttachBtn', html)
        self.assertIn('gemmaImagePreviewWrap', html)
        self.assertIn('gemmaFileInput', html)

    def test_send_request_multimodal_validation(self):
        # 1. Text only
        req_text = SendRequest(prompt='你好')
        self.assertEqual(req_text.prompt, '你好')
        self.assertEqual(req_text.images, [])

        # 2. Image only
        req_img = SendRequest(prompt='', images=['data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA...'])
        self.assertEqual(req_img.prompt, '')
        self.assertEqual(len(req_img.images), 1)

        # 3. Both text and images
        req_both = SendRequest(prompt='分析图表', images=['data:image/jpeg;base64,/9j/4AAQSkZJRg...'])
        self.assertEqual(req_both.prompt, '分析图表')
        self.assertEqual(len(req_both.images), 1)

        # 4. Neither prompt nor images should fail
        with self.assertRaises(ValueError):
            SendRequest(prompt='', images=[])

    def test_stream_multimodal_message_formatting_and_snapshot(self):
        cid = 'a' * 32
        self.controller.jobs['deploy_mm'] = dict(
            id='deploy_mm',
            config=dict(app_id=APP_ID, operation='open', variant='native', gpu='L4', secret='sec123'),
            status='running',
            chat_session='ready',
            remote_result={'ready': True, 'endpoint': 'https://tunnel.example.com', 'secret': 'sec123'},
            created=100.0,
        )

        test_img = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=='
        payload = SendRequest(prompt='请识别这张图', images=[test_img])
        deployment, messages, char_id, char_name, sys_prompt = self.workspace._prepare_history(cid, payload)

        # User message should be formatted as multimodal content array
        self.assertEqual(len(messages), 1)
        user_msg = messages[0]
        self.assertEqual(user_msg['role'], 'user')
        self.assertIsInstance(user_msg['content'], list)
        self.assertEqual(user_msg['content'][0], {'type': 'text', 'text': '请识别这张图'})
        self.assertEqual(user_msg['content'][1], {'type': 'image_url', 'image_url': {'url': test_img}})

        async def consume(resp):
            res = []
            async for c in resp.body_iterator:
                res.append(c)
            return res

        # Mock streaming reply
        fake_stream = [
            b'data: {"choices":[{"delta":{"content":"\xe8\xbf\x99\xe6\x98\xaf"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"\xe4\xb8\x80\xe5\xbc\xa0\xe5\x9b\xbe\xe7\x89\x87"}}]}\n\n',
            b'data: [DONE]\n\n'
        ]
        with patch('requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.iter_lines.return_value = fake_stream
            mock_post.return_value.__enter__.return_value = mock_resp

            resp = self.workspace.stream(cid, payload)
            chunks = asyncio.run(consume(resp))
            self.assertTrue(len(chunks) > 0)

        # Check snapshot includes turn with images
        snap = self.workspace.snapshot(chat_id=cid)
        self.assertEqual(len(snap['turns']), 1)
        turn = snap['turns'][0]
        self.assertEqual(turn['prompt'], '请识别这张图')
        self.assertEqual(turn['images'], [test_img])
        self.assertEqual(turn['reply'], '这是一张图片')
        self.assertEqual(snap['chats'][0]['title'], '请识别这张图')

        # Subsequent turn retains previous multimodal turn in history
        payload2 = SendRequest(prompt='图片里有什么颜色？')
        _, messages2, _, _, _ = self.workspace._prepare_history(cid, payload2)
        self.assertEqual(len(messages2), 3)
        self.assertEqual(messages2[0]['role'], 'user')
        self.assertIsInstance(messages2[0]['content'], list)
        self.assertEqual(messages2[0]['content'][1]['image_url']['url'], test_img)
        self.assertEqual(messages2[1]['role'], 'assistant')
        self.assertEqual(messages2[1]['content'], '这是一张图片')
        self.assertEqual(messages2[2]['role'], 'user')
        self.assertEqual(messages2[2]['content'], '图片里有什么颜色？')

    def test_remote_gemma_native_and_heretic_mmproj_spec(self):
        for variant in ('native', 'heretic'):
            spec = remote_gemma.MODELS.get(variant)
            self.assertIsNotNone(spec)
            self.assertTrue(spec.get('multimodal'))
            self.assertEqual(spec.get('mmproj'), 'gemma-4-31B-it-mmproj.gguf')
            file_names = [f[1] for f in spec['files']]
            self.assertIn('gemma-4-31B-it-mmproj.gguf', file_names)
        self.assertFalse(remote_gemma.MODELS['gpt_oss_120b'].get('multimodal'))

    def test_snapshot_includes_multimodal_flag_and_models_list(self):
        # 1. Default closed state
        snap = self.workspace.snapshot()
        self.assertIn('multimodal', snap)
        self.assertTrue(snap['multimodal'])
        self.assertIn('models', snap)
        models_by_id = {m['id']: m for m in snap['models']}
        self.assertTrue(models_by_id['native']['multimodal'])
        self.assertTrue(models_by_id['heretic']['multimodal'])
        self.assertFalse(models_by_id['gpt_oss_120b']['multimodal'])

        # 2. Deployed with gpt_oss_120b
        self.controller.jobs['deploy_snap_120b'] = dict(
            id='deploy_snap_120b',
            config=dict(app_id=APP_ID, operation='open', variant='gpt_oss_120b', gpu='A100', secret='sec123'),
            status='running',
            chat_session='ready',
            remote_result={'ready': True, 'endpoint': 'https://tunnel.example.com', 'secret': 'sec123'},
            created=100.0,
        )
        snap120 = self.workspace.snapshot()
        self.assertEqual(snap120['variant'], 'gpt_oss_120b')
        self.assertFalse(snap120['multimodal'])

    def test_stream_multimodal_unsupported_without_mmproj_friendly_error(self):
        cid = 'b' * 32
        self.controller.jobs['deploy_mm_err'] = dict(
            id='deploy_mm_err',
            config=dict(app_id=APP_ID, operation='open', variant='heretic', gpu='L4', secret='sec123'),
            status='running',
            chat_session='ready',
            remote_result={'ready': True, 'endpoint': 'https://tunnel.example.com', 'secret': 'sec123'},
            created=100.0,
        )

        test_img = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=='
        payload = SendRequest(prompt='看图', images=[test_img])

        async def consume(resp):
            res = []
            async for c in resp.body_iterator:
                res.append(c)
            return res

        with patch('requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.json.return_value = {
                'error': {
                    'code': 500,
                    'message': 'image input is not supported - hint: if this is unexpected, you may need to provide the mmproj',
                    'type': 'server_error'
                }
            }
            mock_post.return_value.__enter__.return_value = mock_resp

            resp = self.workspace.stream(cid, payload)
            chunks = asyncio.run(consume(resp))
            raw_output = ''.join(chunks)
            self.assertIn('当前远端模型服务未加载视觉投影(mmproj)', raw_output)

        snap = self.workspace.snapshot(chat_id=cid)
        self.assertEqual(len(snap['turns']), 1)
        turn = snap['turns'][0]
        self.assertEqual(turn['status'], 'error')
        self.assertIn('当前远端模型服务未加载视觉投影(mmproj)', turn['message'])

    def test_prepare_history_rejects_images_for_gpt_oss_120b(self):
        cid = 'c' * 32
        self.controller.jobs['deploy_120b'] = dict(
            id='deploy_120b',
            config=dict(app_id=APP_ID, operation='open', variant='gpt_oss_120b', gpu='A100', secret='sec123'),
            status='running',
            chat_session='ready',
            remote_result={'ready': True, 'endpoint': 'https://tunnel.example.com', 'secret': 'sec123'},
            created=100.0,
        )
        from fastapi import HTTPException
        payload = SendRequest(prompt='看图', images=['data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=='])
        with self.assertRaises(HTTPException) as cm:
            self.workspace._prepare_history(cid, payload)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn('纯文本', cm.exception.detail)

        # But text-only prompts succeed, and previous multimodal history turns are gracefully converted to text
        self.controller.jobs['prior_multimodal_turn'] = dict(
            id='prior_turn',
            config=dict(app_id=APP_ID, operation='reply', variant='native', chat_id=cid,
                        prompt='之前看这张图', images=['data:image/png;base64,...']),
            status='done',
            remote_result={'text': '之前的回答'},
            created=90.0,
        )
        text_payload = SendRequest(prompt='针对刚才的分析，请继续总结')
        _, messages, _, _, _ = self.workspace._prepare_history(cid, text_payload)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0], {'role': 'user', 'content': '之前看这张图'})
        self.assertEqual(messages[1], {'role': 'assistant', 'content': '之前的回答'})
        self.assertEqual(messages[2], {'role': 'user', 'content': '针对刚才的分析，请继续总结'})


if __name__ == '__main__':
    unittest.main()

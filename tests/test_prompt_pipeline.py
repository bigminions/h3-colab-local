"""Direct prompt regressions. No real cloud or GPU calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import app
import remote_h3 as h3


class DirectPromptTests(unittest.TestCase):
    def test_direct_mode_preserves_original_prompt_without_rewriting(self):
        text = '[0s-4s] 二维墨线。手机显示“妈妈”。[4s-10s] 她说：“图片9很好看。”'
        c = app.H3JobConfig(prompt=text, people=False, prompt_processing='direct').model_dump()
        c.update(job_id='test')
        self.assertEqual(h3.build_graph(c)['5']['inputs']['prompt'], text)
        self.assertFalse(any('generation_tail' in name for _,name,_ in h3.files_for(c)))
        self.assertIn(h3.CLIP,[name for _,name,_ in h3.files_for(c)])

    def test_explicit_picture_mapping_preserves_prose_and_timestamps(self):
        text = '@图片3 作为场景。[0s-4s] 二维墨线，手机显示“妈妈”。[4s-10s] 女人说：“图片9很好看。”'
        c = app.H3JobConfig(prompt=text, references=[dict(slot=3,image_id='a'*32+'.png')]).model_dump()
        actual = h3.build_graph(dict(c,job_id='test'))['5']['inputs']['prompt']
        self.assertTrue(actual.endswith(text.replace('@图片3', '<Picture 1>')))
        self.assertNotIn('[Shot', actual)

    def test_generation_records_exact_direct_prompt(self):
        c = dict(prompt='[0s-4s] 墨线风格，妈妈。',prompt_processing='direct',_compiled_prompt='WRONG',
                 mode='pdd',people=False,resolution='480p',duration=5,seed=1,job_id='audit')
        queue=MagicMock();queue.json.return_value={}
        history=MagicMock();history.json.return_value={'queued':{'status':{'completed':True},'outputs':{'save':{'videos':[{'filename':'clip.mp4'}]}}}}
        submitted=MagicMock(ok=True);submitted.json.return_value={'prompt_id':'queued'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(h3,'ROOT',Path(tmp)),patch('requests.get',side_effect=[queue,history]),patch('requests.post',return_value=submitted) as post,patch.object(h3,'result_for',return_value='saved'),patch.object(h3,'event'):
            self.assertEqual(h3.generate(c),'saved')
            audit=json.loads((Path(tmp)/'output'/'H3'/'local_audit.prompt.json').read_text(encoding='utf-8'))
            self.assertEqual(audit['original_prompt'],c['prompt'])
            self.assertEqual(audit['compiled_prompt'],c['prompt'])
            self.assertEqual(audit['compiled_prompt'],post.call_args.kwargs['json']['prompt']['5']['inputs']['prompt'])
            self.assertEqual(audit['version'],'direct-h3')

    def test_only_the_owned_private_service_can_be_stopped(self):
        import signal
        with tempfile.TemporaryDirectory() as tmp:
            proc=Path(tmp)/'proc';proc.mkdir()
            owned=proc/'123';owned.mkdir()
            (owned/'cmdline').write_bytes('\0'.join(['python',str(h3.ROOT/'main.py'),'--listen','127.0.0.1','--port','8188','']).encode())
            (owned/'stat').write_text('123 (python) Z other fields')
            unrelated=proc/'456';unrelated.mkdir();(unrelated/'cmdline').write_bytes(b'python\0other.py\0')
            factory=lambda p:proc if p=='/proc' else Path(p)
            with patch.object(h3,'Path',side_effect=factory),patch.object(h3.os,'kill') as kill:
                h3.stop_owned_comfy();kill.assert_called_once_with(123,signal.SIGTERM)
            second=proc/'789';second.mkdir();(second/'cmdline').write_bytes((owned/'cmdline').read_bytes())
            with patch.object(h3,'Path',side_effect=factory),patch.object(h3.os,'kill') as kill:
                with self.assertRaises(h3.PreserveRuntimeError):h3.stop_owned_comfy()
                kill.assert_not_called()


    def test_pipeline_files_splits_qwen_and_diffusion_models(self):
        c = app.H3JobConfig(prompt='测试', prompt_processing='faithful').model_dump()
        qwen, other = h3.pipeline_files(c)
        all_files = h3.files_for(c)
        self.assertEqual(len(qwen), 2)
        encoder, tail = h3.encoder_files(c)
        self.assertEqual(qwen, [encoder, tail])
        self.assertEqual(set(qwen + other), set(all_files))
        self.assertEqual(len(qwen) + len(other), len(all_files))
        self.assertNotIn(encoder, other)
        self.assertNotIn(tail, other)

    def test_pipeline_files_direct_mode_returns_all_in_other(self):
        c = app.H3JobConfig(prompt='测试', prompt_processing='direct').model_dump()
        qwen, other = h3.pipeline_files(c)
        self.assertEqual(qwen, [])
        self.assertEqual(other, h3.files_for(c))

    def test_download_cancel_check_aborts_immediately(self):
        from drive_cache_remote import DriveModelCache
        cache = DriveModelCache('test-token', MagicMock())
        cache.cancel_check = lambda: True
        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b'chunk-1', b'chunk-2']
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'model.safetensors'
            with patch.object(cache, 'lookup', return_value={'id': 'f1'}), \
                 patch.object(cache, 'request', return_value=mock_resp):
                mock_resp.__enter__.return_value = mock_resp
                with self.assertRaisesRegex(RuntimeError, '下载已取消'):
                    cache.restore(target, {'size': 100, 'sha256': 'abc'})
                self.assertFalse(target.with_name(target.name + '.drive-partial').exists())

    def test_load_images_falls_back_to_content_directory(self):
        from PIL import Image
        import h3_prompt_worker
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'ComfyUI'
            (root / 'input').mkdir(parents=True)
            content = Path(tmp) / 'content'
            content.mkdir(parents=True)
            img = Image.new('RGB', (100, 100), 'red')
            img_path = content / 'fallback.png'
            img.save(img_path)
            pictures = [{'image_id': 'fallback.png'}]
            mock_torch = MagicMock()
            mock_np = MagicMock()
            def fake_path(p):
                if str(p) == '/content':
                    return content
                return Path(p)
            with patch.dict('sys.modules', {'torch': mock_torch, 'numpy': mock_np}), \
                 patch.object(h3_prompt_worker, 'Path', side_effect=fake_path):
                h3_prompt_worker.load_images(root, pictures)
                mock_torch.from_numpy.assert_called_once()
                mock_torch.cat.assert_called_once()


if __name__ == '__main__': unittest.main()

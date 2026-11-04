"""Model source logs use local fixtures; no cloud requests or GPU allocation."""
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from apps.h3 import remote_h3 as h3


class ModelLoadLogTests(unittest.TestCase):
    def test_sources_and_cache_failures_are_distinguished(self):
        cases = [
            ('disabled', False, False, 'huggingface', 'disabled', False, False),
            ('local_disabled', False, True, 'local', 'disabled', False, False),
            ('local_enabled', True, True, 'local', 'local_reuse', False, False),
            ('standby', True, False, 'huggingface', 'standby', False, False),
            ('hit', True, False, 'drive', 'hit', True, False),
            ('identity_failed', True, False, 'huggingface', 'identity_failed', False, False),
            ('store_failed', True, False, 'huggingface', 'standby', False, True),
        ]
        data = b'x' * (1024 * 1024 + 1)
        for case, enabled, existing, source, status, hf_fail, allow_store in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                target = root / 'models' / 'test' / 'model.safetensors'
                target.parent.mkdir(parents=True)
                if existing:
                    target.write_bytes(data)
                cache = MagicMock() if enabled else None
                if cache is not None:
                    cache.restore.return_value = False
                    if case == 'hit':
                        cache.restore.side_effect = lambda *args: (target.write_bytes(data), True)[1]
                    if case == 'store_failed':
                        cache.store.side_effect = RuntimeError('test store failure')

                def download(**kwargs):
                    if hf_fail:
                        raise RuntimeError('test download failure')
                    target.write_bytes(data)
                    return target

                downloader = MagicMock(side_effect=download)
                loads = []
                with patch.object(h3, 'ROOT', root), patch.object(h3, 'event') as emit, \
                        patch.object(h3.time, 'monotonic', side_effect=itertools.count()), \
                        patch('drive_cache_remote.model_identity', return_value={
                            'revision': 'test', 'size': len(data), 'sha256': 'test'
                        }) as identity:
                    if case == 'identity_failed':
                        identity.side_effect = RuntimeError('test metadata failure')
                    h3.prepare_model('repo', target.name, 'test', downloader, cache, loads, allow_store=allow_store)
                self.assertEqual(len(loads), 1)
                metric = loads[0]
                self.assertEqual(metric['source'], source)
                self.assertEqual(metric['cache_status'], status)
                self.assertEqual(metric['bytes'], len(data))
                self.assertEqual(metric['seconds'], sum(metric[key] for key in
                                 ('acquire_seconds', 'verify_seconds', 'store_seconds')))
                self.assertEqual(metric['store_status'] == 'failed', case == 'store_failed')
                messages = '\n'.join(call.args[1] for call in emit.call_args_list)
                self.assertIn('模型文件就绪', messages)
                if existing:
                    self.assertIn('不计为 Drive 命中', messages)
                if case == 'hit':
                    self.assertIn('Drive 备用缓存命中', messages)
                if case == 'store_failed':
                    self.assertIn('Drive 模型保存失败', messages)

    def test_summary_counts_sources_and_keeps_write_sync_cost_separate(self):
        loads = [dict(source=source, bytes=10**9, store_seconds=seconds)
                 for source, seconds in [('drive', 0), ('huggingface', 5), ('local', 1)]]
        with patch.object(h3, 'event') as emit:
            h3.model_load_summary(loads, 20, 3)
        message = emit.call_args.args[1]
        for label in ('Drive 恢复', 'Hugging Face 获取', '运行时本地复用'):
            self.assertIn(label + ' 1 个 / 1.00 GB', message)
        self.assertIn('缓存保存检查/写入 6.0 秒，同步 3.0 秒', message)
        self.assertIn('不含 Veda、依赖安装和 GPU 权重加载', message)
        self.assertEqual(emit.call_args.kwargs['timings']['model_files_seconds'], 20)


if __name__ == '__main__':
    unittest.main()

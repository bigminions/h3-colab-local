"""Encoder selection and integrity checks; no downloads or cloud allocation."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import remote_h3 as h3


class ModelConfigTests(unittest.TestCase):
    def test_reference_pdd_download_and_cache_use_pinned_verified_weights(self):
        for cached in (False, True):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                target = root/'models'/'loras'/h3.REF_PDD
                data = b'valid-ref-pdd'
                def download(**options):
                    target.write_bytes(data)
                    return str(target)
                downloader = MagicMock(side_effect=download)
                cache = MagicMock() if cached else None
                if cache is not None:
                    cache.restore.return_value = False
                identity = dict(revision=h3.REF_PDD_REVISION,size=len(data),sha256=hashlib.sha256(data).hexdigest())
                with patch.object(h3,'ROOT',root), patch.object(h3,'REF_PDD_SIZE',len(data)), \
                        patch.object(h3,'REF_PDD_SHA256',identity['sha256']), patch.object(h3,'event'), \
                        patch('drive_cache_remote.model_identity',return_value=identity) as resolve:
                    h3.prepare_model('Kijai/MiniMax-H3-experimental','loras/'+h3.REF_PDD,'loras',downloader,cache)
                    self.assertEqual(downloader.call_args.kwargs['revision'],h3.REF_PDD_REVISION)
                    self.assertTrue(target.with_name(target.name+'.verified.json').exists())
                    if cached:
                        cache.store.assert_not_called()
                    target.unlink()
                    target.with_name(target.name+'.verified.json').unlink()
                    h3.prepare_model('Kijai/MiniMax-H3-experimental','loras/'+h3.REF_PDD,'loras',downloader,cache,allow_store=True)
                    if cached:
                        self.assertEqual(resolve.call_args.args[2],h3.REF_PDD_REVISION)
                        cache.store.assert_called_once_with(target,identity)
                    target.write_bytes(b'X'*len(data))
                    with self.assertRaisesRegex(RuntimeError,'SHA-256'), patch.object(h3,'download_model',return_value=str(target)):
                        h3.prepare_model('Kijai/MiniMax-H3-experimental','loras/'+h3.REF_PDD,'loras',downloader)

    def test_reference_pdd_rejects_wrong_cache_source_identity(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(h3,'ROOT',Path(folder)), \
                patch('drive_cache_remote.model_identity',return_value=dict(revision=h3.REF_PDD_REVISION,size=h3.REF_PDD_SIZE,sha256='wrong')):
            downloader, cache = MagicMock(), MagicMock()
            with self.assertRaisesRegex(RuntimeError,'参考图 PDD 源版本'):
                h3.prepare_model('Kijai/MiniMax-H3-experimental','loras/'+h3.REF_PDD,'loras',downloader,cache)
            downloader.assert_not_called()
            cache.restore.assert_not_called()

    def test_default_routes_download_and_load_community_encoder(self):
        base = dict(prompt='A cat walks.', mode='pdd', people=False,
                    resolution='480p', duration=5, seed=1, job_id='test')
        variants = [base, dict(base, image_name='cat.png'),
                    dict(base, references=[dict(slot=1, image_id='cat.png',
                                               role='character', description='cat')])]
        for config in variants:
            with self.subTest(config=config):
                files = h3.files_for(config)
                self.assertIn((h3.CLIP_REPO, h3.CLIP, 'text_encoders'), files)
                self.assertFalse(any('qwen3vl_32b_minimax_h3_' in f[1] for f in files))
                graph = h3.build_graph(config)
                self.assertEqual(graph['2']['inputs']['clip_name'], h3.CLIP)
                self.assertEqual(graph['2']['inputs']['type'], 'minimax')

    def test_verified_file_is_cached_and_changed_file_is_rechecked(self):
        data = b'valid-model'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'encoder.safetensors'
            path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            with patch.object(h3, 'CLIP_SIZE', len(data)), patch.object(h3, 'CLIP_SHA256', digest):
                h3.verify_clip(path)
                with patch.object(h3.hashlib, 'file_digest', side_effect=AssertionError('unnecessary hash')):
                    h3.verify_clip(path)
                previous_time = path.stat().st_mtime_ns
                path.write_bytes(b'wrong-model')
                os.utime(path, ns=(previous_time + 1000000000, previous_time + 1000000000))
                with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                    h3.verify_clip(path)

    def test_wrong_size_is_rejected_before_hashing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'encoder.safetensors'
            path.write_bytes(b'partial')
            with patch.object(h3, 'CLIP_SIZE', 20), self.assertRaisesRegex(RuntimeError, '大小不符'):
                h3.verify_clip(path)
            self.assertFalse(path.with_name(path.name + '.verified.json').exists())

    def test_invalid_validation_record_is_rebuilt(self):
        data = b'valid-model'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'encoder.safetensors'
            path.write_bytes(data)
            path.with_name(path.name + '.verified.json').write_text('invalid json')
            with patch.object(h3, 'CLIP_SIZE', len(data)), patch.object(h3, 'CLIP_SHA256', hashlib.sha256(data).hexdigest()):
                h3.verify_clip(path)


if __name__ == '__main__':
    unittest.main()

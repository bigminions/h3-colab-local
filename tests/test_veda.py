"""Veda defaults, explicit opt-out, pinned weights, and retained-service checks. No GPU."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import app
import remote_h3 as h3


class VedaTests(unittest.TestCase):
    def config(self, **kwargs):
        return dict(app.JobConfig(prompt='A cat walks.', people=False, **kwargs).model_dump(), job_id='test')

    def test_default_enables_veda(self):
        config = self.config()
        self.assertTrue(config['veda'])
        self.assertEqual(h3.build_graph(config)['10']['inputs']['model'], ['20', 0])

    def test_explicit_off_has_no_node_or_predictor_download(self):
        config = self.config(veda=False)
        self.assertFalse(config['veda'])
        self.assertNotIn('20', h3.build_graph(config))
        self.assertEqual(h3.build_graph(config)['10']['inputs']['model'], ['18', 0])
        with patch.object(h3, 'CURRENT_CONFIG', config), patch.object(h3.subprocess, 'run') as run:
            download = MagicMock()
            h3.prepare_veda(download)
            download.assert_not_called()
            run.assert_not_called()

    def test_patch_is_after_all_model_changes_and_used_by_guider_and_scheduler(self):
        config = self.config(veda=True, mode='pdd')
        graph = h3.build_graph(config)
        self.assertEqual(graph['18']['inputs']['model'], ['6', 0])
        self.assertEqual(graph['20']['inputs']['model'], ['18', 0])
        self.assertEqual(graph['10']['inputs']['model'], ['20', 0])
        self.assertEqual(graph['11']['inputs']['model'], ['20', 0])
        self.assertEqual(graph['20']['inputs']['predictor'], h3.VEDA_PREDICTORS['t2va']['filename'])
        self.assertEqual(graph['20']['inputs']['reference_sparsity'], '0%')

    def test_reference_mode_uses_own_predictor_and_preserves_full_reference_attention(self):
        config = self.config(veda=True, references=[dict(slot=1, image_id='a'*32+'.png', role='character')])
        graph = h3.build_graph(config)
        self.assertEqual(graph['20']['inputs']['predictor'], h3.VEDA_PREDICTORS['r2va']['filename'])
        self.assertEqual(graph['11']['inputs']['steps'], 8)
        self.assertEqual(graph['5']['class_type'], 'MiniMaxH3ReferenceToVideo')
        self.assertEqual(graph['6']['inputs']['lora_name'], h3.REF_PDD)
        self.assertEqual(graph['18']['inputs']['model'], ['6', 0])
        self.assertEqual(h3.veda_predictor(dict(config, references=[dict(frame='first')]))['filename'],
                         h3.VEDA_PREDICTORS['t2va']['filename'])

    def test_history_preserves_both_user_choices(self):
        for enabled in (True, False):
            public = app.controller.public_job(dict(id='test', status='done', config=self.config(veda=enabled)))
            self.assertEqual(public['creation']['veda'], enabled)

    def test_missing_predictor_download_is_pinned_and_verified_before_loading(self):
        for reference in (False, True):
            config = self.config(veda=True)
            if reference:
                config['references'] = [dict(frame='reference')]
            predictor = h3.veda_predictor(config)
            with tempfile.TemporaryDirectory() as tmp, patch.object(h3, 'ROOT', Path(tmp)), \
                    patch.object(h3, 'CURRENT_CONFIG', config), patch.object(h3, 'event'), \
                    patch('builtins.open', MagicMock()), patch.object(h3, 'verify_weight') as verify, \
                    patch.object(h3.subprocess, 'run'), \
                    patch.object(h3.subprocess, 'check_output', return_value=h3.VEDA_REVISION):
                folder = Path(tmp)/'custom_nodes'/h3.VEDA_FOLDER
                folder.mkdir(parents=True)
                (Path(tmp)/'.h3-veda-ready').write_text(h3.VEDA_REVISION)
                download = MagicMock()
                h3.prepare_veda(download)
                self.assertEqual(download.call_args.kwargs['revision'], predictor['revision'])
                self.assertEqual(download.call_args.kwargs['filename'], predictor['filename'])
                self.assertEqual(verify.call_args.args[1:3], (predictor['size'], predictor['sha256']))

    def test_reused_service_is_not_restarted_when_node_exists(self):
        with patch('requests.get') as get, patch.object(h3, 'stop_owned_comfy') as stop:
            get.return_value.json.return_value = {'VedaSparseAttention': {}}
            h3.ensure_veda_service()
            stop.assert_not_called()

    def test_busy_service_is_never_restarted_to_load_node(self):
        with patch('requests.get') as get, patch.object(h3, 'stop_owned_comfy') as stop:
            get.return_value.json.side_effect = [{}, {'queue_running': ['job']}]
            with self.assertRaises(h3.PreserveRuntimeError):
                h3.ensure_veda_service()
            stop.assert_not_called()

    def test_idle_service_restart_must_actually_load_node(self):
        with patch('requests.get') as get, patch.object(h3, 'stop_owned_comfy') as stop, \
                patch.object(h3, 'start_comfy') as start, patch.object(h3, 'event'):
            get.return_value.json.side_effect = [{}, {}, {}]
            with self.assertRaisesRegex(RuntimeError, 'Veda'):
                h3.ensure_veda_service()
            stop.assert_called_once()
            start.assert_called_once()

    def test_only_verified_install_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(h3, 'ROOT', Path(tmp)), \
                patch('requests.get'), patch('builtins.open', MagicMock()), \
                patch.object(h3.subprocess, 'Popen') as popen:
            h3.start_comfy()
            self.assertNotIn('--whitelist-custom-nodes', popen.call_args.args[0])
            (Path(tmp)/'.h3-veda-ready').write_text(h3.VEDA_REVISION)
            h3.start_comfy()
            command = popen.call_args.args[0]
            self.assertIn('--disable-all-custom-nodes', command)
            self.assertEqual(command[command.index('--whitelist-custom-nodes') + 1], h3.VEDA_FOLDER)

    def test_completed_run_records_sampler_cache_hits_for_honest_comparisons(self):
        for cached in (False, True):
            config = self.config(veda=True, prompt_processing='direct')
            queue = MagicMock()
            queue.json.return_value = {}
            history = MagicMock()
            history.json.return_value = {'prompt-id': {
                'status': {'completed': True, 'messages': [
                    ['execution_cached', {'nodes': ['12'] if cached else ['2', '3']}]]},
                'outputs': {'16': {'videos': [{'filename': 'test.mp4'}]}}}}
            submitted = MagicMock(ok=True)
            submitted.json.return_value = {'prompt_id': 'prompt-id'}
            with tempfile.TemporaryDirectory() as tmp, patch.object(h3, 'ROOT', Path(tmp)), \
                    patch('requests.get', side_effect=[queue, history]), \
                    patch('requests.post', return_value=submitted), patch.object(h3, 'result_for'), \
                    patch.object(h3, 'event') as event:
                h3.generate(config)
                records = [call.kwargs['acceleration'] for call in event.call_args_list
                           if 'acceleration' in call.kwargs]
                self.assertEqual(records[-1]['sampler_cached'], cached)


if __name__ == '__main__':
    unittest.main()

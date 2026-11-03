"""Original/accelerated asset and graph routing; no network or GPU allocation."""
import itertools
from pathlib import Path
import unittest

import app
import remote_h3 as h3


class PerformanceConfigTests(unittest.TestCase):
    def routes(self):
        return [{}, {'references': [dict(slot=1,image_id='a'*32+'.png',frame='first')]},
                {'references': [dict(slot=1, image_id='a' * 32 + '.png', role='character')]}]

    def test_master_off_overrides_all_acceleration_and_matches_downloads(self):
        for route in self.routes():
            config = app.H3JobConfig(prompt='A woman speaks.', performance=False,
                                   encoder='heretic_int8', mode='pdd', veda=True, people=True,
                                   **route).model_dump()
            self.assertEqual(config['encoder'], 'official_bf16')
            for key in ('quantized_model', 'quantized_vae', 'veda', 'people'):
                self.assertFalse(config[key])
            graph = h3.build_graph(dict(config, job_id='test'))
            files = h3.files_for(config)
            names = [Path(f[1]).name for f in files]
            self.assertEqual(graph['9']['inputs']['sampler_name'], 'res_multistep')
            self.assertEqual(graph['11']['inputs']['steps'], 20)
            self.assertNotIn('20', graph)
            self.assertFalse(any(n['class_type'] == 'LoraLoaderModelOnly' for n in graph.values()))
            for node, field in [('1', 'unet_name'), ('2', 'clip_name'), ('3', 'vae_name'), ('4', 'vae_name')]:
                self.assertIn(graph[node]['inputs'][field], names)
            self.assertFalse(any('pruned' in n or 'int8' in n or 'heretic' in n for n in names))
            self.assertIn('qwen3vl_32b_h3_instruct_generation_tail_50_63_bf16.safetensors', names)

    def test_remote_graph_also_honors_master_off_for_raw_configs(self):
        config = dict(app.H3JobConfig(prompt='x').model_dump(), performance=False, job_id='test')
        graph = h3.build_graph(config)
        self.assertEqual(graph['2']['inputs']['clip_name'], 'qwen3vl_32b_minimax_h3_bf16.safetensors')
        self.assertNotIn('6', graph)
        self.assertNotIn('20', graph)

    def test_independent_choices_have_matching_model_adapter_and_tail(self):
        for route, encoder, quantized, mode in itertools.product(
                self.routes(), ('heretic_int8', 'official_int8', 'official_bf16'), (True, False), ('pdd', 'base')):
            c = app.H3JobConfig(prompt='x', encoder=encoder, quantized_model=quantized,
                              mode=mode, people=False, **route).model_dump()
            graph = h3.build_graph(dict(c, job_id='test'))
            names = {Path(f[1]).name for f in h3.files_for(c)}
            self.assertIn(graph['1']['inputs']['unet_name'], names)
            self.assertIn(graph['2']['inputs']['clip_name'], names)
            enc, tail = h3.encoder_files(c)
            self.assertIn(Path(tail[1]).name, names)
            self.assertEqual('instruct' in tail[1], encoder != 'heretic_int8')
            if mode == 'pdd':
                adapter = graph['6']['inputs']['lora_name']
                self.assertIn(adapter, names)
                if h3.uses_reference_model(c):
                    self.assertEqual('pruned' in adapter, quantized)
            else:
                self.assertNotIn('6', graph)
            direct_names = {f[1] for f in h3.files_for(dict(c, prompt_processing='direct'))}
            self.assertNotIn(tail[1], direct_names)

    def test_history_exposes_master_and_individual_options(self):
        config = app.H3JobConfig(prompt='x', performance=False).model_dump()
        public = app.controller.public_job(dict(id='test', status='done', config=config))
        for key in ('performance', 'encoder', 'quantized_model', 'quantized_vae', 'mode', 'veda'):
            self.assertEqual(public['creation'][key], config[key])


if __name__ == '__main__':
    unittest.main()

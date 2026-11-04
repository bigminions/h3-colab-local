"""Reference validation and transport tests; no cloud allocation or generation."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, UploadFile
from PIL import Image
from pydantic import ValidationError
import app
from asset_library import AssetLibrary
from apps.h3.remote_h3 import build_graph, files_for, reference_prompt, REF_UNET, UNET, PDD, REF_PDD, PEOPLE


def ref(slot, role='character', description=''):
    return dict(slot=slot, image_id=f'{slot:032x}.png', role=role, description=description)


class ReferenceTests(unittest.TestCase):
    def test_sparse_slots_are_mapped_in_prompt_and_graph_without_crop(self):
        c = app.H3JobConfig(prompt='@图片3中的男主在@图片1的花园里行走', references=[ref(3),ref(1,'scene')]).model_dump()
        g = build_graph(dict(c,job_id='test'))
        self.assertEqual(g['1']['inputs']['unet_name'], REF_UNET)
        self.assertEqual(g['5']['class_type'], 'MiniMaxH3ReferenceToVideo')
        self.assertEqual(g['5']['inputs']['ref_images.ref_image_0'], ['30',0])
        self.assertEqual(g['30']['inputs']['image'], ref(1)['image_id'])
        self.assertEqual(g['31']['inputs']['image'], ref(3)['image_id'])
        self.assertIn('<Picture 2>中的男主在<Picture 1>的花园里行走', g['5']['inputs']['prompt'])
        self.assertNotIn('19',g)
        self.assertEqual(g['11']['inputs']['steps'],8)
        self.assertEqual(g['6']['inputs']['lora_name'],REF_PDD); self.assertNotIn('7',g)
        self.assertEqual(c['mode'],'pdd'); self.assertFalse(c['people'])

    def test_missing_slots_in_prompt_or_description_are_rejected(self):
        for prompt, description in [('@图片2走过花园',''),('角色走过花园','保持@图片2的服装'),('<Picture 9> walks','')]:
            with self.subTest(prompt=prompt,description=description), self.assertRaises(ValidationError):
                app.H3JobConfig(prompt=prompt,references=[ref(1,description=description)])

    def test_direct_pipeline_preserves_literal_picture_words_in_dialogue(self):
        text='女人说：“图片9很好看。”'
        c=app.H3JobConfig(prompt=text,references=[ref(1)])
        self.assertEqual(c.prompt,text)
        self.assertNotIn('prompt_enhance',c.model_dump())
        self.assertTrue(reference_prompt(c.model_dump()).endswith(text))

    def test_duplicate_slots_limits_paths_and_mode_mismatch_are_rejected(self):
        cases = [dict(references=[ref(1),ref(1)]),dict(references=[ref(i) for i in range(1,11)]),
                 dict(references=[dict(ref(1),image_id='../secret.png')]),
                 dict(references=[ref(1)],image_id='a'*32+'.jpg'),
                 dict(generation_type='reference'),dict(generation_type='text',references=[ref(1)])]
        for data in cases:
            with self.subTest(data=data),self.assertRaises(ValidationError):
                app.H3JobConfig(prompt='test',**data)

    def test_reference_files_exclude_incompatible_weights(self):
        c = app.H3JobConfig(prompt='test',references=[ref(1)]).model_dump()
        names = [f[1] for f in files_for(c)]
        self.assertIn('diffusion_models/'+REF_UNET,names)
        self.assertFalse(any(name.endswith((UNET,PDD,PEOPLE)) for name in names))
        self.assertIn('loras/'+REF_PDD,names)
        self.assertEqual(len(names),6)

    def test_reference_sampling_choice_selects_matching_adapter_and_recipe(self):
        for mode in ('pdd','base'):
            for veda in (False,True):
                with self.subTest(mode=mode,veda=veda):
                    c = app.H3JobConfig(prompt='图片2作为场景参考',mode=mode,veda=veda,
                                      references=[dict(ref(1),frame='first'),ref(2,'scene'),dict(ref(3),frame='last')]).model_dump()
                    self.assertEqual(c['mode'],mode)
                    g = build_graph(dict(c,job_id='test'))
                    names = [f[1] for f in files_for(c)]
                    self.assertEqual(g['1']['inputs']['unet_name'],REF_UNET)
                    self.assertEqual(g['11']['inputs']['steps'],8 if mode == 'pdd' else 20)
                    self.assertEqual(g['9']['inputs']['sampler_name'],'euler' if mode == 'pdd' else 'res_multistep')
                    self.assertEqual(g['11']['inputs']['scheduler'],'simple')
                    self.assertEqual(g['18']['inputs']['shift_video'],12.0)
                    self.assertEqual(g['18']['inputs']['shift_audio'],3.0)
                    self.assertEqual(g['11']['inputs']['model'],g['10']['inputs']['model'])
                    self.assertEqual(g['10']['inputs']['conditioning'],['52',0])
                    self.assertEqual('loras/'+REF_PDD in names,mode == 'pdd')
                    self.assertNotIn('loras/'+PDD,names)
                    if mode == 'pdd':
                        self.assertEqual(g['6']['inputs'],dict(model=['1',0],lora_name=REF_PDD,strength_model=1.0))
                        self.assertEqual(g['18']['inputs']['model'],['6',0])
                    else:
                        self.assertNotIn('6',g)
                        self.assertEqual(g['18']['inputs']['model'],['1',0])

    def test_nine_inputs_and_high_detail_are_supported(self):
        c = app.H3JobConfig(prompt='@图片9 says hello',references=[ref(i) for i in range(1,10)],reference_size='max').model_dump()
        g = build_graph(dict(c,job_id='test'))
        self.assertEqual(g['5']['inputs']['ref_images.ref_image_8'],['38',0])
        self.assertEqual(g['5']['inputs']['ref_image_size'],'max')
        self.assertIn('<Picture 9> says hello', reference_prompt(c))

    def test_missing_uploaded_file_never_starts_job(self):
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as temp, patch.object(app,'PRIVATE',Path(temp)), patch.object(app,'asset_library',AssetLibrary(Path(temp)/'materials')), patch.object(app.controller,'start') as start:
            (Path(temp)/'google-token.dat').touch()
            with self.assertRaises(HTTPException) as caught:
                app.create_job(app.H3JobConfig(prompt='test',references=[ref(1)]))
            self.assertEqual(caught.exception.status_code,400)
            start.assert_not_called()

    def test_transport_uploads_all_refs_and_copies_after_prepare(self):
        control = MagicMock(session={})
        transport = app.ColabProvider(control, app.runtime_services)
        transport.refresh = lambda: SimpleNamespace(url='https://example.invalid',token='test-only',kernel_id=None,session_id=None)
        c = app.H3JobConfig(prompt='图片3走进图片1',references=[ref(3),ref(1,'scene')]).model_dump()
        job = dict(id='test',config=c)
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as directory, patch.object(app,'asset_library',AssetLibrary(Path(directory)/'materials')), patch.object(app,'TimedContents') as contents, patch.object(app,'ColabRuntime') as runtime, \
             patch.object(app, 'drive_cache', SimpleNamespace(enabled=False)):
            for item in c['references']:
                app.asset_library.store(item['image_id'],b'image',app_id='h3',purpose='references')
            runtime.return_value.execute_code.return_value = [dict(text='H3_EVENT '+json.dumps(dict(stage='result',message='test',result={'path':'test'})))]
            transport.execute(job)
            self.assertEqual(contents.return_value.upload.call_count,2)
            code = runtime.return_value.execute_code.call_args.args[0]
            compile(code,'remote-reference-test','exec')
            self.assertIn('_orig_prepare()\n    import shutil',code)
            for r in c['references']:
                self.assertIn(r['image_id'],code)
            runtime.return_value.stop.assert_called_once()

    def test_valid_image_upload_preview_and_corrupt_file_rejection(self):
        buf = io.BytesIO(); Image.new('RGB',(64,96),'blue').save(buf,format='PNG')
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as temp, patch.object(app,'PRIVATE',Path(temp)), patch.object(app,'asset_library',AssetLibrary(Path(temp)/'materials')):
            result = asyncio.run(app.upload_image(UploadFile(filename='test.png',file=io.BytesIO(buf.getvalue()))))
            response = app.image_preview(result['image_id'])
            self.assertEqual(response.media_type,'image/png')
            with self.assertRaises(HTTPException): app.image_preview('../secret.png')
            with self.assertRaises(HTTPException):
                asyncio.run(app.upload_image(UploadFile(filename='fake.png',file=io.BytesIO(b'\x89PNG\r\n\x1a\nnot-an-image'))))

    def test_history_exposes_reference_settings_for_reuse(self):
        c = app.H3JobConfig(prompt='test',references=[ref(3,'scene','garden')]).model_dump()
        public = app.controller.public_job(dict(id='test',status='done',config=c))
        self.assertEqual(public['creation']['references'],c['references'])
        self.assertEqual(public['creation']['generation_type'],'image')

    def test_first_and_last_only_use_fl2va_and_preserve_fast_mode(self):
        c = app.H3JobConfig(prompt='@图片3走向@图片1', generation_type='image',
                          references=[dict(ref(1),frame='last'),dict(ref(3),frame='first')]).model_dump()
        g = build_graph(dict(c,job_id='test'))
        self.assertEqual(c['mode'],'pdd')
        self.assertEqual(g['1']['inputs']['unet_name'],UNET)
        self.assertEqual(g['5']['inputs']['first_frame'],['40',0])
        self.assertEqual(g['5']['inputs']['last_frame'],['41',0])
        self.assertEqual(g['30']['inputs']['image'],ref(3)['image_id'])
        self.assertIn('<Picture 1>走向<Picture 2>',g['5']['inputs']['prompt'])
        self.assertEqual(g['11']['inputs']['steps'],8)
        self.assertFalse(any(n['class_type']=='MiniMaxH3AddGuide' for n in g.values()))
        self.assertEqual(files_for(c)[0][1],'diffusion_models/'+UNET)

    def test_reference_plus_anchors_chains_guides_into_sampler(self):
        c = app.H3JobConfig(prompt='图片1中的人物走进图片2', generation_type='image',
                          references=[dict(ref(1),frame='first'),ref(2,'scene'),dict(ref(3),frame='last')]).model_dump()
        g = build_graph(dict(c,job_id='test'))
        self.assertEqual(g['1']['inputs']['unet_name'],REF_UNET)
        self.assertEqual(g['50']['inputs']['frame_idx'],0)
        self.assertEqual(g['52']['inputs']['frame_idx'],-1)
        self.assertEqual(g['50']['inputs']['positive'],['5',0])
        self.assertEqual(g['52']['inputs']['positive'],['50',0])
        self.assertEqual(g['10']['inputs']['conditioning'],['52',0])
        self.assertEqual(g['52']['inputs']['latent'],['5',1])
        self.assertEqual(g['12']['inputs']['latent_image'],['5',1])
        self.assertEqual(c['mode'],'pdd')
        self.assertEqual(len(files_for(c)),6)

    def test_tail_only_and_duplicate_anchors(self):
        c = app.H3JobConfig(prompt='ending',references=[dict(ref(2),frame='last')]).model_dump()
        g = build_graph(dict(c,job_id='test'))
        self.assertNotIn('first_frame',g['5']['inputs'])
        self.assertIn('last_frame',g['5']['inputs'])
        for frame in ('first','last','invalid'):
            with self.subTest(frame=frame),self.assertRaises(ValidationError):
                app.H3JobConfig(prompt='test',references=[dict(ref(1),frame=frame),dict(ref(2),frame=frame)])

    def test_anchor_selection_is_preserved_in_history(self):
        c = app.H3JobConfig(prompt='test',generation_type='image',references=[dict(ref(3),frame='last')]).model_dump()
        public = app.controller.public_job(dict(id='test',status='done',config=c))
        self.assertEqual(public['creation']['references'][0]['frame'],'last')
        self.assertEqual(public['creation']['generation_type'],'image')


if __name__ == '__main__':
    unittest.main()

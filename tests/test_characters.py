"""Character library, mention expansion and audio transport checks; no cloud jobs."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import wave

from fastapi import HTTPException, UploadFile
from pydantic import ValidationError
import app
from asset_library import AssetLibrary
from apps.h3.characters import AudioAsset, CharacterLibrary, CharacterProfile, expand_characters, inspect_audio, mention_names
from apps.h3.remote_h3 import build_graph, files_for, reference_prompt, REF_UNET


def wav_bytes(seconds=2):
    buf = io.BytesIO()
    with wave.open(buf,'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(32000)
        output.writeframes(b'\0\0' * int(seconds * 32000))
    return buf.getvalue()


class CharacterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.directory = Path(self.temp.name)
        self.assets = AssetLibrary(self.directory/'materials')
        self.library = CharacterLibrary(self.directory, self.assets)
        self.images = [f'{i:032x}.png' for i in (1,2,3)]
        for name in self.images:
            self.assets.store(name, b'image-test')
        self.patches = [patch.object(app,'PRIVATE',self.directory),patch.object(app,'asset_library',self.assets),patch.object(app,'character_library',self.library)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def audio(self,seconds=2):
        return asyncio.run(app.upload_audio(UploadFile(filename='voice.wav',file=io.BytesIO(wav_bytes(seconds)))))

    def profile(self,name='男主',images=None,audios=None):
        return self.library.save(CharacterProfile(name=name,image_ids=images or [self.images[0]],
                   audios=audios or [],description='红衣少年',voice='低沉年轻男声'))

    def test_library_is_persistent_editable_and_rejects_duplicates_or_missing_assets(self):
        p = self.profile()
        self.assertEqual(CharacterLibrary(self.directory,self.assets).list()[0].name,'男主')
        p.description = 'new description'; self.library.save(p)
        self.assertEqual(len(self.library.list()),1)
        self.assertEqual(self.library.list()[0].description,'new description')
        with self.assertRaisesRegex(ValueError,'名称已存在'): self.profile()
        with self.assertRaises(ValueError): self.profile('坏图',['f'*32+'.png'])
        with self.assertRaises(ValidationError): CharacterProfile(name='bad name',image_ids=self.images)

    def test_delete_restore_lookup_and_snapshot_preserve_assets(self):
        p=self.profile()
        c=app.H3JobConfig(prompt='@男主 看镜头').model_dump()
        deleted=self.library.delete(p.id)
        self.assertTrue(deleted.deleted)
        self.assertFalse(self.library.list())
        self.assertTrue(self.library.get(p.id,include_deleted=True).deleted)
        self.assertTrue(self.assets.resolve(self.images[0]).is_file())
        with self.assertRaises(KeyError): self.library.get(p.id)
        with self.assertRaises(KeyError): self.library.delete(p.id)
        with self.assertRaises(ValidationError): app.H3JobConfig(prompt='@男主 看镜头')
        self.assertEqual(app.H3JobConfig(**c).model_dump(),c)
        restored=self.library.restore(p.id)
        self.assertFalse(restored.deleted)
        self.assertEqual(self.library.get(p.id).name,'男主')

    def test_restore_rejects_name_conflict_and_unknown_ids_return_404(self):
        p=self.profile(); self.library.delete(p.id); self.profile()
        with self.assertRaises(ValueError): self.library.restore(p.id)
        self.assertTrue(self.library.get(p.id,include_deleted=True).deleted)
        for action in (app.get_character,app.delete_character,app.restore_character):
            with self.assertRaises(HTTPException) as caught: action('f'*32)
            self.assertEqual(caught.exception.status_code,404)

    def test_character_management_routes_use_recoverable_deletion(self):
        p=self.profile()
        self.assertEqual(app.get_character(p.id)['name'],'男主')
        self.assertTrue(app.delete_character(p.id)['ok'])
        self.assertEqual(app.list_characters()['characters'],[])
        self.assertEqual(len(app.list_characters(include_deleted=True)['characters']),1)
        self.assertFalse(app.restore_character(p.id)['deleted'])

    def test_mentions_preserve_order_deduplicate_and_do_not_capture_email(self):
        self.assertEqual(mention_names('让@男主 走过来，@女主 说话，@男主 回答。a@b.com'),['男主','女主'])

    def test_picture_mentions_mix_with_characters_without_becoming_profiles(self):
        self.profile()
        text = '@图片3作为场景参考，@男主 在其中行走'
        self.assertEqual(mention_names(text), ['男主'])
        c = app.H3JobConfig(prompt=text, references=[dict(slot=3,image_id=self.images[1])]).model_dump()
        self.assertEqual([p['name'] for p in c['characters']], ['男主'])
        self.assertEqual(c['references'][1]['role'], 'other')
        self.assertIn('<Picture 2>作为场景参考', reference_prompt(c))
        self.assertIn('<Subject 1> 在其中行走', reference_prompt(c))

    def test_missing_explicit_picture_is_rejected_before_character_expansion(self):
        self.profile()
        for mode in ('direct', 'faithful'):
            for text in ('@图片1 作为场景参考', '@图片1 作为场景参考，@男主 行走'):
                with self.subTest(mode=mode,text=text), self.assertRaisesRegex(ValidationError,'未上传的图片'):
                    app.H3JobConfig(prompt=text,prompt_processing=mode)

    def test_image_only_request_keeps_freeform_purpose_in_both_prompt_paths(self):
        text = '@图片3 @图片7 作为场景参考，生成雨夜街道'
        for mode in ('direct', 'faithful'):
            c = app.H3JobConfig(prompt=text,prompt_processing=mode,references=[
                dict(slot=3,image_id=self.images[0]),dict(slot=7,image_id=self.images[1])]).model_dump()
            self.assertEqual(c['characters'], [])
            self.assertTrue(all(r['role'] == 'other' for r in c['references']))
            self.assertIn('<Picture 1> <Picture 2> 作为场景参考', reference_prompt(c))
            self.assertEqual(c['prompt'], text)
            self.assertEqual(c['prompt_processing'], mode)

    def test_selected_roles_expand_and_all_voice_samples_bind_to_one_subject(self):
        audio1, audio2 = self.audio(),self.audio(3)
        p = self.profile(images=self.images[:2],audios=[audio1,audio2])
        unused = self.profile('路人',[self.images[2]])
        c = app.H3JobConfig(prompt='让@男主 说：“我回来了。”',generation_type='text').model_dump()
        self.assertEqual(c['generation_type'],'image'); self.assertEqual(c['mode'],'pdd')
        self.assertEqual([a['character_id'] for a in c['audio_references']],[p.id,p.id])
        self.assertEqual(len(c['references']),2)
        self.assertEqual(c['characters'][0]['id'],p.id)
        self.assertNotIn(unused.id,json.dumps(c))
        prompt = reference_prompt(c)
        self.assertIn('让<Subject 1> 说',prompt)
        self.assertIn('<Audio 1> is a voice-timbre reference for <Subject 1>',prompt)
        self.assertIn('<Audio 2> is a voice-timbre reference for <Subject 1>',prompt)
        g = build_graph(dict(c,job_id='test',_input_audio_indices=[]))
        self.assertEqual(g['1']['inputs']['unet_name'],REF_UNET)
        self.assertEqual(g['70']['inputs']['audio'],audio1['audio_id'])
        self.assertEqual(g['5']['inputs']['ref_audios.ref_audio_1'],['71',0])
        self.assertEqual(len(files_for(c)),6)

    def test_character_can_be_silent_and_mix_with_first_last_frames(self):
        self.profile()
        c = app.H3JobConfig(prompt='@男主 默默看向远处',references=[dict(slot=2,image_id=self.images[1],frame='first'),
                           dict(slot=3,image_id=self.images[2],frame='last')]).model_dump()
        self.assertFalse(c['audio_references'])
        g = build_graph(dict(c,job_id='test'))
        self.assertEqual(g['51']['inputs']['frame_idx'],0)
        self.assertEqual(g['52']['inputs']['frame_idx'],-1)
        self.assertEqual(g['10']['inputs']['conditioning'],['52',0])

    def test_repeated_mentions_and_shared_image_are_uploaded_once(self):
        self.profile()
        c = app.H3JobConfig(prompt='@男主 看镜头。@男主 转身。',references=[dict(slot=5,image_id=self.images[0],frame='first')]).model_dump()
        self.assertEqual(len(c['references']),1)
        self.assertEqual(len(c['characters']),1)
        self.assertEqual(build_graph(dict(c,job_id='test'))['1']['inputs']['unet_name'],REF_UNET)

    def test_unknown_name_and_global_limits_rejected_before_allocation(self):
        with self.assertRaises(ValidationError): app.H3JobConfig(prompt='@不存在 说话')
        p = self.profile(audios=[self.audio(),self.audio()])
        self.profile('女主',[self.images[1]],audios=[self.audio(),self.audio()])
        with self.assertRaisesRegex(ValidationError,'最多 3 段'): app.H3JobConfig(prompt='@男主 对@女主 说话')
        refs = [dict(slot=i,image_id=f'{i+50:032x}.png') for i in range(1,10)]
        with self.assertRaisesRegex(ValidationError,'最多 9 张'): app.H3JobConfig(prompt='@男主 看镜头',references=refs)
        p.audios=[AudioAsset(**self.audio(8)),AudioAsset(**self.audio(8))]
        with self.assertRaises(ValueError): self.library.save(p)

    def test_job_snapshot_survives_role_library_changes(self):
        p = self.profile()
        c = app.H3JobConfig(prompt='@男主 看镜头').model_dump()
        p.name='改名'; p.image_ids=[self.images[1]]; self.library.save(p)
        retry = app.H3JobConfig(**c).model_dump()
        self.assertEqual(retry,c)
        self.assertEqual(retry['characters'][0]['name'],'男主')

    def test_global_voice_duration_limit_and_real_metadata(self):
        a=self.audio(8)
        p=self.profile(audios=[dict(a,duration=2)])
        self.assertEqual(p.audios[0].duration,8)
        self.profile('女主',[self.images[1]],audios=[self.audio(8)])
        with self.assertRaisesRegex(ValidationError,'15 秒'): app.H3JobConfig(prompt='@男主 与@女主 交谈')
        app.asset_library.resolve(a['audio_id']).write_bytes(b'changed')
        with self.assertRaises(ValueError): self.library.save(p)

    def test_audio_duration_is_decoded_and_bad_audio_is_rejected(self):
        self.assertEqual(inspect_audio(wav_bytes(2)),2)
        self.assertEqual(inspect_audio(wav_bytes(15)),15)
        for data in [b'bad',wav_bytes(1),wav_bytes(16)]:
            with self.assertRaises(ValueError): inspect_audio(data)
        with self.assertRaises(HTTPException):
            asyncio.run(app.upload_audio(UploadFile(filename='secret.exe',file=io.BytesIO(wav_bytes()))))
        a=self.audio(); self.assertEqual(app.audio_preview(a['audio_id']).media_type,'audio/wav')
        with self.assertRaises(HTTPException): app.audio_preview('../secret.wav')

    def test_lost_audio_blocks_job_without_starting_transport(self):
        a=self.audio(); self.profile(audios=[a])
        c=app.H3JobConfig(prompt='@男主 说话')
        (self.directory/'google-token.dat').touch(); app.asset_library.resolve(a['audio_id']).unlink()
        with patch.object(app.controller,'start') as start, self.assertRaises(HTTPException): app.create_job(c)
        start.assert_not_called()

    def test_transport_uploads_audio_and_images_and_history_preserves_roles(self):
        a=self.audio(); self.profile(audios=[a])
        c=app.H3JobConfig(prompt='@男主 看镜头').model_dump()
        control=MagicMock(session={}); transport=app.ColabProvider(control, app.runtime_services)
        transport.refresh=lambda:SimpleNamespace(url='https://example.invalid',token='test-only',kernel_id=None,session_id=None)
        with patch.object(app,'TimedContents') as contents,patch.object(app,'ColabRuntime') as runtime:
            runtime.return_value.execute_code.return_value=[dict(text='H3_EVENT '+json.dumps(dict(stage='result',message='test',result={'path':'test'})))]
            transport.execute(dict(id='test',config=c))
            self.assertEqual(contents.return_value.upload.call_count,2)
            code=runtime.return_value.execute_code.call_args.args[0]
            compile(code,'remote-character-test','exec'); self.assertIn(a['audio_id'],code)
        public=app.controller.public_job(dict(id='test',status='done',config=c))
        self.assertEqual(public['creation']['characters'][0]['audios'][0]['audio_id'],a['audio_id'])


if __name__=='__main__': unittest.main()

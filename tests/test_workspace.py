"""Workspace adapters and shared media, using temporary data and synthetic files."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import wave

from fastapi import HTTPException, UploadFile
from PIL import Image
from pydantic import ValidationError
from starlette.requests import Request

import app
from asset_library import AssetLibrary
from apps.h3.characters import AudioAsset, CharacterLibrary, CharacterProfile, validate_audio_asset
from workspace_runtime import ApplicationRegistry, ProviderRegistry, ProviderSpec
from apps.h3.application import H3Application


class MediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = AssetLibrary(self.root / 'materials')

    def test_shared_storage_preserves_character_references_and_voice_metadata(self):
        image_id, audio_id = 'a'*32+'.png', 'b'*32+'.wav'
        self.library.store(image_id, b'image', app_id='h3', purpose='characters')
        self.library.store(audio_id, b'voice', metadata={'duration':2}, app_id='h3', purpose='characters')
        private = self.root / '.private'
        private.mkdir()
        characters = CharacterLibrary(private, self.library)
        profile = characters.save(CharacterProfile(name='角色',image_ids=[image_id],audios=[AudioAsset(audio_id=audio_id,duration=2)]))
        self.assertEqual(characters.get(profile.id).image_ids,[image_id])
        self.assertEqual(characters.save(profile).audios[0].duration,2)
        self.assertEqual(self.library.resolve(image_id).parent.name,'images')
        self.assertEqual(self.library.resolve(audio_id).parent.name,'audios')
        self.assertEqual(self.library.resolve(audio_id).relative_to(self.library.root).parts[:2],('h3','characters'))

    def test_catalog_reads_only_shared_media_folders(self):
        asset_id='a'*32+'.png'
        (self.root/asset_id).write_bytes(b'unrelated image')
        (self.root/'google-token.dat').write_bytes(b'secret')
        with self.assertRaises(FileNotFoundError): self.library.resolve(asset_id)
        self.assertEqual(self.library.list(),[])
        self.library.store(asset_id,b'shared image')
        self.assertEqual([asset['id'] for asset in self.library.list('images')],[asset_id])
        self.assertEqual(self.library.list('videos'),[])
        for value in ('../secret.png','a'*32+'.exe','/google-token.dat'):
            with self.assertRaises(ValueError): self.library.resolve(value)

    def test_video_library_merges_complete_history_and_cloud_only_results(self):
        local, remote = 'a'*32, 'b'*32
        self.library.store(local+'.mp4',b'video',app_id='h3',purpose='outputs')
        jobs=[{'id':local,'file':local+'.mp4','created':1,'config':{'app_id':'h3','title':'本机作品'}},
              {'id':remote,'drive':{'size':100},'created':2,'config':{'app_id':'h3','title':'云端作品'}}]
        assets=self.library.list('videos',jobs)
        self.assertEqual([asset['name'] for asset in assets],['云端作品','本机作品'])
        self.assertTrue(assets[0]['recoverable'])
        self.assertFalse(assets[0]['local'])
        self.assertTrue(assets[1]['local'])

    def test_video_library_handles_legacy_jobs_without_app_id(self):
        remote = 'c'*32
        jobs = [{'id': remote, 'drive': {'size': 200}, 'created': 3, 'config': {'title': '历史作品'}}]
        assets = self.library.list('videos', jobs)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]['app_id'], 'h3')
        self.assertEqual(assets[0]['name'], '历史作品')
        self.assertTrue(assets[0]['recoverable'])


class WorkspaceAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        patcher=patch.object(app,'PRIVATE',self.root)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher=patch.object(app,'asset_library',AssetLibrary(self.root/'materials'))
        patcher.start(); self.addCleanup(patcher.stop)
        self.control=app.Controller(self.root, self.root/'materials'/'h3'/'outputs'/'videos')
        patcher=patch.object(app,'controller',self.control)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_image_uploaded_once_is_available_to_preview_library_and_h3(self):
        buffer=io.BytesIO(); Image.new('RGB',(64,96),'navy').save(buffer,format='PNG')
        result=asyncio.run(app.import_asset(UploadFile(filename='场景.png',file=io.BytesIO(buffer.getvalue()))))
        asset_id=result['id']
        self.assertEqual(app.asset_library.resolve(asset_id).parent.name,'images')
        self.assertEqual(app.image_preview(asset_id).media_type,'image/png')
        self.assertEqual(app.list_assets('images')['assets'][0]['name'],'场景.png')
        (self.root/'google-token.dat').touch()
        config=app.H3JobConfig(prompt='test',references=[{'slot':1,'image_id':asset_id}])
        with patch.object(app.controller,'start',return_value='test'), patch.object(app.controller,'jobs',{'test':{}}):
            self.assertEqual(app.create_job(config)['id'],'test')
        self.assertFalse((self.root/asset_id).exists())

    def test_generic_audio_library_does_not_apply_h3_voice_duration_limits(self):
        buffer=io.BytesIO()
        with wave.open(buffer,'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
            output.writeframes(b'\0\0'*16000)
        result=asyncio.run(app.import_asset(UploadFile(filename='short.wav',file=io.BytesIO(buffer.getvalue()))))
        self.assertEqual(result['kind'],'audios')
        self.assertEqual(app.audio_preview(result['id']).media_type,'audio/wav')
        self.assertEqual(len(app.list_assets('audios')['assets']),1)
        with self.assertRaises(ValueError):
            validate_audio_asset(app.asset_library,AudioAsset(audio_id=result['id'],duration=2))

    def test_invalid_video_leaves_no_partial_or_catalog_entry(self):
        with self.assertRaises(HTTPException):
            asyncio.run(app.import_asset(UploadFile(filename='bad.mp4',file=io.BytesIO(b'bad'))))
        self.assertEqual(app.list_assets('videos')['assets'],[])
        self.assertEqual(list(app.asset_library.folder('videos','shared','imports').glob('*')),[])

    def test_imported_video_is_saved_in_the_shared_folder_and_served_with_ranges(self):
        import av
        buffer=io.BytesIO()
        with av.open(buffer,'w',format='mp4') as container:
            stream=container.add_stream('mpeg4',rate=24)
            stream.width=64; stream.height=64; stream.pix_fmt='yuv420p'
            frame=av.VideoFrame.from_image(Image.new('RGB',(64,64),'navy'))
            for packet in stream.encode(frame): container.mux(packet)
            for packet in stream.encode(): container.mux(packet)
        result=asyncio.run(app.import_asset(UploadFile(filename='clip.mp4',file=io.BytesIO(buffer.getvalue()))))
        self.assertEqual(result['kind'],'videos')
        request=Request({'type':'http','headers':[(b'range',b'bytes=0-20')]})
        with patch.object(app,'VIDEOS',app.asset_library.folder('videos','shared','imports')):
            response=app.asset_file(result['id'],request)
        self.assertEqual(response.media_type,'video/mp4')
        self.assertEqual(Path(response.path).parent.name,'videos')
        self.assertEqual(response.headers['accept-ranges'],'bytes')

    def test_h3_uploads_and_scoped_imports_have_separate_purposes(self):
        buffer=io.BytesIO(); Image.new('RGB',(64,96),'navy').save(buffer,format='PNG')
        data=buffer.getvalue()
        reference=asyncio.run(app.upload_image(UploadFile(filename='reference.png',file=io.BytesIO(data))))
        character=asyncio.run(app.upload_image(UploadFile(filename='character.png',file=io.BytesIO(data)),purpose='characters'))
        imported=asyncio.run(app.import_asset(UploadFile(filename='shared.png',file=io.BytesIO(data))))
        self.assertEqual(app.asset_library.resolve(reference['image_id']).relative_to(app.asset_library.root).parts[:3],('h3','references','images'))
        self.assertEqual(app.asset_library.resolve(character['image_id']).relative_to(app.asset_library.root).parts[:3],('h3','characters','images'))
        self.assertEqual(app.asset_library.resolve(imported['id']).relative_to(app.asset_library.root).parts[:3],('shared','imports','images'))
        filtered=app.list_assets(app_id='h3',purpose='characters')['assets']
        self.assertEqual([item['id'] for item in filtered],[character['image_id']])
        self.assertEqual(filtered[0]['folder'],'h3/characters/images')

    def test_import_rejects_unsupported_application_purpose_and_kind(self):
        for application,purpose in [('unknown','imports'),('h3','outputs'),('h3','../outside')]:
            buffer=io.BytesIO(); Image.new('RGB',(64,96),'navy').save(buffer,format='PNG')
            with self.subTest(application=application,purpose=purpose),self.assertRaises(HTTPException):
                asyncio.run(app.import_asset(UploadFile(filename='image.png',file=io.BytesIO(buffer.getvalue())),app_id=application,purpose=purpose))
        self.assertEqual(app.asset_library.list(),[])

    def test_unknown_provider_and_application_rejected_before_resource_acquisition(self):
        for options in ({'provider_id':'runpod'},{'provider_id':'modal'},{'provider_id':'unknown'},{'app_id':'unknown'}):
            with self.subTest(options=options),self.assertRaises(ValidationError):
                app.H3JobConfig(prompt='test',**options)

    def test_catalog_and_compute_status_do_not_request_cloud_resources_or_expose_credentials(self):
        with patch.object(app.controller,'transport') as transport:
            data=app.workspace()
            transport.connect.assert_not_called(); transport.account.assert_not_called()
        self.assertEqual(data['applications'][0]['id'],'h3')
        self.assertEqual([tab['id'] for tab in data['applications'][0]['tabs']],['tool','characters'])
        self.assertEqual([p['id'] for p in data['providers'] if p['available']],['colab'])
        self.assertNotIn('token',data['compute'])
        self.assertNotIn('endpoint',data['compute'])


class AdapterTests(unittest.TestCase):
    def test_colab_uses_the_application_selected_in_the_job(self):
        # A synthetic application proves the transport no longer selects H3 source.
        application=MagicMock(event_prefix='CUSTOM_EVENT ')
        application.source.return_value='import json'
        application.input_assets.return_value=[]
        application.invocation.return_value='application invocation'
        controller=MagicMock(session={})
        transport=app.ColabProvider(controller, app.runtime_services)
        transport.refresh=lambda: type('Session',(),dict(url='https://example.invalid',token='test',kernel_id=None,session_id=None))()
        job={'id':'test','config':{'app_id':'synthetic'}}
        with patch.object(app.applications,'get',return_value=application),patch.object(app,'ColabRuntime') as runtime,patch.object(app,'drive_cache',enabled=False):
            runtime.return_value.execute_code.return_value=[{'text':'CUSTOM_EVENT '+json.dumps({'stage':'result','message':'done','result':{'path':'test'}})}]
            transport.execute(job)
        runtime.return_value.execute_code.assert_called_once()
        self.assertEqual(runtime.return_value.execute_code.call_args.args[0],'application invocation')
        self.assertEqual(job['remote_result']['path'],'test')

    def test_provider_factory_is_deferred_and_unsupported_choices_are_explicit(self):
        registry=ProviderRegistry([ProviderSpec('test','Test',True,'test',('GPU',))])
        factory=MagicMock()
        registry.register('test',factory)
        registry.list(); factory.assert_not_called()
        registry.create('test','controller')
        factory.assert_called_once_with('controller')

    def test_h3_application_packaged_in_apps_directory(self):

        h3_app = H3Application()
        source = h3_app.source(app.BASE, {'prompt_processing': 'faithful'})
        self.assertIn('H3_IDLE_CHECK', h3_app.idle_probe)
        self.assertIn('h3-prompt-tools', source)
        self.assertIn('ComfyUI', source)

    def test_multiple_applications_coexist_independently(self):
        from pydantic import BaseModel, Field
        from typing import Literal

        class ImageJobConfig(BaseModel):
            app_id: Literal['image'] = 'image'
            provider_id: Literal['colab'] = 'colab'
            prompt: str = Field(min_length=1)
            gpu: Literal['A100', 'G4'] = 'A100'

        from workspace_runtime import ApplicationSpec
        class MockImageApp:
            config_model = ImageJobConfig
            spec = ApplicationSpec(
                id='image', name='生图应用', description='文生图',
                output_kind='image', providers=('colab',),
                tabs=({'id': 'tool', 'name': '创作'},),
                asset_purposes=({'id': 'outputs', 'name': '生成图片', 'kinds': ('images',)},)
            )
            event_prefix = 'IMAGE_EVENT '
            output_extension = '.png'
            metadata_fields = ('width', 'height')
            def source(self, base, config): return 'print("image source")'
            def input_assets(self, config): return []
            def invocation(self, source, job): return source + '\nrun()'
            def validate_assets(self, config, asset_library): pass

        registry = ApplicationRegistry([H3Application(), MockImageApp()])
        self.assertEqual(len(registry.list()), 2)
        self.assertEqual([a['id'] for a in registry.list()], ['h3', 'image'])
        self.assertEqual(registry.get('h3').spec.output_kind, 'video')
        self.assertEqual(registry.get('image').spec.output_kind, 'image')
        # Submitting an image job via controller validates against MockImageApp
        image_cfg = ImageJobConfig(prompt='a blue bird')
        providers = ProviderRegistry([ProviderSpec('colab', 'Colab', True, 'test', ('A100', 'G4'))])
        providers.register('colab', lambda c: MagicMock())
        providers.validate(registry.get('image'), image_cfg.provider_id, image_cfg.gpu)



class UiOptimizationTests(unittest.TestCase):
    def test_generic_asset_picker_dialog_present(self):
        studio_html = Path("studio.html").read_text(encoding="utf-8")
        self.assertIn('id="assetPickerDialog"', studio_html)
        self.assertIn('class="asset-picker-dialog"', studio_html)
        self.assertIn('id="assetPickerGrid"', studio_html)
        self.assertIn('id="confirmAssetPicker"', studio_html)

        ws_js = Path("static/workspace.js").read_text(encoding="utf-8")
        self.assertIn("openAssetPicker", ws_js)

    def test_top_tabs_cancelled_in_studio(self):
        studio_html = Path("studio.html").read_text(encoding="utf-8")
        self.assertIn('id="applicationHeading"', studio_html)
        self.assertIn('style="display:none;"', studio_html)

    def test_h3_heading_switches_and_character_manager_modal(self):
        h3_html = Path("apps/h3/web/content.html").read_text(encoding="utf-8")
        self.assertIn('creation-heading-left', h3_html)
        self.assertIn('id="textTab"', h3_html)
        self.assertIn('id="imageTab"', h3_html)
        self.assertIn('id="manageCharacters"', h3_html)

        # Character management is a dialog modal
        self.assertIn('<dialog id="characterPage"', h3_html)
        self.assertIn('id="closeCharacterModal"', h3_html)

    def test_merged_add_image_and_library_pick(self):
        # H3
        h3_html = Path("apps/h3/web/content.html").read_text(encoding="utf-8")
        self.assertIn('id="referenceDropZone"', h3_html)
        self.assertIn('id="btnUploadLocal"', h3_html)
        self.assertIn('id="chooseLibraryImage"', h3_html)

        # Qwen-Image
        qi_html = Path("apps/qwen_image/web/content.html").read_text(encoding="utf-8")
        self.assertIn('id="qiReferenceDropZone"', qi_html)
        self.assertIn('id="qiBtnUploadLocal"', qi_html)
        self.assertIn('id="qiChooseLibraryImage"', qi_html)

    def test_caret_following_mention_and_clean_ui(self):
        chars_js = Path("apps/h3/web/characters.js").read_text(encoding="utf-8")
        self.assertIn("getCaretCoordinates", chars_js)
        self.assertIn("positionMentionMenu", chars_js)
        self.assertIn("mention-thumb-box", chars_js)
        self.assertIn("mention-name", chars_js)

    def test_character_manager_card_simplified_and_robust_layout(self):
        chars_js = Path("apps/h3/web/characters.js").read_text(encoding="utf-8")
        self.assertIn("card.onclick = () => {", chars_js)
        self.assertIn("openEditor(profile, !!profile.deleted)", chars_js)
        self.assertNotIn("viewBtn.textContent = '查看'", chars_js)
        self.assertNotIn("editBtn.textContent = '编辑'", chars_js)
        rm_body = chars_js.split("function renderManager")[1].split("async function load")[0]
        self.assertNotIn("张参考图", rm_body)
        self.assertIn("delBtn.onclick = async (event) => {", chars_js)
        self.assertIn("event.stopPropagation()", chars_js)
        self.assertIn("body.append(title, delBtn)", chars_js)

        css = Path("apps/h3/web/workbench.css").read_text(encoding="utf-8")
        self.assertIn("aspect-ratio: 4 / 5 !important", css)
        self.assertIn("cursor: pointer !important", css)

    def test_h3_gpu_card_styling(self):
        css = Path("apps/h3/web/workbench.css").read_text(encoding="utf-8")
        self.assertIn("#toolPage .h3-gpu-card-wrap", css)
        self.assertIn("#toolPage .h3-gpu-card", css)
        self.assertIn("#toolPage .h3-gpu-select", css)
        self.assertIn("#toolPage .h3-gpu-badge", css)
        self.assertIn("#toolPage .h3-gpu-name", css)
        self.assertIn("#toolPage .h3-gpu-status", css)
        self.assertIn("#toolPage #generate", css)

    def test_shared_gpu_card_component_unified_across_apps(self):
        ws_css = Path("static/workspace.css").read_text(encoding="utf-8")
        self.assertIn(".gpu-card-wrap", ws_css)
        self.assertIn(".gpu-card", ws_css)
        self.assertIn(".gpu-dropdown-menu", ws_css)
        self.assertIn(".gpu-menu-item", ws_css)
        self.assertIn("color-scheme: dark !important", ws_css)

        ws_js = Path("static/workspace.js").read_text(encoding="utf-8")
        self.assertIn("renderGpuMenu", ws_js)
        self.assertIn("toggleGpuMenu", ws_js)
        self.assertIn("h3GpuMenu", ws_js)
        self.assertIn("qiGpuMenu", ws_js)

        h3_html = Path("apps/h3/web/content.html").read_text(encoding="utf-8")
        self.assertIn('id="h3GpuMenu"', h3_html)
        self.assertIn('class="gpu-card h3-gpu-card"', h3_html)

        qi_html = Path("apps/qwen_image/web/content.html").read_text(encoding="utf-8")
        self.assertIn('id="qiGpuMenu"', qi_html)
        self.assertIn('class="gpu-card qi-gpu-card"', qi_html)

    def test_app_and_page_persistence_across_refresh(self):
        studio_html = Path("studio.html").read_text(encoding="utf-8")
        self.assertIn("lstudio-active-page", studio_html)
        self.assertIn("dataset.initialPage", studio_html)
        self.assertIn('id="computeCurrentAppHint"', studio_html)
        self.assertIn('id="computeCurrentAppName"', studio_html)

        ws_js = Path("static/workspace.js").read_text(encoding="utf-8")
        self.assertIn("lstudio-active-app", ws_js)
        self.assertIn("lstudio-active-page", ws_js)
        self.assertIn("updateComputeAppInfo", ws_js)
        self.assertIn("activeApp", ws_js)
        self.assertIn("useImageForQi", ws_js)
        self.assertIn("lstudio-library-kind", ws_js)
        self.assertIn("isGenerated", ws_js)

        qi_js = Path("apps/qwen_image/web/qwen-image.js").read_text(encoding="utf-8")
        self.assertIn("useImage", qi_js)

        chars_js = Path("apps/h3/web/characters.js").read_text(encoding="utf-8")
        self.assertNotIn("showPage('tool')", chars_js)


class AssetManagementAndDeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(app, 'PRIVATE', self.root)
        patcher.start(); self.addCleanup(patcher.stop)
        self.library = AssetLibrary(self.root / 'materials')
        patcher = patch.object(app, 'asset_library', self.library)
        patcher.start(); self.addCleanup(patcher.stop)
        self.control = app.Controller(self.root, self.root / 'materials' / 'h3' / 'outputs' / 'videos')
        patcher = patch.object(app, 'controller', self.control)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(app, 'character_library', CharacterLibrary(self.root, self.library))
        patcher.start(); self.addCleanup(patcher.stop)

    def test_image_deduplication_on_repeated_upload(self):
        buf = io.BytesIO()
        Image.new('RGB', (64, 64), 'green').save(buf, format='PNG')
        data = buf.getvalue()

        # Upload first time
        res1 = asyncio.run(app.upload_image(UploadFile(filename='green.png', file=io.BytesIO(data)), purpose='references', app_id='h3'))
        # Upload second time
        res2 = asyncio.run(app.upload_image(UploadFile(filename='green_copy.png', file=io.BytesIO(data)), purpose='references', app_id='h3'))

        self.assertEqual(res1['image_id'], res2['image_id'])
        # Only 1 file should exist in h3 references
        assets = self.library.list('images', app_id='h3', purpose='references')
        self.assertEqual(len(assets), 1)

    def test_single_and_batch_asset_deletion(self):
        buf1 = io.BytesIO(); Image.new('RGB', (40, 40), 'red').save(buf1, format='PNG')
        buf2 = io.BytesIO(); Image.new('RGB', (50, 50), 'blue').save(buf2, format='PNG')
        res1 = asyncio.run(app.upload_image(UploadFile(filename='red.png', file=io.BytesIO(buf1.getvalue()))))
        res2 = asyncio.run(app.upload_image(UploadFile(filename='blue.png', file=io.BytesIO(buf2.getvalue()))))

        id1, id2 = res1['image_id'], res2['image_id']
        self.assertTrue(self.library.exists(id1))
        self.assertTrue(self.library.exists(id2))

        # Single delete
        del_res = app.delete_asset_single(id1)
        self.assertTrue(del_res['ok'])
        self.assertFalse(self.library.exists(id1))

        # Deleting non-existent raises 404
        with self.assertRaises(HTTPException):
            app.delete_asset_single(id1)

        # Batch delete
        batch_res = app.delete_assets_batch(app.AssetBatchDeleteRequest(ids=[id2]))
        self.assertEqual(batch_res['count'], 1)
        self.assertFalse(self.library.exists(id2))

    def test_character_omits_deleted_images_when_accessed(self):
        buf = io.BytesIO(); Image.new('RGB', (40, 40), 'gold').save(buf, format='PNG')
        res = asyncio.run(app.upload_image(UploadFile(filename='gold.png', file=io.BytesIO(buf.getvalue())), purpose='characters', app_id='h3'))
        img_id = res['image_id']

        private = self.root / '.private'
        private.mkdir(exist_ok=True)
        characters = CharacterLibrary(private, self.library)
        profile = characters.save(CharacterProfile(name='测试角色', image_ids=[img_id], audios=[]))
        self.assertEqual(characters.get(profile.id).image_ids, [img_id])

        # Delete the asset
        app.delete_asset_single(img_id)
        self.assertFalse(self.library.exists(img_id))

        # Retrieving or saving character profile sets/omits the missing asset to empty
        retrieved = characters.get(profile.id)
        self.assertEqual(retrieved.image_ids, [])
        saved = characters.save(retrieved)
        self.assertEqual(saved.image_ids, [])

    def test_library_deduplicate_endpoint(self):
        # Force create two duplicate files in same folder
        buf = io.BytesIO(); Image.new('RGB', (60, 60), 'purple').save(buf, format='PNG')
        data = buf.getvalue()
        target_dir = self.library.folder('images', 'shared', 'imports')
        target_dir.mkdir(parents=True, exist_ok=True)
        p1 = target_dir / ('1'*32 + '.png')
        p2 = target_dir / ('2'*32 + '.png')
        p1.write_bytes(data)
        p2.write_bytes(data)

        items_before = self.library.list('images', app_id='shared', purpose='imports')
        self.assertEqual(len(items_before), 2)

        dedup_res = app.deduplicate_assets()
        self.assertTrue(dedup_res['ok'])
        self.assertEqual(dedup_res['removed'], 1)

        items_after = self.library.list('images', app_id='shared', purpose='imports')
        self.assertEqual(len(items_after), 1)

    def test_ui_batch_and_deduplicate_elements_present(self):
        studio_html = Path("studio.html").read_text(encoding="utf-8")
        self.assertIn('id="libraryManageToggle"', studio_html)
        self.assertIn('id="libraryTags"', studio_html)
        self.assertNotIn('libraryDeduplicate', studio_html)
        self.assertNotIn('libraryPurposeFilter', studio_html)
        self.assertIn('id="libraryBatchBar"', studio_html)
        self.assertIn('id="libraryBatchDelete"', studio_html)
        self.assertIn('id="librarySelectAll"', studio_html)

        ws_js = Path("static/workspace.js").read_text(encoding="utf-8")
        self.assertIn("librarySelected", ws_js)
        self.assertIn("isManageMode", ws_js)
        self.assertIn("setLibraryManageMode", ws_js)
        self.assertIn("toggleCardSelection", ws_js)
        self.assertIn("deleteSingleAsset", ws_js)
        self.assertIn("deleteBatchAssets", ws_js)
        self.assertIn("updateLibraryBatchBar", ws_js)

    def test_running_output_cannot_be_deleted_or_hidden(self):
        job_id = 'a' * 32
        asset_id = job_id + '.png'
        self.library.store(asset_id, b'output', app_id='qwen_image_2_1', purpose='outputs')
        job = dict(id=job_id, status='running', file=asset_id, config={})
        self.control.jobs[job_id] = job
        with self.assertRaises(HTTPException) as raised:
            app.delete_asset_single(asset_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(self.library.exists(asset_id))
        self.assertFalse(job.get('history_hidden'))

    def test_partial_batch_delete_reports_in_use_inputs_and_keeps_them(self):
        used, unused = 'a' * 32 + '.png', 'b' * 32 + '.png'
        self.library.store(used, b'reference')
        self.library.store(unused, b'unused')
        job_id = 'c' * 32
        self.control.jobs[job_id] = dict(id=job_id, status='queued',
            config={'references': [{'image_id': used}]})
        result = app.delete_assets_batch(app.AssetBatchDeleteRequest(ids=[used, unused, unused]))
        self.assertFalse(result['ok'])
        self.assertEqual(result['deleted'], [unused])
        self.assertEqual([error['id'] for error in result['errors']], [used])
        self.assertTrue(self.library.exists(used))
        self.assertFalse(self.library.exists(unused))

    def test_completed_image_deletion_hides_its_history(self):
        job_id = 'a' * 32
        asset_id = job_id + '.png'
        self.library.store(asset_id, b'output', app_id='qwen_image_2_1', purpose='outputs')
        self.control.jobs[job_id] = dict(id=job_id, status='done', file=asset_id, config={})
        app.delete_asset_single(asset_id)
        self.assertFalse(self.library.exists(asset_id))
        self.assertTrue(self.control.jobs[job_id]['history_hidden'])
        with self.assertRaises(HTTPException) as raised:
            app.delete_asset_single(asset_id)
        self.assertEqual(raised.exception.status_code, 404)

    def test_deduplication_keeps_referenced_files_and_generated_outputs(self):
        first, referenced = 'a' * 32 + '.png', 'b' * 32 + '.png'
        self.library.store(first, b'same', app_id='h3', purpose='characters')
        self.library.store(referenced, b'same', app_id='h3', purpose='characters')
        app.character_library.save(CharacterProfile(name='测试角色', image_ids=[referenced]))
        for asset_id in ['c' * 32 + '.png', 'd' * 32 + '.png']:
            self.library.store(asset_id, b'same output', app_id='qwen_image_2_1', purpose='outputs')
        result = app.deduplicate_assets()
        self.assertEqual(result['removed'], 0)
        self.assertTrue(all(self.library.exists(asset_id) for asset_id in
            [first, referenced, 'c' * 32 + '.png', 'd' * 32 + '.png']))

    def test_stale_hashes_cannot_merge_or_delete_different_files(self):
        first, changed = 'a' * 32 + '.png', 'b' * 32 + '.png'
        self.library.store(first, b'original')
        path = self.library.store(changed, b'original')
        path.write_bytes(b'changed content')
        self.assertEqual(len(self.library.list()), 2)
        result = app.deduplicate_assets()
        self.assertEqual(result['removed'], 0)
        self.assertEqual(path.read_bytes(), b'changed content')

    def test_existing_asset_id_does_not_silently_discard_different_content(self):
        asset_id = 'a' * 32 + '.png'
        self.library.store(asset_id, b'original')
        with self.assertRaises(ValueError):
            self.library.store(asset_id, b'different')
        self.assertEqual(self.library.resolve(asset_id).read_bytes(), b'original')

    def test_missing_qwen_reference_is_rejected_before_start(self):
        (self.root / 'google-token.dat').touch()
        config = app.QwenImageJobConfig(prompt='test', references=[{'slot': 1, 'image_id': 'a' * 32 + '.png'}])
        with patch.object(self.control, 'start') as start, self.assertRaises(HTTPException) as raised:
            app.create_job(config)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(len(config.references), 1)
        start.assert_not_called()

"""Small in-memory Drive protocol checks. No cloud allocation or network."""
import hashlib
import datetime
import json
from pathlib import Path
import tempfile
import unittest
import types
from unittest.mock import MagicMock, patch

from drive_cache import DriveCache
from drive_cache_remote import DriveModelCache
from mounted_drive_cache import MountedDriveModelCache
from apps.h3 import remote_h3 as h3
from native_drive_mount import install_mount_hook


class NativeMountHookTests(unittest.TestCase):
    def check_existing_callback(self, confirmed):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'drive-native-auth.json'
            state.write_text(json.dumps({'pending':True,'endpoint':'owned-vm','url':'https://accounts.google.com/test','confirmed':confirmed}))
            runtime, client, wsclient = MagicMock(), MagicMock(), MagicMock()
            client.colab_domain = 'https://colab.research.google.com'
            client.session.request.side_effect = [
                MagicMock(status_code=200,text=")]}'\n"+json.dumps({'token':'test-csrf'})),
                MagicMock(status_code=200,text=")]}'\n"+json.dumps({'success':True}))]
            install_mount_hook(runtime,client,'owned-vm',MagicMock(),root)
            def acknowledge(_):
                prior = json.loads(state.read_text())
                self.assertEqual(prior['url'],'https://accounts.google.com/test')
                prior['confirmed'] = True
                state.write_text(json.dumps(prior))
            with patch('native_drive_mount.time.sleep',side_effect=acknowledge):
                self.assertTrue(runtime.colab_request_hook({'content':{'request':{'authType':'dfs_ephemeral'}},'metadata':{'colab_msg_id':'message'}},wsclient))
            self.assertEqual(client.session.request.call_count,2)
            self.assertEqual(client.session.request.call_args.kwargs['params']['dryrun'],'false')
            self.assertFalse(json.loads(state.read_text())['pending'])
            wsclient.stdin_channel.send.assert_called_once()

    def test_confirmed_same_vm_does_not_reopen_consent(self):
        self.check_existing_callback(True)

    def test_waiting_same_vm_keeps_callback_until_acknowledged(self):
        self.check_existing_callback(False)


class DownloadRecoveryTests(unittest.TestCase):
    def test_xet_rate_limit_restarts_only_failed_file_over_http(self):
        constants = types.SimpleNamespace(HF_HUB_DISABLE_XET=False)
        module = types.ModuleType('huggingface_hub')
        module.constants = constants
        downloader = MagicMock(side_effect=[RuntimeError('CAS Client Error: 429 Too Many Requests'), 'model'])
        with patch.dict('sys.modules', {'huggingface_hub':module}), patch.object(h3.time, 'sleep'), patch.object(h3, 'event'), patch.dict(h3.os.environ):
            self.assertEqual(h3.download_model(downloader, repo_id='repo', filename='model'), 'model')
        self.assertTrue(constants.HF_HUB_DISABLE_XET)
        self.assertEqual(downloader.call_args.kwargs, {'force_download':True,'repo_id':'repo','filename':'model'})

    def test_other_errors_are_not_retried(self):
        downloader = MagicMock(side_effect=RuntimeError('access denied'))
        with self.assertRaisesRegex(RuntimeError, 'access denied'):
            h3.download_model(downloader, filename='model')
        self.assertEqual(downloader.call_count, 1)


def response(code=200, value=None, headers=None, content=b''):
    result = MagicMock(status_code=code, headers=headers or {})
    result.json.return_value = value or {}
    result.__enter__.return_value = result
    result.iter_content.return_value = [content]
    return result


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'model.safetensors'
        self.data = b'model-bytes'
        self.identity = dict(key='a'*64, sha256=hashlib.sha256(self.data).hexdigest(),
                             size=len(self.data), revision='pinned')
        self.emit = MagicMock()
        self.session = MagicMock()
        self.cache = DriveModelCache('test-only', self.emit, self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quota_project_header_is_shared_by_drive_requests(self):
        DriveModelCache('test-only', self.emit, self.session, quota_project='test-quota-project')
        self.session.headers.update.assert_any_call({'x-goog-user-project':'test-quota-project'})

    def test_expired_remote_token_is_renewed_once_without_oauth(self):
        provider = MagicMock(return_value='renewed-test-only')
        cache = DriveModelCache('expired-test-only', self.emit, self.session, token_provider=provider)
        self.session.request.side_effect = [response(401),response(value={'files':[]})]
        self.assertEqual(cache.find("name='model'"), [])
        provider.assert_called_once_with()
        self.session.headers.update.assert_any_call({'Authorization':'Bearer renewed-test-only'})

    def test_runtime_token_reply_rejects_other_prompts(self):
        local = DriveCache(self.root, lambda path:path.read_bytes(), lambda path,data:path.write_bytes(data))
        runtime = MagicMock()
        hook = local.runtime_stdin_hook(runtime)
        with patch.object(local,'remote_token',return_value='test-only'):
            hook({'content':{'prompt':'H3_DRIVE_ACCESS_TOKEN'}})
            runtime.kernel_client._manager.client.input.assert_called_once_with('test-only')
            with self.assertRaisesRegex(RuntimeError,'拒绝'):
                hook({'content':{'prompt':'unrecognized'}})
            self.assertEqual(runtime.kernel_client._manager.client.input.call_count,1)

    def test_local_quota_project_survives_disable_enable(self):
        local = DriveCache(self.root, lambda path:path.read_bytes(), lambda path,data:path.write_bytes(data))
        local.token_path.write_bytes(b'test-only')
        local.configure(False, 'api', 'test-quota-project')
        local.configure(True)
        local.configure(False)
        self.assertEqual(local.quota_project, 'test-quota-project')

    def test_existing_authorization_is_reused_without_new_consent(self):
        local = DriveCache(self.root, lambda path:path.read_bytes(), lambda path,data:path.write_bytes(data))
        local.token_path.write_bytes(b'test-only')
        with patch.object(local, 'credentials'), patch.object(local, 'quota'), patch('drive_cache.InstalledAppFlow') as flow:
            self.assertTrue(local.authorize_start()['reused'])
            flow.from_client_config.assert_not_called()

    def test_quota_project_preserves_token_expiry(self):
        local = DriveCache(self.root, lambda path:path.read_bytes(), lambda path,data:path.write_bytes(data))
        local.token_path.write_text(json.dumps({'token':'test-only','refresh_token':'test-only',
            'client_id':'test-only','client_secret':'test-only','expiry':'2030-01-01T00:00:00Z'}))
        local.configure(False, 'api', 'test-quota-project')
        credentials = local.credentials()
        self.assertEqual(credentials.quota_project_id, 'test-quota-project')
        self.assertEqual(credentials.expiry, datetime.datetime(2030,1,1))

    def test_disabled_api_does_not_start_new_oauth(self):
        local = DriveCache(self.root, lambda path:path.read_bytes(), lambda path,data:path.write_bytes(data))
        local.token_path.write_bytes(b'test-only')
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response(403, {'error':{'details':[{'reason':'SERVICE_DISABLED'}]}})
        with patch.object(local, 'credentials'), patch('drive_cache.AuthorizedSession',return_value=session), patch('drive_cache.InstalledAppFlow') as flow:
            with self.assertRaisesRegex(RuntimeError, '无需重新认证'):
                local.authorize_start()
            flow.from_client_config.assert_not_called()

    def file(self, verified='yes'):
        return dict(id='file-id', size=len(self.data),
                    appProperties={'sha256':self.identity['sha256'], 'verified':verified})

    def test_incomplete_upload_is_not_a_cache_hit(self):
        self.session.request.return_value = response(value={'files':[self.file('no')]})
        self.assertIsNone(self.cache.lookup(self.identity))

    def test_verified_restore_records_speed(self):
        self.session.request.side_effect = [response(value={'files':[self.file()]}), response(content=self.data)]
        self.assertTrue(self.cache.restore(self.path, self.identity))
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assertTrue(self.cache.metrics[0]['verified'])
        self.assertFalse(self.path.with_name(self.path.name+'.drive-partial').exists())

    def test_wrong_hash_never_publishes_a_model(self):
        self.session.request.side_effect = [response(value={'files':[self.file()]}), response(content=b'bad-content')]
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            self.cache.restore(self.path, self.identity)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.cache.metrics)

    def test_store_checks_server_md5_before_publishing(self):
        self.path.write_bytes(self.data)
        self.cache.folder = 'folder'
        self.session.request.side_effect = [response(value={'files':[]}),
            response(value={'storageQuota':{'limit':'100000','usage':'0'}}),
            response(headers={'Location':'https://www.googleapis.com/upload/drive/v3/files?upload_id=test'}),
            response(value={'id':'new-file','size':len(self.data), 'md5Checksum':hashlib.md5(self.data).hexdigest()}),
            response(value={'id':'new-file'})]
        self.cache.store(self.path, self.identity)
        final = self.session.request.call_args
        self.assertEqual(final.args[0], 'PATCH')
        self.assertEqual(final.kwargs['json']['appProperties']['verified'], 'yes')
        self.assertEqual(self.cache.metrics[0]['operation'], '保存')

    def test_store_checksum_failure_keeps_cache_unverified(self):
        self.path.write_bytes(self.data)
        self.cache.folder = 'folder'
        self.session.request.side_effect = [response(value={'files':[]}),
            response(value={'storageQuota':{}}),
            response(headers={'Location':'https://www.googleapis.com/upload/drive/v3/files?upload_id=test'}),
            response(value={'id':'new-file','size':len(self.data),'md5Checksum':'wrong'})]
        with self.assertRaisesRegex(RuntimeError, 'MD5'):
            self.cache.store(self.path, self.identity)
        self.assertFalse(any(call.args[0] == 'PATCH' for call in self.session.request.call_args_list))

    def test_insufficient_quota_stops_before_upload(self):
        self.path.write_bytes(self.data)
        self.session.request.side_effect = [response(value={'files':[]}),
                                           response(value={'storageQuota':{'limit':'1','usage':'0'}})]
        with self.assertRaisesRegex(RuntimeError, '空间不足'):
            self.cache.store(self.path, self.identity)
        self.assertEqual(self.session.request.call_count, 2)

    def test_partial_chunk_upload_continues_at_acknowledged_offset(self):
        self.path.write_bytes(self.data)
        self.cache.folder = 'folder'
        self.cache.CHUNK = 5
        self.session.request.side_effect = [response(value={'files':[]}), response(value={'storageQuota':{}}),
            response(headers={'Location':'https://www.googleapis.com/upload/drive/v3/files?upload_id=test'}),
            response(code=308, headers={'Range':'bytes=0-4'}), response(code=308, headers={'Range':'bytes=0-9'}),
            response(value={'id':'new-file','size':len(self.data),'md5Checksum':hashlib.md5(self.data).hexdigest()}),
            response(value={'id':'new-file'})]
        self.cache.store(self.path, self.identity)
        puts = [call for call in self.session.request.call_args_list if call.args[0]=='PUT']
        self.assertEqual([call.kwargs['data'] for call in puts],[self.data[:5],self.data[5:10],self.data[10:]])

    def test_drive_hit_when_hf_fails(self):
        cache = MagicMock()
        cache.restore.side_effect = lambda target, identity: (target.write_bytes(self.data), True)[1]
        downloader = MagicMock(side_effect=RuntimeError('HF connection error'))
        with patch.object(h3, 'ROOT', self.root), patch('drive_cache_remote.model_identity', return_value=self.identity), \
                patch.object(h3, 'MODEL_IDENTITIES', {('repo', 'model.safetensors'): self.identity}), \
                patch.object(h3.hashlib, 'file_digest', side_effect=AssertionError('Drive hit must not re-hash from disk')):
            path = h3.prepare_model('repo','model.safetensors','test',downloader,cache)
        self.assertEqual(path.read_bytes(),self.data)
        self.assertTrue(path.with_name(path.name + '.verified.json').exists())
        downloader.assert_called_once()
        cache.restore.assert_called_once()
        cache.store.assert_not_called()

    def test_hf_success_skips_drive_restore_and_generation_store(self):
        cache = MagicMock()
        def download(**kwargs):
            target = Path(kwargs['local_dir'])/'model.safetensors'
            target.write_bytes(self.data)
            return str(target)
        with patch.object(h3, 'ROOT', self.root), patch.object(h3,'event',self.emit), patch('drive_cache_remote.model_identity',return_value=self.identity):
            h3.prepare_model('repo','model.safetensors','test',download,cache)
        cache.restore.assert_not_called()
        cache.store.assert_not_called()
        metrics = [call.kwargs['cache_metric'] for call in self.emit.call_args_list if 'cache_metric' in call.kwargs]
        self.assertEqual(metrics[0]['operation'], 'Hugging Face 下载')

    def test_hf_failure_and_drive_miss_raises_with_backup_prompt(self):
        cache = MagicMock()
        cache.restore.return_value = False
        downloader = MagicMock(side_effect=RuntimeError('HF network unreachable'))
        with patch.object(h3, 'ROOT', self.root), patch('drive_cache_remote.model_identity', return_value=self.identity):
            with self.assertRaisesRegex(RuntimeError, '自动备份模型到 Drive'):
                h3.prepare_model('repo', 'model.safetensors', 'test', downloader, cache)

    def test_allow_store_stores_to_drive(self):
        cache = MagicMock()
        def download(**kwargs):
            target = Path(kwargs['local_dir'])/'model.safetensors'
            target.write_bytes(self.data)
            return str(target)
        with patch.object(h3, 'ROOT', self.root), patch('drive_cache_remote.model_identity', return_value=self.identity):
            h3.prepare_model('repo', 'model.safetensors', 'test', download, cache, allow_store=True)
        cache.store.assert_called_once()

    def test_authorization_is_separate_and_enable_requires_token(self):
        local = DriveCache(self.root, lambda path:path.read_bytes(), lambda path,data:path.write_bytes(data))
        self.assertFalse(local.enabled)
        with self.assertRaises(RuntimeError):
            local.configure(True)
        (self.root/'drive-token.dat').write_bytes(b'private-test-token')
        local.configure(True)
        self.assertTrue(local.status()['enabled'])
        self.assertFalse((self.root/'google-token.dat').exists())
        local.configure(False)
        self.assertFalse(local.enabled)


class MountedCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = b'test-model-data'
        self.identity = {'key':'a'*64,'filename':'folder/model.safetensors','size':len(self.data),
                         'sha256':hashlib.sha256(self.data).hexdigest(),'revision':'fixed'}
        self.cache = MountedDriveModelCache(MagicMock(), self.root/'drive'/'H3-Model-Cache')

    def tearDown(self):
        self.tmp.cleanup()

    def test_store_restore_hash_and_speed(self):
        source = self.root/'source.safetensors'; source.write_bytes(self.data)
        self.cache.store(source,self.identity)
        self.assertIsNotNone(self.cache.lookup(self.identity))
        target = self.root/'new-runtime'/'model.safetensors'
        self.assertTrue(self.cache.restore(target,self.identity))
        self.assertEqual(target.read_bytes(),self.data)
        self.assertEqual(self.cache.metrics[-1]['operation'],'Drive 读取')

    def test_tampered_cloud_copy_never_reaches_local_loader(self):
        source = self.root/'source.safetensors'; source.write_bytes(self.data)
        self.cache.store(source,self.identity)
        cloud,_ = self.cache.paths(self.identity)
        cloud.write_bytes(b'X'*len(self.data))
        target = self.root/'local.safetensors'
        with self.assertRaisesRegex(RuntimeError,'SHA-256'):
            self.cache.restore(target,self.identity)
        self.assertFalse(target.exists())

    def test_partial_or_missing_manifest_is_not_a_hit(self):
        cloud,_ = self.cache.paths(self.identity)
        cloud.parent.mkdir(parents=True); cloud.write_bytes(self.data)
        self.assertFalse(self.cache.lookup(self.identity))

    def test_invalid_version_cannot_escape_cache_folder(self):
        with self.assertRaises(ValueError):
            self.cache.paths({**self.identity,'key':'../../other-folder'})


if __name__ == '__main__':
    unittest.main()

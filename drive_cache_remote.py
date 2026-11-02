"""Private Drive model cache. Source is sent to Colab; no mounted filesystem."""
import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


def model_identity(repo, filename, revision=None):
    from huggingface_hub import get_hf_file_metadata, hf_hub_url
    metadata = get_hf_file_metadata(hf_hub_url(repo, filename, revision=revision))
    sha = metadata.etag.strip('"')
    if not re.fullmatch(r'[0-9a-f]{64}', sha) or not metadata.size:
        raise RuntimeError('模型源未提供 SHA-256，无法校验 Drive 缓存：' + filename)
    return {'repo': repo, 'filename': filename, 'revision': metadata.commit_hash,
            'sha256': sha, 'size': metadata.size,
            'key': hashlib.sha256((repo + '\n' + filename + '\n' + metadata.commit_hash).encode()).hexdigest()}


class DriveModelCache:
    API = 'https://www.googleapis.com/drive/v3'
    FIELDS = 'id,name,size,md5Checksum,appProperties'
    CHUNK = 64 * 1024**2

    def __init__(self, token, emit, session=None, quota_project=None, token_provider=None):
        self.session = session or requests.Session()
        self.session.headers.update({'Authorization': 'Bearer ' + token})
        if quota_project:
            self.session.headers.update({'x-goog-user-project': quota_project})
        self.emit = emit
        self.folder = None
        self.metrics = []
        self.token_provider = token_provider

    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', (15, 180))
        # Do not expose resumable URLs or credentials in exceptions/events.
        try:
            response = self.session.request(method, url, **kwargs)
            if response.status_code == 401 and self.token_provider:
                response.close()
                self.session.headers.update({'Authorization': 'Bearer ' + self.token_provider()})
                response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise RuntimeError('Drive 网络请求失败：' + type(exc).__name__) from None
        if response.status_code >= 400:
            raise RuntimeError('Drive 请求失败（HTTP %d）' % response.status_code)
        return response

    def find(self, query):
        files, page = [], None
        while True:
            params = {'q': "trashed = false and " + query,
                      'fields': 'nextPageToken,files(' + self.FIELDS + ')', 'pageSize': 100}
            if page:
                params['pageToken'] = page
            result = self.request('GET', self.API + '/files', params=params).json()
            files.extend(result.get('files', []))
            page = result.get('nextPageToken')
            if not page:
                return files

    def folder_id(self):
        if not self.folder:
            found = self.find("mimeType = 'application/vnd.google-apps.folder' and "
                              "appProperties has { key='h3_cache_folder' and value='v1' }")
            if found:
                self.folder = found[0]['id']
            else:
                self.folder = self.request('POST', self.API + '/files',
                    params={'fields': 'id'}, json={'name': 'H3-Model-Cache',
                    'mimeType': 'application/vnd.google-apps.folder',
                    'appProperties': {'h3_cache_folder': 'v1'}}).json()['id']
        return self.folder

    def lookup(self, identity):
        found = self.find("appProperties has { key='h3_key' and value='%s' }" % identity['key'])
        return next((file for file in found if int(file.get('size', 0)) == identity['size']
                     and file.get('appProperties', {}).get('sha256') == identity['sha256']
                     and file.get('appProperties', {}).get('verified') == 'yes'), None)

    def progress(self, operation, filename, done, total, started):
        elapsed = max(time.monotonic() - started, .001)
        self.emit('prepare', 'Drive %s %s：%.2f / %.2f GB，%.1f MB/s，已用时 %.0f 秒' %
                  (operation, filename, done / 1e9, total / 1e9, done / 1e6 / elapsed, elapsed))

    def record(self, operation, target, size, started, **extra):
        seconds = round(time.monotonic() - started, 3)
        metric = dict(operation=operation, filename=target.name, bytes=size, seconds=seconds,
                      mb_per_second=round(size / 1e6 / max(seconds, .001), 2), **extra)
        self.metrics.append(metric)
        self.emit('prepare', 'Drive %s完成 %s：%.1f 秒，%.1f MB/s' %
                  (operation, target.name, seconds, metric['mb_per_second']), cache_metric=metric)
        return metric

    def restore(self, target, identity):
        target = Path(target)
        file = self.lookup(identity)
        if not file:
            return False
        partial = target.with_name(target.name + '.drive-partial')
        target.parent.mkdir(parents=True, exist_ok=True)
        started = last = time.monotonic()
        digest, done = hashlib.sha256(), 0
        self.emit('prepare', '从 Drive 读取模型：' + target.name)
        try:
            with self.request('GET', self.API + '/files/' + file['id'],
                              params={'alt': 'media'}, stream=True,
                              headers={'Accept-Encoding': 'identity'}) as response, partial.open('wb') as output:
                for chunk in response.iter_content(8 * 1024**2):
                    if not chunk:
                        continue
                    if getattr(self, 'cancel_check', None) and self.cancel_check():
                        raise RuntimeError('下载已取消')
                    output.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if time.monotonic() - last >= 15:
                        self.progress('读取', target.name, done, identity['size'], started)
                        last = time.monotonic()
            if done != identity['size'] or digest.hexdigest() != identity['sha256']:
                raise RuntimeError('Drive 模型大小或 SHA-256 不符：' + target.name)
            partial.replace(target)
            self.record('读取', target, done, started, verified=True)
            return True
        finally:
            partial.unlink(missing_ok=True)

    def store(self, target, identity):
        target = Path(target)
        if self.lookup(identity):
            self.emit('prepare', 'Drive 已缓存：' + target.name)
            return
        size = target.stat().st_size
        sha, md5 = hashlib.sha256(), hashlib.md5()
        self.emit('prepare', '校验待保存到 Drive 的模型：' + target.name)
        checked, hash_started = 0, time.monotonic()
        hash_last = hash_started
        with target.open('rb') as source:
            for chunk in iter(lambda: source.read(8 * 1024**2), b''):
                sha.update(chunk)
                md5.update(chunk)
                checked += len(chunk)
                if time.monotonic() - hash_last >= 15:
                    self.progress('上传前校验', target.name, checked, size, hash_started)
                    hash_last = time.monotonic()
        if size != identity['size'] or sha.hexdigest() != identity['sha256']:
            raise RuntimeError('模型与源版本 SHA-256 不符，停止缓存：' + target.name)
        quota = self.request('GET', self.API + '/about', params={'fields': 'storageQuota'}).json()['storageQuota']
        if quota.get('limit') and int(quota['limit']) - int(quota.get('usage', 0)) < size:
            raise RuntimeError('Drive 剩余空间不足以保存：' + target.name)
        props = {'h3_key': identity['key'], 'sha256': identity['sha256'], 'verified': 'no',
                 'revision': identity['revision']}
        response = self.request('POST', 'https://www.googleapis.com/upload/drive/v3/files',
            params={'uploadType': 'resumable', 'fields': self.FIELDS},
            headers={'X-Upload-Content-Type': 'application/octet-stream', 'X-Upload-Content-Length': str(size)},
            json={'name': target.name, 'parents': [self.folder_id()], 'appProperties': props})
        location = response.headers.get('Location', '')
        parsed = urlparse(location)
        if parsed.scheme != 'https' or parsed.hostname != 'www.googleapis.com':
            raise RuntimeError('Drive 返回的上传地址不合法')
        started = last = time.monotonic()
        offset, failures, result = 0, 0, None
        self.emit('prepare', '开始保存模型到 Drive：' + target.name)
        with target.open('rb') as source:
            while offset < size:
                source.seek(offset)
                data = source.read(min(self.CHUNK, size - offset))
                headers = {'Content-Length': str(len(data)),
                           'Content-Range': 'bytes %d-%d/%d' % (offset, offset + len(data) - 1, size)}
                try:
                    response = self.request('PUT', location, data=data, headers=headers)
                except RuntimeError:
                    failures += 1
                    if failures > 4:
                        raise
                    time.sleep(min(2**failures, 15))
                    response = self.request('PUT', location, data=b'',
                        headers={'Content-Length': '0', 'Content-Range': 'bytes */%d' % size})
                if response.status_code in (200, 201):
                    result = response.json()
                    offset = size
                elif response.status_code == 308:
                    accepted = response.headers.get('Range', '')
                    new_offset = int(accepted.split('-')[-1]) + 1 if accepted else 0
                    if new_offset < offset or new_offset > size:
                        raise RuntimeError('Drive 分块上传返回了无效进度')
                    if new_offset == offset:
                        failures += 1
                        if failures > 4:
                            raise RuntimeError('Drive 分块上传没有进展')
                    else:
                        failures = 0
                    offset = new_offset
                else:
                    raise RuntimeError('Drive 上传未确认（HTTP %d）' % response.status_code)
                if time.monotonic() - last >= 15:
                    self.progress('保存', target.name, offset, size, started)
                    last = time.monotonic()
        if not result or int(result.get('size', 0)) != size or result.get('md5Checksum') != md5.hexdigest():
            raise RuntimeError('Drive 上传后的大小或 MD5 校验未通过：' + target.name)
        props['verified'] = 'yes'
        self.request('PATCH', self.API + '/files/' + result['id'], json={'appProperties': props}, params={'fields': 'id'})
        self.record('保存', target, size, started, verified=True, file_id=result['id'])

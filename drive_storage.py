"""Private result storage using the existing Drive authorization."""
import time
from urllib.parse import quote, urlparse

from google.auth.transport.requests import AuthorizedSession

API = 'https://www.googleapis.com/drive/v3/files'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
FIELDS = 'id,size,md5Checksum,sha256Checksum,trashed,appProperties'


class DriveStorage:
    def __init__(self, authorization):
        self.authorization = authorization

    @property
    def enabled(self):
        # Result storage is independent of the model-cache switch/backend.
        return self.authorization.token_path.is_file()

    def request(self, method, url, **kwargs):
        with AuthorizedSession(self.authorization.credentials()) as session:
            response = session.request(method, url, timeout=(15, 120), **kwargs)
        if response.status_code >= 400:
            response.close()
            raise RuntimeError('Drive 请求失败（HTTP %s）' % response.status_code)
        return response

    def preflight(self):
        quota = self.authorization.quota()
        if quota.get('limit') and int(quota.get('usage', 0)) >= int(quota['limit']):
            raise RuntimeError('Drive 空间已满，请清理空间后再生成')

    def folder(self):
        query = ("trashed = false and mimeType = 'application/vnd.google-apps.folder' "
                 "and appProperties has { key='h3_role' and value='videos' }")
        files = self.request('GET', API, params={'q': query, 'fields': 'files(id)', 'pageSize': 1}).json()['files']
        if files:
            return files[0]['id']
        return self.request('POST', API, params={'fields': 'id'}, json={
            'name': 'H3-Videos', 'mimeType': 'application/vnd.google-apps.folder',
            'appProperties': {'h3_role': 'videos'}}).json()['id']

    def ensure_file(self, job):
        if job.get('drive_upload_id'):
            return job['drive_upload_id']
        # Recover a prior create if the local process stopped before saving its ID.
        job_id = job['id']
        if not job_id.isalnum():
            raise ValueError('无效任务 ID')
        query = "trashed = false and appProperties has { key='h3_job' and value='%s' }" % job_id
        files = self.request('GET', API, params={'q': query, 'fields': 'files(id)', 'pageSize': 1}).json()['files']
        if files:
            return files[0]['id']
        return self.request('POST', API, params={'fields': 'id'}, json={
            'name': job_id + '.mp4', 'mimeType': 'video/mp4', 'parents': [self.folder()],
            'appProperties': {'h3_job': job_id}}).json()['id']

    def upload_target(self, job):
        result = job['remote_result']
        file_id = job['drive_upload_id']
        response = self.request('PATCH', UPLOAD + '/' + quote(file_id, safe=''),
            params={'uploadType': 'resumable'},
            headers={'X-Upload-Content-Type': 'video/mp4', 'X-Upload-Content-Length': str(result['size'])},
            json={'appProperties': {'h3_job': job['id'], 'sha256': result['sha256']}})
        url = response.headers.get('Location', '')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'www.googleapis.com' or parsed.username or parsed.password:
            raise RuntimeError('Drive 未返回有效上传地址，Colab 视频保留')
        return {'file_id': file_id, 'url': url,
                'headers': {'Content-Type': 'video/mp4', 'Content-Length': str(result['size'])}}

    def confirm_stored(self, stored):
        info = self.request('GET', API + '/' + quote(stored['file_id'], safe=''),
                            params={'fields': FIELDS}).json()
        if (info.get('trashed') or int(info.get('size', -1)) != stored['size']
                or info.get('md5Checksum') != stored['md5']
                or info.get('appProperties', {}).get('sha256') != stored['sha256']
                or (info.get('sha256Checksum') and info['sha256Checksum'] != stored['sha256'])):
            raise RuntimeError('Drive 视频长度或校验信息不匹配，Colab 视频保留')

    def verify(self, job, target, md5):
        stored = {'provider': 'drive', 'file_id': target['file_id'], 'job_id': job['id'],
                  'size': job['remote_result']['size'], 'sha256': job['remote_result']['sha256'],
                  'md5': md5, 'uploaded_at': time.time()}
        self.confirm_stored(stored)
        return stored

    def download_link(self, stored):
        # Playback stays behind the local proxy; never publish files or bearer tokens.
        return {**stored, 'download_url': '/video/' + quote(stored['job_id'], safe='') + '.mp4',
                'expires_at': None}

    def open_download(self, stored, headers=None):
        session = AuthorizedSession(self.authorization.credentials())
        try:
            response = session.get(API + '/' + quote(stored['file_id'], safe=''),
                params={'alt': 'media'}, headers=headers or {}, stream=True, timeout=(15, 60))
        except Exception:
            session.close()
            raise
        original_close = response.close
        def close():
            try:
                original_close()
            finally:
                session.close()
        response.close = close
        return response

"""Local Drive authorization, separate from the Colab credential."""
import json
import re
from importlib import resources
from pathlib import Path

from google.auth.transport.requests import Request, AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.file']


class DriveCache:
    def __init__(self, directory, load_secret, save_secret):
        self.directory = Path(directory)
        self.load_secret, self.save_secret = load_secret, save_secret
        self.flow = None

    @property
    def token_path(self):
        return self.directory / 'drive-token.dat'

    @property
    def enabled(self):
        try:
            return bool(json.loads((self.directory / 'drive-cache.json').read_text())['enabled'])
        except (OSError, ValueError, KeyError):
            return False

    @property
    def backend(self):
        try:
            return json.loads((self.directory / 'drive-cache.json').read_text()).get('backend', 'api')
        except (OSError, ValueError):
            return 'api'

    @property
    def quota_project(self):
        try:
            return json.loads((self.directory / 'drive-cache.json').read_text()).get('quota_project')
        except (OSError, ValueError):
            return None

    def status(self):
        result = {'authorized': self.token_path.exists(), 'enabled': self.enabled,
                  'folder_name': 'H3-Model-Cache', 'scope': 'native-drive' if self.backend == 'native' else 'drive.file',
                  'backend': self.backend, 'quota_project': self.quota_project}
        try:
            result['native_auth'] = json.loads((self.directory / 'drive-native-auth.json').read_text())
        except (OSError, ValueError):
            result['native_auth'] = {'pending': False}
        try:
            result['test_progress'] = json.loads((self.directory / 'drive-cache-progress.json').read_text())
        except (OSError, ValueError):
            result['test_progress'] = None
        return result

    def authorize_start(self):
        if self.token_path.exists():
            self.credentials()
            self.quota()
            self.configure(True, 'api')
            return {'reused': True, **self.status()}
        config = json.loads(resources.files('colab_cli').joinpath('oauth_config.json').read_text())
        self.flow = InstalledAppFlow.from_client_config(config, DRIVE_SCOPES)
        self.flow.redirect_uri = 'https://sdk.cloud.google.com/applicationdefaultauthcode.html'
        url, _ = self.flow.authorization_url(prompt='consent', token_usage='remote', access_type='offline')
        return {'url': url}

    def authorize_finish(self, code):
        if self.flow is None:
            raise RuntimeError('请先打开 Drive 授权页面')
        self.flow.fetch_token(code=code.strip(), timeout=60)
        self.save_secret(self.token_path, self.flow.credentials.to_json().encode())
        self.flow = None
        self.quota()
        self.configure(True)

    def configure(self, enabled, backend=None, quota_project=None):
        if enabled and not self.token_path.exists():
            raise RuntimeError('请先完成 Drive 授权')
        path = self.directory / 'drive-cache.json'
        temporary = path.with_suffix('.tmp')
        backend = backend or self.backend
        if backend not in ('api', 'native'):
            raise ValueError('Drive 缓存方式不合法')
        quota_project = self.quota_project if quota_project is None else quota_project.strip() or None
        if quota_project and not re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]+', quota_project):
            raise ValueError('Google Cloud 配额项目 ID 不合法')
        temporary.write_text(json.dumps({'enabled': bool(enabled), 'backend': backend,
                                         'quota_project': quota_project}))
        temporary.replace(path)
        return self.status()

    def credentials(self):
        if not self.token_path.exists():
            raise RuntimeError('请先完成 Drive 授权')
        info = json.loads(self.load_secret(self.token_path))
        info['quota_project_id'] = self.quota_project
        credentials = Credentials.from_authorized_user_info(info, scopes=DRIVE_SCOPES)
        if not credentials.valid:
            credentials.refresh(Request())
            self.save_secret(self.token_path, credentials.to_json().encode())
        return credentials

    def remote_token(self):
        # Only the short-lived access token goes to the owned Colab runtime.
        # Refresh credentials remain encrypted on this Windows account.
        return self.credentials().token

    def runtime_stdin_hook(self, runtime):
        def reply(message):
            if message.get('content', {}).get('prompt') != 'H3_DRIVE_ACCESS_TOKEN':
                raise RuntimeError('拒绝未识别的运行时凭据请求')
            runtime.kernel_client._manager.client.input(self.remote_token())
        return reply

    def quota(self):
        with AuthorizedSession(self.credentials()) as session:
            response = session.get('https://www.googleapis.com/drive/v3/about',
                                   params={'fields': 'storageQuota'}, timeout=(15, 60))
            if response.status_code >= 400:
                try:
                    details = response.json().get('error', {}).get('details', [])
                    reasons = {item.get('reason') for item in details}
                except (ValueError, AttributeError):
                    reasons = set()
                if 'SERVICE_DISABLED' in reasons:
                    project = self.quota_project or '未配置'
                    raise RuntimeError('Drive 授权仍有效；请在配额项目 ' + project + ' 启用 Google Drive API，无需重新认证')
                if 'USER_PROJECT_DENIED' in reasons:
                    raise RuntimeError('当前账号不能使用该配额项目，请检查项目权限；无需重新认证 Drive')
                raise RuntimeError('Drive API 检查失败（HTTP %d），请检查配额项目配置' % response.status_code)
            return response.json()['storageQuota']

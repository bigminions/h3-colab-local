"""LStudio local workspace, application jobs and compute coordination."""
import ctypes
from contextlib import asynccontextmanager
from ctypes import wintypes
import hashlib
import io
from importlib import resources
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import uuid
from urllib.parse import quote, urlparse

if sys.platform == 'win32':
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost
        def _silence_connection_lost(self, exc):
            try:
                _orig_call_connection_lost(self, exc)
            except (ConnectionResetError, OSError):
                pass
        _ProactorBasePipeTransport._call_connection_lost = _silence_connection_lost
    except Exception:
        pass

from fastapi import Body, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Annotated, Literal, Union
from PIL import Image, UnidentifiedImageError
from apps.h3 import DIMENSIONS, reference_prompt
from apps.h3 import CharacterLibrary, CharacterProfile, AudioAsset, AUDIO_ID, inspect_audio, validate_audio_asset
from drive_cache import DriveCache
from drive_storage import DriveStorage
from history_recycle import HistoryRecycle
from asset_library import AssetLibrary
from compute_colab import ColabProvider
from apps.h3 import H3Application, H3JobConfig
from apps.qwen_image import QwenImageApplication, QwenImageJobConfig
from workspace_runtime import ApplicationRegistry, ProviderRegistry, ProviderSpec
from job_queue import JobStore, waiting_jobs, application_id, queue_plan
# These imports also provide the runtime_services API injected into providers.
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import InstalledAppFlow
from colab_cli.client import Client, Prod, Variant, Accelerator, Shape
from colab_cli.runtime import ColabRuntime
from colab_cli.state import SessionState
from colab_cli.contents import ContentsClient
from runtime_recovery import observe_execution, connection_failure, TaskCancelled, ReconnectExhausted, enable_cancellation

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('LSTUDIO_DATA_ROOT') or BASE).resolve()
PRIVATE = DATA / '.private'
MATERIALS = DATA / 'materials'
VIDEOS = MATERIALS / 'h3' / 'outputs' / 'videos'
LOG = DATA / 'log'
PRIVATE.mkdir(parents=True, exist_ok=True)
VIDEOS.mkdir(parents=True, exist_ok=True)
LOG.mkdir(exist_ok=True)
SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/colaboratory']
PORT = int(os.environ.get('LSTUDIO_PORT', '7865'))
LOCAL_ORIGINS = {f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'}
GPU_ACCELERATORS = {Accelerator.G4, Accelerator.T4, Accelerator.L4, Accelerator.A100, Accelerator.H100}
CONNECT_PROGRESS_INTERVAL = 15


def atomic(path, text):
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    tmp.write_text(text, encoding='utf-8')
    try:
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def protect(data, decrypt=False):
    """Windows DPAPI: credentials can only be decrypted by this Windows user."""
    if os.name != 'nt':
        raise RuntimeError('本程序的凭据保护需要 Windows DPAPI')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    dll = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    dll.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    dll.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    if decrypt:
        ok = dll.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        ok = dll.CryptProtectData(ctypes.byref(source), 'LStudio', None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def save_secret(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes(protect(data))
    tmp.replace(path)


def load_secret(path):
    return protect(path.read_bytes(), decrypt=True)


COLAB_GPUS = ('G4', 'A100', 'H100', 'L4', 'T4')
applications = ApplicationRegistry([H3Application(), QwenImageApplication()])
providers = ProviderRegistry([
    ProviderSpec('colab', 'Google Colab', True, '使用自己的 Colab 计算单元，按需连接运行时', COLAB_GPUS),
    ProviderSpec('runpod', 'RunPod', False, '按需 GPU 实例'),
    ProviderSpec('modal', 'Modal', False, '按任务启动 GPU 容器'),
])
asset_library = AssetLibrary(MATERIALS)


drive_cache = DriveCache(PRIVATE, load_secret, save_secret)
character_library = CharacterLibrary(PRIVATE, asset_library)


def safe_error(exc):
    # Never surface credential-bearing URL/query strings from requests exceptions.
    if hasattr(exc, 'response') and getattr(exc, 'response', None) is not None:
        response = exc.response
        return f'Google/Colab 请求失败（HTTP {response.status_code}），请检查授权、余额或显卡供应。'
    s = re.sub(r'https?://\S+', '[远程地址]', str(exc))
    return s[:1500]


class TimedSession(AuthorizedSession):
    def request(self, method, url, **kwargs):
        path = urlparse(url).path
        allocating = method.upper() == 'POST' and path == '/tun/m/assign'
        kwargs.setdefault('timeout', (15, 180 if allocating else 60))
        started = time.monotonic()
        try:
            return super().request(method, url, **kwargs)
        except Exception as exc:
            # Keep operation/timing, never authenticated URLs, queries or headers.
            exc.h3_request = {'method': method.upper(), 'service': 'Google/Colab',
                              'operation': {'/tun/m/assign': '分配显卡',
                                            '/tun/m/assignments': '查询运行时'}.get(path, '请求'),
                              'seconds': round(time.monotonic() - started, 1)}
            raise


def connection_diagnostic(exc, operation):
    import requests
    context = getattr(exc, 'h3_request', {})
    phase = getattr(exc, 'h3_phase', None) or operation
    if context.get('method'):
        phase += ' / ' + context['method'] + ' ' + context['operation']
    if '10013' in str(exc):
        reason = '本机网络访问被拒绝（WinError 10013），请检查启动权限或防火墙'
    elif isinstance(exc, requests.ReadTimeout):
        reason = '等待 Google/Colab 响应超时（ReadTimeout）'
    elif isinstance(exc, requests.ConnectTimeout):
        reason = '连接 Google/Colab 超时（ConnectTimeout）'
    elif isinstance(exc, (requests.Timeout, TimeoutError)):
        reason = '网络请求超时（' + type(exc).__name__ + '）'
    elif getattr(exc, 'response', None) is not None:
        code = getattr(exc.response, 'status_code', None)
        reason = 'Google/Colab 返回 HTTP ' + str(code) if code else safe_error(exc)
    elif isinstance(exc, requests.ConnectionError):
        reason = '网络连接失败（' + type(exc).__name__ + '），请检查网络或代理'
    else:
        reason = safe_error(exc)
    detail = {'phase': phase, 'type': type(exc).__name__, 'reason': reason}
    if context:
        detail['seconds'] = context['seconds']
    return detail


class TimedContents(ContentsClient):
    def _request(self, method, path, params=None, json_data=None):
        import requests
        response = requests.request(method, self.base_url + '/api/contents/' + quote(path.strip('/'), safe='/'),
            params={'authuser': '0', 'colab-runtime-proxy-token': self.token, **(params or {})},
            json=json_data, timeout=(15, 180))
        response.raise_for_status()
        return response.json()


class ReleasePolicy(BaseModel):
    mode: Literal['always', 'idle', 'manual'] = 'idle'
    idle_minutes: int = Field(default=10, ge=1, le=120)


H3JobConfig.character_profiles = lambda: character_library.list()
runtime_services = sys.modules[__name__]
providers.register('colab', lambda control: ColabProvider(control, runtime_services))

# Observed account reference; live consumption replaces it when available.
A100_REFERENCE_RATE = 6.77


def valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def single_a100_rate(data):
    consumption = data.get('consumption', {})
    runtimes = data.get('runtimes', [])
    rate = consumption.get('consumption_rate_hourly')
    if (len(runtimes) == 1 and runtimes[0].get('gpu') == 'A100'
            and consumption.get('assignments_count', 1) == 1 and valid_number(rate) and rate > 0):
        return rate
    return None


def a100_estimate(data):
    rate = data.get('a100_rate_hourly')
    source = 'last_observed'
    if not valid_number(rate) or rate <= 0:
        rate, source = A100_REFERENCE_RATE, 'reference'
    elif single_a100_rate(data) == rate and not data.get('stale'):
        source = 'live'
    balance = data.get('consumption', {}).get('paid_compute_units_balance')
    hours = balance / rate if valid_number(balance) and balance >= 0 else None
    if hours is not None and not math.isfinite(hours):
        hours = None
    return {'gpu': 'A100', 'rate_hourly': rate, 'rate_source': source,
            'rate_updated_at': data.get('a100_rate_updated_at') if source != 'reference' else None,
            'hours': hours, 'remaining_minutes': math.floor(hours * 60) if hours is not None and math.isfinite(hours * 60) else None}


def g4_estimate(data):
    rate = data.get('g4_rate_hourly')
    if not valid_number(rate) or rate <= 0:
        return {'gpu': 'G4', 'rate_hourly': None, 'rate_source': 'unknown', 'hours': None, 'remaining_minutes': None}
    consumption = data.get('consumption', {})
    live = (len(data.get('runtimes', [])) == 1 and data['runtimes'][0].get('gpu') == 'G4'
            and consumption.get('assignments_count') == 1 and consumption.get('consumption_rate_hourly') == rate and not data.get('stale'))
    balance = consumption.get('paid_compute_units_balance')
    hours = balance / rate if valid_number(balance) and balance >= 0 else None
    if hours is not None and not math.isfinite(hours * 60):
        hours = None
    return {'gpu': 'G4', 'rate_hourly': rate, 'rate_source': 'live' if live else 'last_observed',
            'rate_updated_at': data.get('g4_rate_updated_at'), 'hours': hours,
            'remaining_minutes': math.floor(hours * 60) if hours is not None else None}


def generic_gpu_estimate(data, gpu):
    if gpu == 'A100':
        return a100_estimate(data)
    if gpu == 'G4':
        return g4_estimate(data)
    rate = data.get(f'{gpu.lower()}_rate_hourly')
    if not valid_number(rate) or rate <= 0:
        return {'gpu': gpu, 'rate_hourly': None, 'rate_source': 'unknown', 'hours': None, 'remaining_minutes': None}
    consumption = data.get('consumption', {})
    live = (len(data.get('runtimes', [])) == 1 and data['runtimes'][0].get('gpu') == gpu
            and consumption.get('assignments_count') == 1 and consumption.get('consumption_rate_hourly') == rate and not data.get('stale'))
    balance = consumption.get('paid_compute_units_balance')
    hours = balance / rate if valid_number(balance) and balance >= 0 else None
    if hours is not None and not math.isfinite(hours * 60):
        hours = None
    return {'gpu': gpu, 'rate_hourly': rate, 'rate_source': 'live' if live else 'last_observed',
            'rate_updated_at': data.get(f'{gpu.lower()}_rate_updated_at'), 'hours': hours,
            'remaining_minutes': math.floor(hours * 60) if hours is not None else None}


class AccountCache:
    """Caches read-only account data so polling tabs do not flood Google."""
    def __init__(self, provider, path, now=time.time):
        self.provider, self.path, self.now = provider, path, now
        self.lock = threading.Lock()
        self.data, self.last_attempt, self.error = None, 0, None
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                pass

    def get(self, refresh=False):
        with self.lock:
            now = self.now()
            interval = 5 if refresh else 25
            if now - self.last_attempt >= interval:
                self.last_attempt = now
                try:
                    source = self.provider()
                    previous = self.data or {}
                    self.data = {'runtimes': source['runtimes'], 'consumption': source['consumption'], 'updated_at': self.now()}
                    for key in ('a100_rate_hourly', 'a100_rate_updated_at', 'g4_rate_hourly', 'g4_rate_updated_at'):
                        if key in previous:
                            self.data[key] = previous[key]
                    for key, val in previous.items():
                        if (key.endswith('_rate_hourly') or key.endswith('_rate_updated_at')) and key not in self.data:
                            self.data[key] = val
                    rate = single_a100_rate(self.data)
                    if rate is not None:
                        self.data.update(a100_rate_hourly=rate, a100_rate_updated_at=self.data['updated_at'])
                    consumption = self.data['consumption']
                    runtimes = self.data.get('runtimes', [])
                    active_rate = consumption.get('consumption_rate_hourly')
                    if (len(runtimes) == 1 and consumption.get('assignments_count') == 1
                            and valid_number(active_rate) and active_rate > 0):
                        gpu_name = runtimes[0].get('gpu')
                        if gpu_name:
                            self.data[f'{gpu_name.lower()}_rate_hourly'] = active_rate
                            self.data[f'{gpu_name.lower()}_rate_updated_at'] = self.data['updated_at']
                    self.error = None
                    atomic(self.path, json.dumps(self.data))
                except Exception as exc:
                    self.error = safe_error(exc)
            if self.data is None:
                raise RuntimeError(self.error or '账户余额暂时不可用，请稍后刷新。')
            result = {**self.data, 'stale': bool(self.error) or now - self.data['updated_at'] > 90, 'error': self.error}
            all_gpus = set(COLAB_GPUS)
            for r in result.get('runtimes', []):
                if r.get('gpu'):
                    all_gpus.add(r['gpu'])
            estimates = {g: generic_gpu_estimate(result, g) for g in all_gpus}
            estimates['A100'] = a100_estimate(result)
            estimates['G4'] = g4_estimate(result)
            return {**result, 'a100_estimate': a100_estimate(result),
                    'gpu_estimates': estimates}


class Controller:
    def __init__(self, directory=PRIVATE, videos=VIDEOS, transport=None, now=time.time, result_drive=None):
        self.directory, self.videos = directory, videos
        self.lock = threading.RLock()
        self.cancel_wakeup = threading.Event()
        self.active = None
        self.session = None
        self.jobs = {}
        self.drive_storage = result_drive or DriveStorage(drive_cache if directory == PRIVATE else DriveCache(directory, load_secret, save_secret))
        self.recycling = set()
        self.preparing_videos = set()
        self.verified_videos = {}
        self.download_threads = {}
        self.download_semaphore = threading.Semaphore(2)
        self.open_video_directory = lambda folder: os.startfile(str(folder))
        self.history_recycle = HistoryRecycle(directory / 'recovered-works',videos,self.drive_storage,asset_library=asset_library)
        self.now = now
        self.release_policy = ReleasePolicy()
        self.release_revision = 0
        self.idle_since = None
        self.release_error = None
        self.release_message = None
        self.release_retry_at = 0
        self.release_checking = False
        self.monitor_stop = threading.Event()
        self.monitor_thread = None
        self.worker_thread = None
        self._in_queue_worker = False
        self.store = JobStore(directory / 'studio.sqlite3')
        self.consumer = {'state': 'disconnected', 'checked_at': None}
        state = self.store.get_state('release')
        if state:
            try:
                self.release_policy = ReleasePolicy(**state['policy'])
                self.release_revision = int(state.get('revision', 0))
                self.idle_since = state.get('idle_since') if valid_number(state.get('idle_since')) else None
            except (ValueError, KeyError, OSError, TypeError):
                pass
        self.jobs = self.store.load_jobs()
        if self.jobs:
            for job in self.jobs.values():
                if job['status'] in ('running', 'cancelling'):
                    job.update(status='interrupted', message='本地程序中断；等待核对远端任务，可继续取回结果。')
            for batch_id in {j.get('batch_id') for j in self.jobs.values()} - {None}:
                siblings = [j for j in self.jobs.values() if j.get('batch_id') == batch_id]
                if all(j.get('file') for j in siblings):
                    for child in siblings:
                        child['batch_complete'] = True
                        if child['status'] == 'interrupted':
                            child['status'] = 'done'
        if (directory / 'session.dat').exists():
            self.session = json.loads(load_secret(directory / 'session.dat'))
        self.provider_id = 'colab'
        self.transport = transport or providers.create(self.provider_id, self)
        if self.session and self.idle_since is None:
            self.idle_since = self.now()
        self.save_jobs()

    def save_release_state(self):
        self.store.set_state('release', {
            'policy': self.release_policy.model_dump(), 'revision': self.release_revision, 'idle_since': self.idle_since})

    def configure_release(self, policy):
        with self.lock:
            if self.release_checking:
                raise HTTPException(409, '正在检查并释放显卡，请稍后修改配置')
            if policy.mode != self.release_policy.mode:
                self.idle_since = self.now() if self.session and not self.active else None
            self.release_policy = policy
            self.release_revision += 1
            self.release_error = self.release_message = None
            self.release_retry_at = 0
            self.save_release_state()
            return self.release_status()

    def release_status(self):
        deadline = (self.idle_since + self.release_policy.idle_minutes * 60
                    if self.session and not self.active and not waiting_jobs(self.jobs) and self.idle_since is not None and self.release_policy.mode == 'idle' else None)
        return {'policy': self.release_policy.model_dump(), 'revision': self.release_revision,
                'deadline': deadline, 'checking': self.release_checking,
                'error': self.release_error, 'message': self.release_message,
                'monitor_running': bool(self.monitor_thread and self.monitor_thread.is_alive())}

    def occupying_app_info(self, applications_registry=None):
        registry = applications_registry or globals().get('applications')
        active_id = self.active
        active_job = self.jobs.get(active_id) if isinstance(active_id, str) else None
        if active_job:
            app_id = active_job.get('app_id') or active_job.get('config', {}).get('app_id')
        elif self.session:
            app_id = self.session.get('app_id')
            if not app_id:
                # Look for the most recent job matching this session endpoint
                recent = sorted((j for j in self.jobs.values() if j.get('endpoint') == self.session.get('endpoint') and j.get('status') not in ('cancelled', 'discarded')),
                                key=lambda j: j.get('created', 0), reverse=True)
                if recent:
                    app_id = recent[0].get('app_id') or recent[0].get('config', {}).get('app_id')
                    self.session['app_id'] = app_id
                    self.save_session()
        else:
            app_id = None
        app_name = None
        if app_id:
            try:
                app_obj = registry.get(app_id) if registry else None
                app_name = app_obj.spec.name if app_obj else app_id
            except Exception:
                app_name = 'H3 视频' if app_id == 'h3' else ('Qwen-Image-2.1' if app_id == 'qwen_image_2_1' else app_id)
        return app_id, app_name

    def pending_jobs(self):
        return [j for j in self.jobs.values() if j['status'] in ('error', 'interrupted', 'queued', 'running', 'cancelling')
                and not (j.get('file') or j.get('drive')) and not j.get('safe_release')]

    def check_cancelled(self, job):
        if job.get('cancel_requested'):
            raise TaskCancelled('任务已请求取消')

    def connected_operation(self, job, operation, label='执行视频任务'):
        for attempt in range(4):
            self.check_cancelled(job)
            try:
                return operation()
            except Exception as exc:
                self.check_cancelled(job)
                if not connection_failure(exc):
                    raise
                detail = connection_diagnostic(exc, label)
                job.setdefault('connection_errors', []).append(detail)
                job['connection_errors'] = job['connection_errors'][-20:]
                job['failure_reason'] = detail['phase'] + '：' + detail['reason']
                elapsed = '，本次等待 %.1f 秒' % detail['seconds'] if 'seconds' in detail else ''
                if attempt == 3:
                    self.event(job, 'recover', job['failure_reason'] + elapsed + '；连续重试失败')
                    raise ReconnectExhausted(job['failure_reason']) from exc
                job['reconnect_attempts'] = job.get('reconnect_attempts',0) + 1
                self.event(job,'recover',job['failure_reason'] + elapsed + f'；正在自动重试 {attempt+1}/3，保留原任务')
                self.cancel_wakeup.wait(2 * (attempt+1))

    def cancel_targets(self, job):
        return [j for j in self.jobs.values() if j['id'] == job['id'] or
                job.get('batch_id') and j.get('batch_id') == job['batch_id']]

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise HTTPException(404,'任务不存在')
            targets = self.cancel_targets(job)
            target_ids = {j['id'] for j in targets}
            if job.get('status') == 'queued' and (not self.active or self.active not in target_ids):
                for target in targets:
                    if target.get('status') == 'queued':
                        target.update(cancel_requested=True, status='cancelled', safe_release=True, batch_complete=True)
                        self.event(target, 'cancelled', '任务已取消')
                self.save_jobs()
                return {'ok': True, 'pending': False, 'message': '任务已取消'}
            if self.active and self.active not in target_ids:
                raise HTTPException(409,'另一个任务正在运行，不能取消它的显卡')
            if not self.active and all(j.get('file') or j.get('drive') or j.get('safe_release') for j in targets):
                raise HTTPException(409,'该任务已结束，无需取消')
            if self.session and job.get('endpoint') and job['endpoint'] != self.session['endpoint']:
                raise HTTPException(409,'此任务不属于当前运行时，未执行取消')
            for target in targets:
                if not (target.get('file') or target.get('drive')):
                    target.update(cancel_requested=True,status='cancelling')
            # A saved active item may still be uploading or own the remaining batch.
            if self.active:
                self.jobs[self.active]['cancel_requested'] = True
            self.cancel_wakeup.set()
            active = self.active
            self.event(job,'cancel','正在取消任务，保留显卡和已准备的环境')
        if not active:
            self.finish_cancel(job)
        return_pending = bool(self.active)
        return {'ok':True,'pending':return_pending,'message':job.get('message')}

    def finish_cancel(self, job):
        with self.lock:
            if job.get('cancel_finishing'):
                return
            job['cancel_finishing'] = True
        try:
            if self.session:
                if job.get('endpoint') and job['endpoint'] != self.session['endpoint']:
                    raise RuntimeError('任务与当前运行时不一致，无法确认取消')
                targets = self.cancel_targets(job)
                remote_job = next((j for j in targets if j['id'] == self.active), None)
                if remote_job is None:
                    remote_job = max((j for j in targets if j.get('execution_started')
                        and not (j.get('file') or j.get('drive'))),
                        key=lambda j: j.get('batch_index', 0), default=job)
                self.transport.cancel_job(remote_job)
            elif job.get('assignment_pending'):
                self.transport.cancel_pending_assignment(job)
            with self.lock:
                for target in self.cancel_targets(job):
                    saved = bool(target.get('file') or target.get('drive'))
                    target.update(status='done' if saved else 'cancelled',safe_release=True,batch_complete=True)
                    target.pop('cancel_error',None)
                self.release_error = self.release_message = None
                message = ('已取消剩余执行，已保存的结果和显卡环境保留' if job.get('file') or job.get('drive') else
                           '任务已取消，显卡环境保留，继续处理后续任务')
                if job.get('auto_cancel_reason'):
                    message += '；失败原因：' + job['auto_cancel_reason']
                self.event(job,'done' if job.get('file') or job.get('drive') else 'cancelled', message)
        except Exception as exc:
            job.update(status='error',safe_release=False,cancel_error=safe_error(exc))
            self.event(job,'cancel','取消尚未确认：' + safe_error(exc) + '；环境保留，请重试取消')
        finally:
            job.pop('cancel_finishing',None)
            self.save_jobs()

    def resubmit(self, job_id, placement="back"):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or not job.get('config'):
                raise HTTPException(404, '任务不存在或缺少配置')
            if job.get('file') or job.get('drive'):
                raise HTTPException(400, '该任务已有成片，无需重新提交')
            if job.get('status') in ('running', 'queued', 'cancelling'):
                raise HTTPException(409, '任务正在处理或已在排队中')

            job.update(status='queued', stage='connect', message='排队中，等待执行',
                       safe_release=False, batch_complete=False, created=self.now())
            job.pop('remote_result', None)
            job.pop('execution_started', None)
            job.pop('consumer_run_id', None)
            job.pop('prompt_id', None)
            job.pop('cancel_requested', None)
            job.pop('auto_cancel_reason', None)
            job.pop('history_hidden', None)
            job.pop('local_download_error', None)
            job.pop('gpu_release_error', None)
            job.pop('cancel_error', None)

            job.update(queue_order=self.queue_order(placement == 'front'), queue_front=placement == 'front')
            self.event(job, 'connect', '已重新提交任务，加入排队')

            self.ensure_worker()

            return {'ok': True, 'id': job_id}

    def verify_saved_results(self, endpoint):
        for job in self.jobs.values():
            if job.get('endpoint') != endpoint:
                continue
            if job.get('drive'):
                self.drive_storage.confirm_stored(job['drive'])
            if job.get('file'):
                app_id = job.get('config', {}).get('app_id', 'h3')
                application = applications.get(app_id)
                output_folder = self.output_folder_for(application) if application else self.videos
                path = output_folder / job['file']
                if not path.is_file():
                    is_video = self.app_output_kind(application) == 'video' if application else True
                    noun = '视频' if is_video else '图片'
                    raise HTTPException(409, f'本机{noun}文件已丢失，运行时保留，请先取回结果。')
                result = job.get('remote_result')
                if result:
                    with path.open('rb') as handle:
                        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                    if path.stat().st_size != result['size'] or digest != result['sha256']:
                        is_video = self.app_output_kind(application) == 'video' if application else True
                        noun = '视频' if is_video else '图片'
                        raise HTTPException(409, f'本机{noun}校验失败，运行时保留，请先取回结果。')

    def release_online_gpu(self, endpoint):
        with self.lock:
            if self.active or self.release_checking:
                raise HTTPException(409, '任务执行或检查中，请完成后再释放显卡')
            if self.session and self.session['endpoint'] == endpoint:
                return self.check_and_release()
            if any(job.get('endpoint') == endpoint for job in self.pending_jobs()):
                raise HTTPException(409, '该显卡有未取回的结果，请先取回再释放')
            self.release_checking = True
            try:
                self.verify_saved_results(endpoint)
                released = self.transport.release_detected_gpu(endpoint)
                return {'ok': True, 'endpoint': endpoint,
                        'message': '已检查并确认显卡已释放' if released else '该显卡已离线，无需释放'}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(400, safe_error(exc)) from None
            finally:
                self.release_checking = False

    def on_runtime_recycled(self, endpoint):
        with self.lock:
            if self.session and self.session.get('endpoint') == endpoint:
                self.session = None
                self.save_session()
            affected = False
            for job in self.jobs.values():
                if job.get('endpoint') == endpoint and not (job.get('file') or job.get('drive')) and not job.get('safe_release'):
                    job.update(safe_release=True)
                    if job.get('status') in ('running', 'queued', 'cancelling'):
                        job.update(status='interrupted', message='Colab 运行时已被回收，未能完成生成。')
                    elif job.get('status') in ('error', 'interrupted'):
                        msg = job.get('message', '')
                        if '运行时已离线' not in msg and '已被回收' not in msg:
                            job.update(message=(msg + '；原 Colab 运行时已离线。').strip('；'))
                    affected = True
            if affected:
                self.save_jobs()
            self.idle_since = None
            self.release_error = None
            self.release_message = '已检查：当前没有本控制台管理的显卡，无需释放'
            self.save_release_state()

    def _discard_pending_jobs(self):
        pending = self.pending_jobs()
        for job in pending:
            if job.get('status') == 'queued':
                continue
            job.update(safe_release=True, status='discarded')
        if pending:
            self.save_jobs()

    def check_and_release(self, discard_pending=False, force=False):
        with self.lock:
            if self.active:
                raise HTTPException(409, '任务执行中，请先取消任务，确认停止后再释放显卡')
            if not self.session:
                self._discard_pending_jobs()
                self.idle_since = None
                self.release_message = '已检查：当前没有本控制台管理的显卡，无需释放'
                self.release_error = None
                self.save_release_state()
                return {'ok': True, 'message': self.release_message}
            pending = self.pending_jobs()
            if any(j.get('status') != 'queued' for j in pending) and not discard_pending and not force:
                raise HTTPException(409, '存在未取回的视频。先重试取回，再检查并释放。')
            self.release_checking = True
            try:
                if not force:
                    self.verify_saved_results(self.session['endpoint'])
                    if not self.transport.check_idle():
                        raise HTTPException(409, 'Colab 仍有任务运行或排队，显卡已保留，请稍后再检查。')
                if self.session:
                    self.transport.release()
                self._discard_pending_jobs()
                self.idle_since = None
                self.release_error = None
                self.release_message = '已检查并确认显卡已释放'
                self.save_release_state()
                return {'ok': True, 'message': self.release_message}
            except HTTPException as exc:
                self.release_error = exc.detail
                raise
            except Exception as exc:
                self.release_error = safe_error(exc)
                raise HTTPException(400, self.release_error) from None
            finally:
                self.release_checking = False

    def idle_tick(self):
        with self.lock:
            if not self.session or self.active or waiting_jobs(self.jobs) or self.release_policy.mode == 'manual' or self.now() < self.release_retry_at:
                return
            deadline = self.release_status()['deadline']
            if self.release_policy.mode == 'idle' and (deadline is None or self.now() < deadline):
                return
            try:
                self.check_and_release()
            except HTTPException as exc:
                self.release_error = exc.detail
                self.release_retry_at = self.now() + 60

    def start_idle_monitor(self):
        if self.monitor_thread and self.monitor_thread.is_alive():
            return
        self.monitor_stop.clear()
        def monitor():
            while not self.monitor_stop.wait(5):
                try:
                    self.refresh_consumer()
                    if waiting_jobs(self.jobs):
                        self.ensure_worker()
                    self.idle_tick()
                except Exception as exc:
                    self.release_error = safe_error(exc)
                    self.release_retry_at = self.now() + 60
        self.monitor_thread = threading.Thread(target=monitor, daemon=True)
        self.monitor_thread.start()

    def save_session(self):
        path = self.directory / 'session.dat'
        if self.session:
            save_secret(path, json.dumps(self.session).encode())
        elif path.exists():
            path.unlink()

    def save_jobs(self):
        with self.lock:
            self.store.save_jobs(self.jobs)

    def history_clearable(self, job):
        if job['id'] in self.recycling or job['id'] in self.preparing_videos:
            return False
        if job.get('status') in ('queued', 'running', 'cancelling') or job['id'] == self.active:
            return False
        active = self.jobs.get(self.active) or {}
        if active.get('batch_id') and active['batch_id'] == job.get('batch_id'):
            return False
        return bool(job.get('file') or job.get('drive') or job.get('safe_release') or
                    job.get('status') not in ('error', 'interrupted'))

    def clear_history(self, job_id=None):
        with self.lock:
            if job_id is not None:
                if job_id not in self.jobs:
                    raise HTTPException(404, '作品记录不存在')
                job = self.jobs[job_id]
                if not self.history_clearable(job):
                    raise HTTPException(409, '该任务正在处理或结果待取回，请完成处理后清除')
                targets = [job]
            else:
                targets = [j for j in self.jobs.values() if self.history_clearable(j)]
            cleared = [j['id'] for j in targets if not j.get('history_hidden')]
            for job_id in cleared:
                self.jobs[job_id]['history_hidden'] = True
            if cleared:
                self.save_jobs()
            return {'ok': True, 'ids': cleared, 'count': len(cleared)}

    def restore_history(self, ids):
        with self.lock:
            if any(job_id not in self.jobs for job_id in ids):
                raise HTTPException(404, '作品记录不存在')
            if any(self.jobs[job_id].get('history_recycle_token') for job_id in ids):
                raise HTTPException(409,'请在系统回收箱中还原整个作品文件夹')
            restored = []
            for job_id in dict.fromkeys(ids):
                if self.jobs[job_id].pop('history_hidden', False):
                    restored.append(job_id)
            if restored:
                self.save_jobs()
            return {'ok': True, 'ids': restored, 'count': len(restored)}

    def recover_history(self):
        with self.lock:
            if self.history_recycle.recover(self.jobs,self.recycling,self.save_jobs): self.save_jobs()

    def valid_local_video(self, path, expected):
        if not path.is_file():
            return False
        if not expected:
            return True
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, expected.get('sha256'))
        if stat.st_size != expected.get('size'):
            return False
        if self.verified_videos.get(path) == signature:
            return True
        if self.history_recycle.digest(path) != expected.get('sha256'):
            return False
        after = path.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != signature[:3]:
            return False
        self.verified_videos[path] = signature
        return True

    def load_history_video(self, job_id):
        existing_thread = None
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.get('history_hidden'): raise HTTPException(404,'作品记录不存在')
            if job_id in self.recycling:
                raise HTTPException(409,'这条作品正在处理，请稍后重试')
            t = self.download_threads.get(job_id)
            if isinstance(t, threading.Thread) and t is not threading.current_thread() and t.is_alive():
                existing_thread = t

        if existing_thread:
            existing_thread.join(timeout=120)
            with self.lock:
                target = self.history_recycle.video(job.get('file') or job_id + '.mp4')
                expected = job.get('drive') or job.get('remote_result')
                if self.valid_local_video(target, expected):
                    return {'ok': True, 'file': job.get('file')}
                if existing_thread.is_alive():
                    raise HTTPException(409, '视频正在从 Drive 下载中，请稍候…')
                if job.get('local_download_error'):
                    raise HTTPException(400, '无法加载视频文件：' + str(job['local_download_error']))

        with self.lock:
            if job_id in self.preparing_videos:
                raise HTTPException(409,'视频正在从 Drive 下载中，请稍候…')
            if not (job.get('file') or job.get('drive')): raise HTTPException(409,'视频尚未保存')
            self.preparing_videos.add(job_id)
        temporary = None
        try:
            target = self.history_recycle.video(job.get('file') or job_id + '.mp4')
            expected = job.get('drive') or job.get('remote_result')
            if not self.valid_local_video(target, expected):
                stored = job.get('drive')
                if not stored: raise HTTPException(404,'本机视频缺失或损坏，且没有 Drive 副本')
                if target.exists() and not job.get('file'):
                    raise HTTPException(409,'保存位置已有其他文件，未覆盖')
                if not target.exists():
                    target = self.history_recycle.video(job_id + '.mp4')
                    if target.exists() and not self.valid_local_video(target, expected):
                        raise HTTPException(409,'保存位置已有其他文件，未覆盖')
                self.videos.mkdir(parents=True,exist_ok=True)
                if not self.valid_local_video(target, expected):
                    temporary = target.with_name('.' + uuid.uuid4().hex + '.partial')
                    self.history_recycle.download(stored,temporary)
                    temporary.replace(target)
                    self.verified_videos.pop(target, None)
            with self.lock:
                changed = job.get('file') != target.name or bool(job.get('local_download_error'))
                job['file'] = target.name
                if job.pop('local_download_error', None) and job['status'] in ('warning', 'interrupted'):
                    job['status'] = 'warning' if job.get('gpu_release_error') else 'done'
                if changed:
                    self.save_jobs()
            return {'ok':True,'file':job.get('file')}
        except HTTPException:
            raise
        except Exception as exc:
            with self.lock:
                job['local_download_error'] = safe_error(exc)
                self.save_jobs()
            raise HTTPException(400,'无法加载视频文件：' + safe_error(exc)) from None
        finally:
            try:
                if temporary is not None: temporary.unlink(missing_ok=True)
            finally:
                with self.lock: self.preparing_videos.discard(job_id)

    def download_drive_result(self, job):
        self.event(job, 'download', '正在从 Drive 下载到本机，完成校验后即可播放')
        try:
            self.load_history_video(job['id'])
        except Exception as exc:
            detail = str(exc.detail) if isinstance(exc, HTTPException) else safe_error(exc)
            job.update(status='warning', local_download_error=detail)
            self.event(job, 'download', '视频已保存在 Drive，本机下载未完成；可重新下载，无需生成：' + detail)
            return False
        job['status'] = 'warning' if job.get('gpu_release_error') else 'done'
        self.event(job, 'done', '视频已下载到本机并校验，Drive 副本保留用于恢复' +
                   ('；显卡释放尚未确认' if job.get('gpu_release_error') else ''))
        return True

    def start_drive_download(self, job):
        job_id = job['id']
        with self.lock:
            if job_id in self.preparing_videos or job_id in self.download_threads:
                return self.download_threads.get(job_id)

        def runner():
            with self.download_semaphore:
                try:
                    self.download_drive_result(job)
                finally:
                    with self.lock:
                        self.download_threads.pop(job_id, None)

        thread = threading.Thread(target=runner, name=f'drive-download-{job_id}', daemon=True)
        with self.lock:
            self.download_threads[job_id] = thread
        thread.start()
        return thread

    def wait_drive_downloads(self, job_ids=None, timeout=None):
        with self.lock:
            if job_ids is None:
                threads = list(self.download_threads.values())
            else:
                threads = [self.download_threads[jid] for jid in job_ids if jid in self.download_threads]
        for t in threads:
            t.join(timeout=timeout)

    def app_output_kind(self, application):
        return getattr(getattr(application, 'spec', None), 'output_kind', getattr(application, 'output_kind', 'video'))

    def output_folder_for(self, application):
        if self.app_output_kind(application) == 'image':
            folder = asset_library.folder('images', application.spec.id, 'outputs')
        else:
            folder = self.videos
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def open_history_folder(self, job_id):
        try:
            job = self.jobs.get(job_id)
            if job and job.get('config', {}).get('app_id') == 'qwen_image_2_1':
                folder = asset_library.folder('images', 'qwen_image_2_1', 'outputs')
                self.open_video_directory(folder)
                return {'ok': True}
            res = self.load_history_video(job_id)
            target = self.history_recycle.video(res.get('file') or job_id + '.mp4')
            self.open_video_directory(target.parent)
            return {'ok':True}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400,'无法打开文件夹：' + safe_error(exc)) from None

    def recycle_history(self, job_id=None, app_id=None):
        with self.lock:
            if job_id is not None:
                job = self.jobs.get(job_id)
                if not job:
                    batch_jobs = [j for j in self.jobs.values() if j.get('batch_id') == job_id and not j.get('history_hidden') and self.history_clearable(j)]
                    if batch_jobs:
                        targets = batch_jobs
                    else:
                        raise HTTPException(404, '作品记录不存在')
                else:
                    batch_id = job.get('batch_id')
                    if batch_id:
                        targets = [j for j in self.jobs.values() if j.get('batch_id') == batch_id and not j.get('history_hidden') and self.history_clearable(j)]
                    else:
                        if not self.history_clearable(job):
                            raise HTTPException(409, '该任务正在处理或结果待取回')
                        targets = [job] if not job.get('history_hidden') else []
            else:
                targets = [job for job in self.jobs.values() if not job.get('history_hidden')
                           and (job.get('file') or job.get('drive'))
                           and (app_id is None or (job.get('config') or {}).get('app_id', 'h3') == app_id)
                           and self.history_clearable(job)]
            self.recycling.update(job['id'] for job in targets)
        completed = []; errors = []
        try:
            for job in targets:
                folder = manifest = None
                try:
                    with self.lock:
                        keep = bool(job.get('file') and any(other['id'] != job['id'] and not other.get('history_hidden') and other.get('file') == job['file'] for other in self.jobs.values()))
                    folder,manifest = self.history_recycle.prepare(job,keep_source=keep)
                    with self.lock:
                        job['history_recycle_pending'] = {'folder':folder.name,'token':manifest['token']}
                        self.save_jobs()
                    try:
                        self.history_recycle.send(folder)
                    except Exception:
                        if folder.exists(): raise
                    with self.lock:
                        job.update(history_hidden=True,history_recycle_token=manifest['token'])
                        job.pop('history_recycle_pending',None)
                        self.save_jobs(); completed.append(job['id'])
                except Exception as exc:
                    # If the system operation failed, return the original video before reporting it.
                    if folder is not None and folder.exists():
                        try: self.history_recycle.rollback(folder,manifest)
                        except Exception: pass
                    with self.lock:
                        if folder is None or not folder.exists(): job.pop('history_recycle_pending',None)
                        self.save_jobs()
                    errors.append({'id':job['id'],'message':safe_error(exc)})
        finally:
            with self.lock: self.recycling.difference_update(job['id'] for job in targets)
        return {'ok':not errors,'ids':completed,'count':len(completed),'errors':errors}

    def event(self, job, stage, message):
        with self.lock:
            job['stage'], job['message'] = stage, message
            job['events'].append({'timestamp': self.now(), 'time': time.strftime('%H:%M:%S'), 'stage': stage, 'message': message})
            job['events'] = job['events'][-120:]
            self.save_jobs()

    def ensure_worker(self):
        with self.lock:
            if self.worker_thread is None or not self.worker_thread.is_alive():
                self.worker_thread = threading.Thread(target=self._run_queue, daemon=True)
                self.worker_thread.start()

    def queue_order(self, front=False):
        positions = [j.get('queue_order', j.get('created', 0)) for j in self.jobs.values()]
        return (min(positions, default=0) - 100) if front else (max(positions, default=0) + 100)

    def queue_snapshot(self):
        with self.lock:
            return queue_plan(self.jobs, self.session)

    def refresh_consumer(self):
        with self.lock:
            endpoint = (self.session or {}).get('endpoint')
            active_id = self.active
        if not endpoint:
            state = {'state': 'connecting' if active_id else 'disconnected'}
        else:
            try:
                state = self.transport.consumer_status()
            except Exception as exc:
                state = {'state': 'unknown', 'error': safe_error(exc)}
        with self.lock:
            if endpoint == (self.session or {}).get('endpoint'):
                self.consumer = {**state, 'endpoint': endpoint, 'checked_at': self.now()}
                self.store.set_state('consumer', self.consumer)
            return dict(self.consumer)

    def queue_blocker(self):
        endpoint = (self.session or {}).get('endpoint')
        return next((j for j in self.pending_jobs() if j.get('status') in ('error', 'interrupted', 'cancelling')
                     and (endpoint and j.get('endpoint') == endpoint or j.get('assignment_pending'))), None)

    def _run_queue(self):
        self._in_queue_worker = True
        target = None
        restart_if_queued = False
        try:
            while not self.monitor_stop.is_set():
                with self.lock:
                    blocker = self.queue_blocker()
                    queued = waiting_jobs(self.jobs, self.session)
                    if blocker or not queued:
                        restart_if_queued = not blocker
                        self.active = None
                        self.idle_since = self.now() if self.session else None
                        if blocker:
                            self.consumer = {**self.consumer, 'state': 'blocked', 'job_id': blocker['id']}
                        self.save_release_state()
                        self.save_jobs()
                        return
                    target = queued[0]
                    if 'release_mode' in target.get('config', {}):
                        self.configure_release(ReleasePolicy(mode=target['config']['release_mode'],
                            idle_minutes=target['config'].get('idle_minutes', 10)))
                    self.active = target['id']
                    target.update(status='running')
                    self.idle_since = None
                    self.release_error = self.release_message = None
                    self.save_jobs()
                    self.save_release_state()
                try:
                    if self.session:
                        state = self.refresh_consumer()
                        recovering = (target.get('recover_existing') and state['state'] == 'busy'
                                      and state.get('job_id') == target['id']
                                      and state.get('run_id') == target.get('consumer_run_id'))
                        if state['state'] != 'idle' and not recovering:
                            with self.lock:
                                target['status'] = 'queued'
                            return
                        config = target['config']
                        if (self.provider_id != config.get('provider_id', 'colab') or
                                self.session.get('app_id') != application_id(target) or
                                self.session.get('accelerator') != config.get('gpu')):
                            self.consumer = {**self.consumer, 'state': 'switching'}
                            self.verify_saved_results(self.session['endpoint'])
                            self.transport.release()
                            # Providers clear session only after release has been confirmed.
                            if self.session:
                                raise RuntimeError('释放尚未确认，暂停环境切换')
                    target['queue_front'] = False
                    self.cancel_wakeup.clear()
                    target.pop('history_hidden', None)
                    self.run(target)
                    with self.lock:
                        target.pop('recover_existing', None)
                        if target.get('batch_id'):
                            siblings = self.cancel_targets(target)
                            if target['status'] in ('error', 'interrupted'):
                                for child in siblings:
                                    if child['status'] == 'queued':
                                        child.update(status='interrupted', message='本批暂停，重试后继续生成')
                            if all(j['status'] not in ('queued', 'running', 'cancelling') for j in siblings):
                                for child in siblings:
                                    child['batch_complete'] = True
                except Exception as exc:
                    with self.lock:
                        target.update(status='queued')
                        self.release_error = safe_error(exc)
                        self.consumer = {**self.consumer, 'state': 'unknown', 'error': self.release_error}
                        self.event(target, 'connect', '消费者状态未确认，队列暂停：' + self.release_error)
                    return
                finally:
                    with self.lock:
                        self.active = None
                        self.save_jobs()
        finally:
            with self.lock:
                self._in_queue_worker = False
                self.active = None
                self.worker_thread = None
                self.idle_since = self.now() if self.session else None
                self.save_release_state()
                self.save_jobs()
                # A submitter may have observed this worker as alive just after
                # its empty-queue check. Transfer that wakeup before returning.
                if restart_if_queued and waiting_jobs(self.jobs) and not self.monitor_stop.is_set():
                    self.ensure_worker()

    def start(self, config, retry=None, placement="back"):
        if config is not None:
            providers.validate(applications.get(config.app_id), config.provider_id, config.gpu)
        with self.lock:
            if retry and self.jobs.get(retry, {}).get('drive'):
                job = self.jobs[retry]
                if job.get('history_hidden') or retry in self.recycling:
                    raise HTTPException(409, '这条作品正在处理或已移除')
                job.pop('local_download_error', None)
                job.update(stage='download', message='正在从 Drive 重新下载到本机')
                self.save_jobs()
                self.start_drive_download(job)
                return retry
            if (PRIVATE / 'drive-cache-session.dat').exists():
                raise HTTPException(409, 'CPU 正在准备 Drive 模型缓存，请等保存完成后再生成')
            if drive_cache.enabled and drive_cache.backend == 'api':
                try:
                    drive_cache.quota()
                except Exception as exc:
                    raise HTTPException(400, 'Drive 缓存未就绪：' + safe_error(exc) + '；尚未申请显卡') from None
            if self.drive_storage.enabled:
                try:
                    self.drive_storage.preflight()
                except Exception as exc:
                    raise HTTPException(400, 'Drive 成片存储未就绪：' + safe_error(exc) + '；尚未申请显卡') from None
            if retry:
                job = self.jobs.get(retry)
                if not job or job.get('file') or job.get('drive'):
                    raise HTTPException(400, '该任务无需重新取回')
                if job.get('cancel_requested'):
                    raise HTTPException(409,'任务正在取消或取消尚未确认，请先重试取消')
                retry_endpoint = job.get('endpoint')
                if not retry_endpoint and job.get('batch_id'):
                    retry_endpoint = next((j.get('endpoint') for j in self.cancel_targets(job) if j.get('endpoint')), None)
                if not self.session or retry_endpoint != self.session['endpoint'] or job['status'] == 'discarded':
                    raise HTTPException(409, '原任务的运行时已离线，无法在另一个运行时取回。')
                batch = sorted((j for j in self.jobs.values() if j.get('batch_id') == job.get('batch_id')
                                and job.get('batch_id') and not (j.get('file') or j.get('drive'))),
                               key=lambda j: j['batch_index'])
                if batch:
                    job = batch[0]
                    for child in batch:
                        child.update(status='queued', recover_existing=True)
                        child.pop('history_hidden', None)
                else:
                    job.update(status='queued', recover_existing=True)
                    job.pop('history_hidden', None)
                target_id = job['id']
            else:
                job_id = uuid.uuid4().hex
                job = dict(id=job_id, config=config.model_dump(), events=[], status='queued', stage='connect', message='排队中，等待执行', created=time.time())
                self.jobs[job_id] = job
                if config.count > 1:
                    for index in range(config.count):
                        child = job if index == 0 else dict(id=uuid.uuid4().hex, events=[], status='queued',
                            stage='connect', message='排队中，等待执行', created=job['created'] + index / 1000)
                        child.update(config={**config.model_dump(), 'count': 1, 'seed': (config.seed + index) % 2**53},
                                     batch_id=job_id, batch_index=index + 1, batch_total=config.count, batch_complete=False)
                        self.jobs[child['id']] = child
                target_id = job['id']

            front = placement == 'front'
            position = self.queue_order(front)
            for index, child in enumerate(batch if retry and batch else
                    [j for j in self.jobs.values() if j['id'] == target_id or j.get('batch_id') == target_id]):
                child.update(queue_order=position + index, queue_front=front)
            self.idle_since = None
            self.save_jobs()
            self.save_release_state()
            self.ensure_worker()
            return target_id

    def run(self, job):
        application = applications.get(job['config'].get('app_id', 'h3'))
        config = application.config_model(**job['config'])
        try:
            if config.provider_id != self.provider_id:
                if self.session:
                    raise RuntimeError('请先释放当前算力再切换提供商')
                self.transport = providers.create(config.provider_id, self)
                self.provider_id = config.provider_id
            if job.get('drive'):
                self.download_drive_result(job)
                return
            self.event(job, 'connect', '正在连接 ' + config.gpu + '；只有提交任务才申请显卡')
            self.connected_operation(job,lambda:self.transport.connect(config), '连接／申请 ' + config.gpu)
            job['endpoint'] = self.session['endpoint']
            self.check_cancelled(job)
            self.save_jobs()
            self.event(job, 'connect', config.gpu + ' 运行时已连接，准备执行任务')
            if not job.get('remote_result'):
                self.connected_operation(job,lambda:self.transport.execute(job))
            self.check_cancelled(job)
            result = job['remote_result']
            output_kind = self.app_output_kind(application)
            if self.drive_storage.enabled and output_kind == 'video':
                self.event(job, 'download', '从 Colab 上传到 Drive，校验后自动下载到本机')
                job['drive'] = self.connected_operation(job,lambda:self.transport.upload_drive(job), '上传视频到 Drive')
                self.save_jobs()
            else:
                download_desc = '下载图片到本机并校验 SHA-256' if output_kind == 'image' else '下载 MP4 到本机并校验 SHA-256'
                self.event(job, 'download', download_desc)
                output_folder = self.output_folder_for(application)
                partial = output_folder / (job['id'] + '.partial')
                op_name = '下载图片到本机' if output_kind == 'image' else '下载视频到本机'
                self.connected_operation(job,lambda:self.transport.download(result, partial), op_name)
                with partial.open('rb') as handle:
                    digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                if partial.stat().st_size != result['size'] or digest != result['sha256']:
                    raise RuntimeError('下载校验未通过，Colab 运行时已保留，请重试取回。')
                target = output_folder / (job['id'] + application.output_extension)
                partial.replace(target)
                job['file'] = target.name
            job['metadata'] = {k: result[k] for k in application.metadata_fields if k in result}
            saved = '图片已保存到本机' if output_kind == 'image' else ('视频已上传 Drive 并校验' if job.get('drive') else '视频已保存到本机')
            self.event(job, 'saved', saved)
            self.check_cancelled(job)
            has_more = any(j.get('status') == 'queued' and not j.get('cancel_requested') for j in self.jobs.values())
            release_now = self.release_policy.mode == 'always' and not has_more
            if release_now:
                self.event(job, 'release', '正在释放 Colab 显卡')
                try:
                    self.transport.release()
                except Exception as exc:
                    job['status'] = 'warning'
                    self.release_error = safe_error(exc)
                    job['gpu_release_error'] = self.release_error
                    self.release_retry_at = self.now() + 60
                    self.event(job, 'release', '视频已保存，但释放尚未确认：' + safe_error(exc))
                    if not job.get('drive'):
                        return
            if job.get('drive'):
                next_waiting = has_more
                if next_waiting:
                    job['status'] = 'warning' if job.get('gpu_release_error') else 'done'
                    kept = (f'{saved}，闲置 {self.release_policy.idle_minutes} 分钟后检查并释放显卡'
                             if self.release_policy.mode == 'idle' else saved + '，显卡保留；请手动检查并释放')
                    self.event(job, 'done', saved + '，显卡已释放' if release_now else kept)
                    self.start_drive_download(job)
                    self.check_cancelled(job)
                    return
                self.download_drive_result(job)
                self.check_cancelled(job)
                return
            job['status'] = 'done'
            kept = (f'{saved}，闲置 {self.release_policy.idle_minutes} 分钟后检查并释放显卡'
                     if self.release_policy.mode == 'idle' else saved + '，显卡保留；请手动检查并释放')
            self.event(job, 'done', saved + '，显卡已释放' if release_now else kept)
        except Exception as exc:
            if job.get('cancel_requested') or isinstance(exc, (TaskCancelled,ReconnectExhausted)):
                job['cancel_requested'] = True
                if isinstance(exc,ReconnectExhausted):
                    job['auto_cancel_reason'] = str(exc)
                    self.event(job,'cancel','自动重连连续失败：' + str(exc) + '；正在确认原任务状态，保留显卡环境')
                self.finish_cancel(job)
                return
            job['status'] = 'error'
            message = safe_error(exc)
            if '自动备份模型到 Drive' in message or 'Drive 备用缓存未找到' in message:
                job['drive_backup_needed'] = True
            can_retrieve = bool(self.session and job.get('endpoint') and job.get('endpoint') == self.session['endpoint']
                                and (job.get('remote_result') or job.get('execution_started')))
            if self.release_policy.mode == 'always' and job.get('safe_release') and not job.get('remote_result') and self.session and not waiting_jobs(self.jobs):
                try:
                    self.transport.release()
                    message += '；失败任务的显卡已释放。'
                except Exception as release_exc:
                    message += '；释放失败：' + safe_error(release_exc)
            elif can_retrieve:
                job['safe_release'] = False
                message += '；运行时保留，请重试取回结果。'
            else:
                job['safe_release'] = True
                if not job.get('endpoint'):
                    message += '；尚未连接显卡，可重新提交任务。'
                elif not (job.get('remote_result') or job.get('execution_started')):
                    message += '；尚未开始生成，可重新提交任务。'
            self.event(job, 'error', message)
        finally:
            with self.lock:
                if not getattr(self, '_in_queue_worker', False):
                    self.active = None
                    self.idle_since = self.now() if self.session else None
                self.save_release_state()
                self.save_jobs()

    def public_drive(self, stored):
        if not stored:
            return None
        try:
            return self.drive_storage.download_link(stored)
        except Exception as exc:
            return {**stored, 'download_url': None, 'expires_at': None, 'error': safe_error(exc)}

    def public_job(self, job, queue_info=None):
        config = job.get('config', {})
        active = self.jobs.get(self.active) or {}
        siblings = sorted((j for j in self.jobs.values() if job.get('batch_id') and j.get('batch_id') == job['batch_id']),
                          key=lambda j: j['batch_index'])
        app_id = config.get('app_id', 'h3')
        application = applications.get(app_id)
        is_video = self.app_output_kind(application) == 'video'
        output_folder = self.videos if is_video else asset_library.folder('images', app_id, 'outputs')
        local_file = bool(job.get('file') and (output_folder / job['file']).is_file() and not job.get('local_download_error'))
        is_preparing = bool(job['id'] in self.preparing_videos or job['id'] in self.download_threads)
        creation_keys = (
            'title', 'prompt', 'negative_prompt', 'prompt_processing', 'performance', 'encoder',
            'quantized_model', 'quantized_vae', 'veda', 'aspect_ratio', 'resolution',
            'duration', 'mode', 'seed', 'people', 'strength', 'references',
            'generation_type', 'reference_size', 'characters',
            'model_file', 'steps', 'cfg', 'denoise',
        )
        creation_data = {**{k: config[k] for k in creation_keys if k in config}, 'count': job.get('batch_total', 1), 'gpu': config.get('gpu', 'A100')}
        if 'references' in creation_data and isinstance(creation_data['references'], list):
            enriched_refs = []
            for r in creation_data['references']:
                ref_dict = dict(r) if isinstance(r, dict) else r.model_dump()
                img_id = ref_dict.get('image_id')
                if img_id and not ref_dict.get('name'):
                    try:
                        resolved_path = asset_library.resolve(img_id)
                        meta_file = resolved_path.with_suffix(resolved_path.suffix + '.json')
                        if meta_file.is_file():
                            meta = json.loads(meta_file.read_text(encoding='utf-8'))
                            if meta.get('name'):
                                ref_dict['name'] = meta['name']
                    except Exception:
                        pass
                enriched_refs.append(ref_dict)
            creation_data['references'] = enriched_refs

        if queue_info is None:
            queue_info = next((j for j in self.queue_snapshot() if j['id'] == job['id']), {})
        return {'app_id': app_id, 'provider_id': config.get('provider_id', 'colab'),
                'queue_position': queue_info.get('position'), 'queue_reason': queue_info.get('reason'),
                **{k: v for k, v in job.items() if k not in ('remote_result', 'config', 'safe_release', 'drive_upload_id')},
                'local_file': local_file,
                'is_preparing': is_preparing,
                'is_downloading': is_preparing,
                'batch': {'id': job['batch_id'], 'total': job['batch_total'],
                          'completed': sum(bool(j.get('file') and not j.get('local_download_error')) for j in siblings),
                          'finished': bool(job.get('batch_complete')), 'ids': [j['id'] for j in siblings]} if siblings else None,
                'creation': creation_data,
                'can_clear_history': self.history_clearable(job),
                'drive': self.public_drive(job.get('drive')),
                'recovery_kind': 'download' if job.get('drive') else 'retrieve' if job.get('remote_result') else 'resume',
                'can_cancel': bool(job['id'] == self.active or job.get('batch_id') and active.get('batch_id') == job['batch_id'] or job.get('assignment_pending') and job.get('cancel_requested') or job.get('status') == 'queued' or self.session and
                    job.get('endpoint') == self.session['endpoint'] and job['status'] in ('error','interrupted','queued','running','cancelling') and not (job.get('file') or job.get('drive'))),
                'can_retry': bool(job.get('drive') and not local_file and not is_preparing
                    or not job.get('cancel_requested') and not self.active and self.session and job.get('endpoint') == self.session['endpoint'] and job['status'] in ('error', 'interrupted') and not (job.get('file') or job.get('drive'))),
                'can_resubmit': bool(not (job.get('file') or job.get('drive')) and job.get('status') not in ('running', 'queued', 'cancelling', 'done') and bool(job.get('config')))}


controller = Controller()
account_cache = AccountCache(lambda: controller.transport.account(), PRIVATE / 'account-cache.json')
key_path = PRIVATE / 'api-key.dat'
if key_path.exists():
    API_KEY = load_secret(key_path).decode()
else:
    API_KEY = secrets.token_urlsafe(32)
    save_secret(key_path, API_KEY.encode())
@asynccontextmanager
async def lifespan(application):
    controller.start_idle_monitor()
    if waiting_jobs(controller.jobs):
        controller.ensure_worker()
    yield
    controller.monitor_stop.set()


app = FastAPI(title='LStudio · LocalStudio', docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
for app_inst in applications:
    app_web = getattr(app_inst, 'web_dir', None) or (BASE / 'apps' / app_inst.spec.id / 'web')
    if app_web and app_web.is_dir():
        app.mount(f'/apps/{app_inst.spec.id}/web', StaticFiles(directory=app_web), name=f'{app_inst.spec.id}_web')
oauth_flow = None


@app.middleware('http')
async def local_only(request: Request, call_next):
    hostname = request.headers.get('host', '').split(':')[0]
    origin = request.headers.get('origin')
    if hostname not in ('127.0.0.1', 'localhost') or request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin not in LOCAL_ORIGINS):
        return JSONResponse({'detail': '只允许本机访问'}, status_code=403)
    if request.url.path.startswith('/api/') and not secrets.compare_digest(request.headers.get('X-LStudio-Key', ''), API_KEY):
        return JSONResponse({'detail': '需要本地 API Key'}, status_code=401)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


def render_studio_html():
    raw_html = (BASE / 'studio.html').read_text(encoding='utf-8')
    app_styles, app_scripts, app_contents = [], [], []
    for app_inst in applications:
        app_id = app_inst.spec.id
        app_web = getattr(app_inst, 'web_dir', None) or (BASE / 'apps' / app_id / 'web')
        if not app_web or not app_web.is_dir():
            continue
        content_path = app_web / 'content.html'
        if content_path.is_file():
            app_contents.append(content_path.read_text(encoding='utf-8'))
        for css in sorted(app_web.glob('*.css')):
            app_styles.append(f'<link rel="stylesheet" href="/apps/{app_id}/web/{css.name}">')
        js_files = sorted(app_web.glob('*.js'), key=lambda p: (0 if 'workbench' in p.name else 1, p.name))
        for js in js_files:
            app_scripts.append(f'<script src="/apps/{app_id}/web/{js.name}" defer></script>')

    output = raw_html.replace('__API_KEY__', API_KEY)
    output = output.replace('<!-- APP_STYLES -->', '\n  '.join(app_styles))
    output = output.replace('<!-- APP_SCRIPTS -->', '\n  '.join(app_scripts))
    output = output.replace('<!-- APP_CONTENTS -->', '\n\n  '.join(app_contents))
    return output


@app.get('/', response_class=HTMLResponse)
def index():
    return render_studio_html()


@app.get('/api/status')
def status():
    controller.recover_history()
    with controller.lock:
        ordered = sorted((j for j in controller.jobs.values() if not j.get('history_hidden')),
                         key=lambda j: j['created'], reverse=True)
        saved = [j for j in ordered if j.get('file') or j.get('drive')]
        active_or_queued = [j for j in ordered if j['status'] in ('running', 'queued', 'cancelling')]
        recent_ids = {j['id'] for j in active_or_queued + ordered[:20] + saved[:20]}
        recent_batches = {j['batch_id'] for j in ordered if j['id'] in recent_ids and j.get('batch_id')}
        queue = controller.queue_snapshot()
        queue_lookup = {j['id']: j for j in queue}
        occupying_app_id, occupying_app_name = controller.occupying_app_info(applications)
        return {'authorized': (PRIVATE / 'google-token.dat').exists(), 'active': controller.active,
                'compute': {'provider_id': controller.provider_id, 'gpu': (controller.session or {}).get('accelerator'),
                            'occupying_app_id': occupying_app_id, 'occupying_app_name': occupying_app_name},
                'occupying_app_id': occupying_app_id, 'occupying_app_name': occupying_app_name,
                'consumer': dict(controller.consumer), 'queue': queue,
                'gpu_online_available': True,
                'drive_cache': drive_cache.status(),
                'managed_gpu': bool(controller.session), 'release': controller.release_status(),
                'history': {'count': len(saved), 'clearable': sum(controller.history_clearable(j) for j in saved)},
                'jobs': [controller.public_job(j, queue_lookup.get(j['id'], {})) for j in ordered if j['id'] in recent_ids or j.get('batch_id') in recent_batches]}

def asset_scopes():
    scopes = [{'app_id': 'shared', 'app_name': '共享素材', 'purpose': 'imports',
               'purpose_name': '导入素材', 'kinds': ('images', 'videos', 'audios')}]
    for application in applications.list():
        for purpose in application['asset_purposes']:
            scopes.append({'app_id': application['id'], 'app_name': application['name'],
                           'purpose': purpose['id'], 'purpose_name': purpose['name'], 'kinds': purpose['kinds']})
    return scopes


def validate_asset_scope(app_id, purpose, kind):
    scope = next((item for item in asset_scopes() if item['app_id'] == app_id and item['purpose'] == purpose), None)
    if not scope or kind not in scope['kinds']:
        raise HTTPException(400, '所选应用用途不支持该素材类型')


@app.get('/api/workspace')
def workspace():
    return {'applications': applications.list(), 'providers': providers.list(),
            'compute': compute_status(),
            'library': {'root': str(asset_library.root), 'scopes': asset_scopes()}}


@app.get('/api/compute')
def compute_status():
    session = controller.session or {}
    occupying_app_id, occupying_app_name = controller.occupying_app_info(applications)
    return {'provider_id': controller.provider_id, 'provider_name': providers.get(controller.provider_id).name,
            'authorized': (PRIVATE / 'google-token.dat').exists(),
            'connected': bool(session), 'busy': bool(controller.active), 'consumer': dict(controller.consumer),
            'gpu': session.get('accelerator'), 'release': controller.release_status(),
            'occupying_app_id': occupying_app_id, 'occupying_app_name': occupying_app_name}


@app.get('/api/consumer')
def consumer_status():
    return controller.refresh_consumer()


@app.get('/api/assets')
def list_assets(kind: str | None = None, app_id: str | None = None, purpose: str | None = None):
    try:
        items = asset_library.list(kind, list(controller.jobs.values()), app_id=app_id, purpose=purpose)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {'assets': items, 'root': str(asset_library.root), 'scopes': asset_scopes()}


@app.post('/api/assets', status_code=201)
async def import_asset(file: UploadFile, app_id: Annotated[str, Form()] = 'shared', purpose: Annotated[str, Form()] = 'imports'):
    extension = Path(file.filename or '').suffix.lower()[1:]
    if extension in ('png', 'jpg', 'jpeg', 'webp'):
        result = await save_image(file, app_id, purpose)
        return {'id': result['image_id'], 'kind': 'images', 'app_id': app_id, 'purpose': purpose}
    if extension not in ('mp4', 'wav', 'mp3', 'm4a', 'ogg', 'flac', 'aac'):
        raise HTTPException(400, '支持 PNG、JPEG、WebP 图片，MP4 视频和常见音频格式')
    library = asset_library
    asset_id = uuid.uuid4().hex + '.' + extension
    validate_asset_scope(app_id, purpose, library.kind(asset_id))
    target = library.target(asset_id, app_id, purpose)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.partial')
    size = 0
    try:
        with temporary.open('xb') as output:
            while chunk := await file.read(1024 ** 2):
                size += len(chunk)
                if size > 512 * 1024 ** 2:
                    raise HTTPException(400, '单项素材不能超过 512MB')
                output.write(chunk)
        import av
        try:
            with av.open(str(temporary)) as container:
                stream = next(iter(container.streams.video if extension == 'mp4' else container.streams.audio), None)
                if stream is None or (extension != 'mp4' and container.streams.video):
                    raise ValueError('素材类型与文件格式不符')
                next(container.decode(stream))
                duration = float(container.duration / av.time_base) if container.duration else 0
        except Exception:
            raise HTTPException(400, '素材无法解码，请使用有效的视频或音频文件') from None
        temporary.replace(target)
        library._write_metadata(target, {'name': Path(file.filename).name[:120], 'duration': duration})
        return {'id': asset_id, 'kind': library.kind(asset_id), 'app_id': app_id, 'purpose': purpose}
    finally:
        temporary.unlink(missing_ok=True)


@app.get('/api/assets/{asset_id}/file')
def asset_file(asset_id: str, request: Request, download: bool = False):
    library = asset_library
    try:
        kind = library.kind(asset_id)
        if kind == 'videos':
            return video(asset_id, request, download)
        path = library.resolve(asset_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, '素材不存在') from None
    if not path.is_file():
        raise HTTPException(404, '素材不存在')
    return FileResponse(path, media_type=library.media_type(asset_id), filename=asset_id if download else None)


@app.delete('/api/history')
def clear_history():
    return controller.clear_history()


class HistoryRecycleRequest(BaseModel):
    job_id: str | None = Field(default=None,min_length=1,max_length=128)
    app_id: str | None = Field(default=None,max_length=64)


@app.post('/api/history/recycle')
def recycle_history(payload: HistoryRecycleRequest):
    return controller.recycle_history(payload.job_id, app_id=payload.app_id)


@app.delete('/api/history/{job_id}')
def clear_history_item(job_id: str):
    return controller.clear_history(job_id)


@app.post('/api/history/{job_id}/open-folder')
def open_history_folder(job_id: str):
    return controller.open_history_folder(job_id)


@app.post('/api/history/{job_id}/load')
def load_history_video(job_id: str):
    return controller.load_history_video(job_id)


class HistoryRestore(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=10000)


@app.post('/api/history/restore')
def restore_history(payload: HistoryRestore):
    return controller.restore_history(payload.ids)


@app.get('/api/release-policy')
def release_policy():
    return controller.release_status()


@app.put('/api/release-policy')
def configure_release_policy(policy: ReleasePolicy):
    return controller.configure_release(policy)


@app.get('/api/account')
def account(refresh: bool = False):
    try:
        return account_cache.get(refresh=refresh)
    except Exception as exc:
        raise HTTPException(400, safe_error(exc)) from None


@app.get('/api/gpu/online')
def detect_online_gpus():
    try:
        return controller.transport.detect_online_gpus()
    except Exception as exc:
        raise HTTPException(400, safe_error(exc)) from None


class GpuReleaseConfig(BaseModel):
    endpoint: str = Field(min_length=1, max_length=256)


@app.post('/api/gpu/release')
def release_online_gpu(payload: GpuReleaseConfig):
    return controller.release_online_gpu(payload.endpoint)


@app.get('/api/settings')
def generation_settings():
    # UI and remote graph use the same dimension table.
    h3_app = applications.get('h3')
    settings = h3_app.settings() if hasattr(h3_app, 'settings') else {'dimensions': DIMENSIONS, 'fps': 24, 'alignment': 32}
    return {**settings,
            'result_storage': 'drive' if controller.drive_storage.enabled else 'local',
            'storage_ready': True, 'drive_folder': 'H3-Videos'}


@app.post('/api/authorize/start')
def authorize_start():
    global oauth_flow
    config = json.loads(resources.files('colab_cli').joinpath('oauth_config.json').read_text())
    oauth_flow = InstalledAppFlow.from_client_config(config, SCOPES)
    oauth_flow.redirect_uri = 'https://sdk.cloud.google.com/applicationdefaultauthcode.html'
    url, _ = oauth_flow.authorization_url(prompt='consent', token_usage='remote', access_type='offline')
    return {'url': url}


class AuthCode(BaseModel):
    code: str = Field(min_length=5, max_length=4096)


@app.post('/api/drive-cache/authorize/start')
def drive_authorize_start():
    try:
        return drive_cache.authorize_start()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post('/api/drive-cache/authorize/finish')
def drive_authorize_finish(payload: AuthCode):
    try:
        drive_cache.authorize_finish(payload.code)
        return drive_cache.status()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(400, 'Drive 授权未完成，请重新打开授权页面。') from None


class DriveCacheConfig(BaseModel):
    enabled: bool
    backend: Literal['api', 'native'] | None = None
    quota_project: str | None = None


@app.put('/api/drive-cache')
def configure_drive_cache(payload: DriveCacheConfig):
    try:
        if payload.quota_project is not None:
            drive_cache.configure(False, payload.backend, payload.quota_project)
        if payload.enabled and (payload.backend or drive_cache.backend) == 'api':
            drive_cache.quota()
        return drive_cache.configure(payload.enabled, payload.backend, payload.quota_project)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.get('/api/drive-cache')
def drive_cache_status():
    return drive_cache.status()


@app.post('/api/drive-cache/backup')
def trigger_drive_backup():
    if not (PRIVATE / 'google-token.dat').exists():
        raise HTTPException(400, '请先连接 Google 账号')
    if not drive_cache.token_path.exists() and drive_cache.backend != 'native':
        raise HTTPException(400, '请先完成 Drive 授权')
    if controller.active:
        raise HTTPException(400, '当前有正在生成的任务，请等待生成完成或取消后再执行备份')
    progress_file = PRIVATE / 'drive-cache-progress.json'
    try:
        if progress_file.exists():
            data = json.loads(progress_file.read_text())
            if data.get('running') and time.time() - data.get('updated_at', 0) < 600:
                raise HTTPException(400, '自动备份任务已在运行中')
    except (OSError, ValueError):
        pass

    import threading
    from apps.h3 import seed_drive_cache

    def run_worker():
        try:
            seed_drive_cache.run_backup(gpu_type='T4')
        except Exception:
            pass

    threading.Thread(target=run_worker, name='drive-backup-worker', daemon=True).start()
    return {'ok': True, 'message': '已启动自动备份任务，正在申请低成本 T4 显卡备份模型至 Drive，完成后自动释放。'}


@app.post('/api/drive-cache/native/confirm')
def confirm_native_drive():
    path = PRIVATE / 'drive-native-auth.json'
    try:
        state = json.loads(path.read_text())
        if not state.get('pending'):
            raise ValueError('当前没有等待确认的原生 Drive 授权')
        state['confirmed'] = True
        atomic(path, json.dumps(state))
        return {'ok': True}
    except (OSError, ValueError) as exc:
        raise HTTPException(400, '当前没有等待确认的原生 Drive 授权') from None


@app.post('/api/authorize/finish')
def authorize_finish(payload: AuthCode):
    global oauth_flow
    if oauth_flow is None:
        raise HTTPException(400, '请先打开 Google 授权链接')
    try:
        oauth_flow.fetch_token(code=payload.code.strip(), timeout=60)
        save_secret(PRIVATE / 'google-token.dat', oauth_flow.credentials.to_json().encode())
        oauth_flow = None
        return {'ok': True}
    except Exception:
        raise HTTPException(400, '授权未完成，授权码可能过期；请重新打开 Google 授权链接。') from None


JobConfig = Annotated[Union[H3JobConfig, QwenImageJobConfig], Field(discriminator='app_id'), Body(...)]


@app.post('/api/jobs', status_code=202)
def create_job(config: JobConfig, placement: Literal["back", "front"] = "back"):
    if not (PRIVATE / 'google-token.dat').exists():
        raise HTTPException(401, '请先授权 Google 账号')
    if not config.prompt.strip():
        raise HTTPException(400, '请输入提示词')
    app_id = getattr(config, 'app_id', 'h3')
    application = applications.get(app_id)
    if hasattr(application, 'validate_assets'):
        try:
            application.validate_assets(config, asset_library)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    else:
        for reference in getattr(config, 'references', []):
            if not asset_library.exists(reference.image_id):
                raise HTTPException(400, f'图片{reference.slot}已丢失，请重新上传')
        for audio in getattr(config, 'audio_references', []):
            try:
                actual = validate_audio_asset(asset_library, audio)
                if abs(actual.duration - audio.duration) > .001:
                    raise ValueError('声音样本时长不匹配，请重新上传')
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
    job_id = controller.start(config, placement=placement)
    batch_id = controller.jobs[job_id].get('batch_id')
    return {'id': job_id, 'ids': [j['id'] for j in controller.jobs.values() if j.get('batch_id') == batch_id]
            if batch_id else [job_id]}


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str):
    if job_id not in controller.jobs:
        raise HTTPException(404, '任务不存在')
    return controller.public_job(controller.jobs[job_id])


@app.get('/api/jobs/{job_id}/download-link')
def download_link(job_id: str):
    job = controller.jobs.get(job_id)
    if not job or not job.get('drive'):
        raise HTTPException(404, '该任务没有已确认的云端视频')
    try:
        stored = job['drive']
        return controller.drive_storage.download_link(stored)
    except Exception as exc:
        raise HTTPException(502, safe_error(exc)) from None


@app.post('/api/jobs/{job_id}/retry')
def retry(job_id: str):
    if not controller.session and not controller.jobs.get(job_id, {}).get('drive'):
        raise HTTPException(409, '原运行时已释放，无法取回临时文件；请重新生成。')
    return {'id': controller.start(None, retry=job_id)}


@app.post('/api/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    return controller.cancel(job_id)


@app.post('/api/jobs/{job_id}/resubmit')
def resubmit_job(job_id: str, placement: Literal["back", "front"] = "back"):
    return controller.resubmit(job_id, placement=placement)


class ReleaseConfig(BaseModel):
    discard_pending: bool = False
    force: bool = False


@app.post('/api/release')
def release(config: ReleaseConfig):
    return controller.check_and_release(discard_pending=config.discard_pending, force=config.force)


@app.post('/api/images')
async def upload_image(file: UploadFile, purpose: Annotated[str, Form()] = 'references', app_id: Annotated[str, Form()] = 'h3'):
    return await save_image(file, app_id, purpose)


async def save_image(file, app_id, purpose):
    validate_asset_scope(app_id, purpose, 'images')
    data = await file.read(20 * 1024**2 + 1)
    if len(data) > 20 * 1024**2:
        raise HTTPException(400, '图片不能超过 20MB')
    extension = 'png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'jpg' if data.startswith(b'\xff\xd8\xff') else 'webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else None
    if not extension:
        raise HTTPException(400, '仅支持 PNG、JPEG、WebP 图片')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 40_000_000 or min(image.size) < 32:
                raise ValueError('请使用至少 32px、最多 4000 万像素的图片')
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(400, '图片无法读取或尺寸过大，请使用有效的 PNG、JPEG、WebP 图片') from None
    image_id = uuid.uuid4().hex + '.' + extension
    asset_library.store(image_id, data, name=Path(file.filename or image_id).name[:120], app_id=app_id, purpose=purpose)
    return {'image_id': image_id}


@app.get('/api/characters')
def list_characters(include_deleted: bool = False):
    return {'characters': [c.model_dump() for c in character_library.list(include_deleted=include_deleted)]}


@app.get('/api/characters/{character_id}')
def get_character(character_id: str):
    try:
        return character_library.get(character_id, include_deleted=True).model_dump()
    except KeyError:
        raise HTTPException(404, '角色不存在') from None


@app.delete('/api/characters/{character_id}')
def delete_character(character_id: str):
    try:
        return {'ok': True, 'character': character_library.delete(character_id).model_dump()}
    except KeyError:
        raise HTTPException(404, '角色不存在') from None


@app.post('/api/characters/{character_id}/restore')
def restore_character(character_id: str):
    try:
        return character_library.restore(character_id).model_dump()
    except KeyError:
        raise HTTPException(404, '角色不存在') from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post('/api/characters')
def save_character(profile: CharacterProfile):
    try:
        return character_library.save(profile).model_dump()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post('/api/characters/preview')
def preview_characters(config: H3JobConfig):
    return {'characters': [c.name for c in config.characters], 'images': len(config.references),
            'audios': len(config.audio_references),
            'audio_seconds': sum(a.duration for a in config.audio_references),
            'requires_qwen': config.prompt_processing == 'faithful',
            'preview_kind': 'source' if config.prompt_processing == 'faithful' else 'compiled',
            'compiled_prompt': None if config.prompt_processing == 'faithful' else
                (reference_prompt(config.model_dump()) if config.references else config.prompt)}


@app.post('/api/audios')
async def upload_audio(file: UploadFile):
    data = await file.read(20 * 1024**2 + 1)
    if len(data) > 20 * 1024**2:
        raise HTTPException(400, '每段声音样本不能超过 20MB')
    extension = Path(file.filename or '').suffix.lower()[1:]
    if extension not in ('wav', 'mp3', 'm4a', 'ogg', 'flac', 'aac'):
        raise HTTPException(400, '支持 WAV、MP3、M4A、OGG、FLAC、AAC 音频')
    try:
        duration = inspect_audio(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    audio_id = uuid.uuid4().hex + '.' + extension
    asset_library.store(audio_id, data, name=Path(file.filename or '声音样本').name[:120], metadata={'duration': duration}, app_id='h3', purpose='characters')
    return AudioAsset(audio_id=audio_id, name=Path(file.filename or '声音样本').name[:120], duration=duration).model_dump()


@app.get('/api/audios/{audio_id}')
def audio_preview(audio_id: str):
    if not re.fullmatch(AUDIO_ID, audio_id):
        raise HTTPException(404, '声音样本不存在')
    try:
        path = asset_library.resolve(audio_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, '声音样本不存在') from None
    if not path.is_file():
        raise HTTPException(404, '声音样本不存在')
    return FileResponse(path, media_type={'wav':'audio/wav', 'mp3':'audio/mpeg',
        'm4a':'audio/mp4', 'ogg':'audio/ogg', 'flac':'audio/flac', 'aac':'audio/aac'}[Path(audio_id).suffix[1:]])


@app.get('/api/images/{image_id}')
def image_preview(image_id: str):
    if not re.fullmatch(r'[0-9a-f]{32}\.(png|jpg|jpeg|webp)', image_id, re.IGNORECASE):
        raise HTTPException(404, '图片不存在')
    try:
        path = asset_library.resolve(image_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, '图片不存在') from None
    if not path.is_file():
        raise HTTPException(404, '图片不存在')
    ext = path.suffix.lower().lstrip('.')
    media = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp'}.get(ext, 'application/octet-stream')
    return FileResponse(path, media_type=media)


@app.get('/video/{filename}')
def video(filename: str, request: Request, download: bool = False):
    if not re.fullmatch(r'[0-9a-f]{32}\.mp4', filename):
        raise HTTPException(404)
    byte_range = request.headers.get('range')
    if byte_range and (len(byte_range) > 128 or not re.fullmatch(r'bytes=\d*-\d*', byte_range)):
        raise HTTPException(416, '不支持该视频范围请求')
    job = controller.jobs.get(filename.removesuffix('.mp4'))
    if job:
        path = controller.history_recycle.video(job.get('file') or filename)
    else:
        try:
            path = asset_library.resolve(filename)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, '视频不存在') from None
    if job and job.get('drive'):
        target = controller.history_recycle.video(job.get('file') or filename)
        if not controller.valid_local_video(target, job['drive']) or not job.get('file'):
            result = controller.load_history_video(job['id'])
            target = controller.history_recycle.video(result['file'])
        path = target
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type='video/mp4', filename=filename if download else None)


@app.get('/image/{filename}')
def image_file(filename: str, download: bool = False):
    if not re.fullmatch(r'[0-9a-f]{32}\.(png|jpg|jpeg|webp)', filename, re.IGNORECASE):
        raise HTTPException(404, '图片不存在')
    try:
        path = asset_library.resolve(filename)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, '图片不存在') from None
    if not path.is_file():
        raise HTTPException(404, '图片不存在')
    ext = path.suffix.lower().lstrip('.')
    media = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp'}.get(ext, 'application/octet-stream')
    return FileResponse(path, media_type=media, filename=filename if download else None)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=PORT, access_log=False)

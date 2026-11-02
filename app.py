"""Local-only Windows controller using Google Colab CLI core transports."""
import base64
import ctypes
from contextlib import asynccontextmanager
from ctypes import wintypes
import hashlib
import io
from importlib import resources
import json
import math
import os
from pathlib import Path, PurePosixPath
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

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from typing import Literal
from PIL import Image, UnidentifiedImageError
from remote_h3 import DIMENSIONS, reference_prompt, uses_reference_model
from characters import CharacterLibrary, CharacterProfile, VoiceReference, AudioAsset, AUDIO_ID, expand_characters, inspect_audio, validate_audio_asset
from drive_cache import DriveCache
from drive_storage import DriveStorage
from history_recycle import HistoryRecycle
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import InstalledAppFlow
from colab_cli.client import Client, Prod, Variant, Accelerator, Shape
from colab_cli.runtime import ColabRuntime
from colab_cli.state import SessionState
from colab_cli.contents import ContentsClient
from runtime_recovery import observe_execution, connection_failure, TaskCancelled, ReconnectExhausted, enable_cancellation

BASE = Path(__file__).resolve().parent
PRIVATE = BASE / '.private'
VIDEOS = BASE / 'videos'
LOG = BASE / 'log'
PRIVATE.mkdir(exist_ok=True)
VIDEOS.mkdir(exist_ok=True)
LOG.mkdir(exist_ok=True)
SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/colaboratory']
LOCAL_ORIGINS = {'http://127.0.0.1:7865', 'http://localhost:7865'}
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
        ok = dll.CryptProtectData(ctypes.byref(source), 'H3 Colab local', None, None, None, 1, ctypes.byref(result))
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


drive_cache = DriveCache(PRIVATE, load_secret, save_secret)
character_library = CharacterLibrary(PRIVATE)


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


class ReferenceImage(BaseModel):
    slot: int = Field(ge=1, le=9)
    image_id: str = Field(pattern=r'^[0-9a-f]{32}\.(png|jpg|webp)$')
    role: Literal['character', 'scene', 'clothing', 'style', 'product', 'other'] = 'other'
    description: str = Field(default='', max_length=500)
    frame: Literal['reference', 'first', 'last'] = 'reference'
    character_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')


class ReleasePolicy(BaseModel):
    mode: Literal['always', 'idle', 'manual'] = 'idle'
    idle_minutes: int = Field(default=10, ge=1, le=120)


class JobConfig(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    prompt_processing: Literal['faithful', 'direct'] = 'faithful'
    performance: bool = True
    encoder: Literal['heretic_int8', 'official_int8', 'official_bf16'] = 'heretic_int8'
    quantized_model: bool = True
    quantized_vae: bool = True
    veda: bool = True
    title: str = Field(default='', max_length=80)
    aspect_ratio: Literal['16:9', '9:16', '1:1', '4:5', '3:4'] = '16:9'
    mode: Literal['pdd', 'base'] = 'pdd'
    resolution: Literal['480p', '768p'] = '768p'
    duration: Literal[5, 10, 12, 15] = 5
    count: int = Field(default=1, ge=1, le=8, strict=True)
    seed: int = Field(default=20261009, ge=0, le=2**53 - 1)
    people: bool = True
    strength: float = Field(default=.7, ge=0, le=1)
    auto_release: bool = False
    release_mode: Literal['always', 'idle', 'manual'] | None = None
    idle_minutes: int = Field(default=10, ge=1, le=120)
    gpu: Literal['A100', 'G4'] = 'A100'
    endpoint: str | None = None
    image_id: str | None = None
    generation_type: Literal['text', 'image', 'reference'] | None = None
    references: list[ReferenceImage] = Field(default_factory=list, max_length=9)
    reference_size: Literal['match', 'max'] = 'match'
    characters: list[CharacterProfile] = Field(default_factory=list, max_length=9)
    audio_references: list[VoiceReference] = Field(default_factory=list, max_length=3)

    @model_validator(mode='before')
    @classmethod
    def resolve_mentions(cls, data):
        if isinstance(data, dict) and '@' in str(data.get('prompt', '')):
            return expand_characters(data, character_library.list())
        return data

    @model_validator(mode='after')
    def validate_references(self):
        if not self.performance:
            self.encoder = 'official_bf16'
            self.quantized_model = self.quantized_vae = self.veda = self.people = False
            self.mode = 'base'
        if self.release_mode is not None:
            self.auto_release = self.release_mode == 'always'
        elif 'auto_release' in self.model_fields_set:
            self.release_mode = 'always' if self.auto_release else 'manual'
        else:
            self.release_mode = 'idle'
            self.auto_release = False
        if self.characters and (not self.references or len({c.id for c in self.characters}) != len(self.characters)):
            raise ValueError('角色配置需要有效参考图，且角色不能重复')
        if self.audio_references:
            if not self.references:
                raise ValueError('声音参考需要搭配角色参考图')
            if sum(a.duration for a in self.audio_references) > 15.0001:
                raise ValueError('声音参考总长不能超过 15 秒')
            if any(a.character_id not in {c.id for c in self.characters} for a in self.audio_references):
                raise ValueError('声音参考未绑定有效角色')
        inferred = 'image' if self.references or self.image_id else 'text'
        # Accept saved/API requests using the previous reference mode name.
        selected = 'image' if self.generation_type == 'reference' and self.references else self.generation_type
        if selected and selected != inferred:
            raise ValueError('生成方式与上传的图片不匹配；图生视频至少需要一张图片')
        if self.references:
            if self.image_id:
                raise ValueError('参考图与首帧图不能同时使用')
            if len({r.slot for r in self.references}) != len(self.references):
                raise ValueError('参考图编号不能重复')
            self.references.sort(key=lambda r: r.slot)
            for frame, label in [('first', '首帧'), ('last', '尾帧')]:
                if sum(r.frame == frame for r in self.references) > 1:
                    raise ValueError(label + '只能指定一张图片')
            input_images = {r.image_id for r in self.references}
            if any(not set(ch.image_ids).issubset(input_images) for ch in self.characters):
                raise ValueError('角色参考图未加入本次输入')
            reference_prompt(self.model_dump())
            # Ref2VA selects its own PDD adapter; keep the user's sampling mode.
            if uses_reference_model(self.model_dump()):
                self.people = False
        self.generation_type = inferred
        return self


class GoogleTransport:
    def __init__(self, controller):
        self.controller = controller
        self.running_runtime = None
        self.assignment_hash = uuid.uuid4()

    def cancel_pending_assignment(self, job):
        if not job.get('assignment_pending'):
            return
        config = JobConfig(**job['config'])
        client = self.client()
        found = client._get_assignment(uuid.UUID(job['assignment_hash']),variant=Variant.GPU,
            accelerator=Accelerator(config.gpu),shape=None if config.gpu=='G4' else Shape.HIGH_RAM)
        endpoint = getattr(found,'endpoint',None)
        if endpoint:
            self._unassign_confirmed(client,endpoint)
        job['assignment_pending'] = False

    def stop_running(self):
        if self.running_runtime is not None:
            self.running_runtime.stop()

    def client(self):
        path = PRIVATE / 'google-token.dat'
        if not path.exists():
            raise RuntimeError('请先完成 Google 授权')
        creds = Credentials.from_authorized_user_info(json.loads(load_secret(path)), scopes=SCOPES)
        return Client(Prod(), TimedSession(creds))

    def account(self):
        client = self.client()
        assignments = client.list_assignments()
        managed = (self.controller.session or {}).get('endpoint')
        if managed and not any(a.endpoint == managed for a in assignments):
            self.controller.session = None
            if hasattr(self.controller, 'save_session'):
                self.controller.save_session()
            if hasattr(self.controller, 'on_runtime_recycled'):
                self.controller.on_runtime_recycled(managed)
        return {'runtimes': [{'endpoint': a.endpoint, 'gpu': a.accelerator.value} for a in assignments],
                'consumption': client.get_consumption_user_info().model_dump(mode='json')}

    def detect_online_gpus(self):
        managed = (self.controller.session or {}).get('endpoint')
        assignments = [a for a in self.client().list_assignments() if a.accelerator in GPU_ACCELERATORS]
        if managed and not any(a.endpoint == managed for a in assignments):
            self.controller.session = None
            if hasattr(self.controller, 'save_session'):
                self.controller.save_session()
            if hasattr(self.controller, 'on_runtime_recycled'):
                self.controller.on_runtime_recycled(managed)
        return {'runtimes': [dict(endpoint=a.endpoint, gpu=a.accelerator.value,
                                  managed=a.endpoint == (self.controller.session or {}).get('endpoint'))
                             for a in assignments],
                'updated_at': time.time()}

    def connection_step(self, phase, operation):
        active = getattr(self.controller, 'active', None)
        job = self.controller.jobs.get(active) if isinstance(active, str) else None
        started, finished = time.monotonic(), threading.Event()
        if job:
            self.controller.event(job, 'connect', phase)
            def waiting():
                while not finished.wait(CONNECT_PROGRESS_INTERVAL):
                    if job.get('cancel_requested'):
                        return
                    self.controller.event(job, 'connect', phase + '，已等待 %d 秒' % (time.monotonic() - started))
            threading.Thread(target=waiting, daemon=True).start()
        try:
            result = operation()
        except Exception as exc:
            exc.h3_phase = phase
            if not getattr(exc, 'h3_request', None):
                exc.h3_request = {'method': '', 'operation': '请求',
                                  'seconds': round(time.monotonic() - started, 1)}
            raise
        finally:
            finished.set()
        if job:
            self.controller.event(job, 'connect', phase + '完成，用时 %.1f 秒' % (time.monotonic() - started))
        return result

    def connect(self, config):
        client = self.client()
        previous = self.controller.session
        if previous:
            if config.endpoint and config.endpoint != previous['endpoint']:
                raise RuntimeError('请先释放本控制台正在管理的运行时，再选择其他显卡。')
            if previous.get('accelerator', 'A100') != config.gpu:
                raise RuntimeError('请先释放当前运行时，再切换显卡型号。')
            return self.connection_step('检查已有 Colab 运行时', self.refresh)
        accelerator = Accelerator(config.gpu)
        # G4 has one fixed memory profile: Colab rejects shape=hm for G4.
        shape = None if config.gpu == 'G4' else Shape.HIGH_RAM
        assignments = self.connection_step('查询 Colab 运行时列表（含授权检查）', client.list_assignments)
        active_id = getattr(self.controller,'active',None)
        owned_job = self.controller.jobs.get(active_id) if isinstance(active_id,str) else None
        notebook_hash = uuid.UUID(owned_job['id']) if owned_job else self.assignment_hash
        if owned_job:
            owned_job.update(assignment_hash=str(notebook_hash),assignment_pending=True)
            self.controller.save_jobs()
        if config.endpoint:
            match = next((a for a in assignments if a.endpoint == config.endpoint and a.accelerator == accelerator), None)
            if match is None:
                released_by_this_app = any(j.get('endpoint') == config.endpoint and j['status'] == 'done' and j['config'].get('auto_release') for j in self.controller.jobs.values())
                if not released_by_this_app:
                    raise RuntimeError('选中的 ' + config.gpu + ' 运行时已离线或型号不匹配，请刷新列表。')
                assignment = self.connection_step('申请并启动 ' + config.gpu + ' 显卡',
                    lambda: client.assign(notebook_hash, variant=Variant.GPU, accelerator=accelerator, shape=shape))
            else:
                assignment = match
        else:
            assignment = self.connection_step('申请并启动 ' + config.gpu + ' 显卡',
                lambda: client.assign(notebook_hash, variant=Variant.GPU, accelerator=accelerator, shape=shape))
        proxy = assignment.runtime_proxy_info
        self.controller.session = dict(endpoint=assignment.endpoint, url=proxy.url, token=proxy.token,
            name='h3-local', variant='GPU', accelerator=config.gpu, machine_shape='HIGH_RAM',
            kernel_id=None, session_id=None)
        self.controller.save_session()
        if owned_job:
            owned_job['assignment_pending'] = False
        return SessionState(**self.controller.session)

    def refresh(self):
        endpoint = self.controller.session['endpoint']
        match = next((a for a in self.client().list_assignments() if a.endpoint == endpoint), None)
        if match is None:
            self.controller.session = None
            if hasattr(self.controller, 'save_session'):
                self.controller.save_session()
            if hasattr(self.controller, 'on_runtime_recycled'):
                self.controller.on_runtime_recycled(endpoint)
            raise RuntimeError('Colab 运行时已被回收，远程临时文件可能已丢失。')
        self.controller.session.update(url=match.runtime_proxy_info.url, token=match.runtime_proxy_info.token)
        self.controller.save_session()
        return SessionState(**self.controller.session)

    def execute(self, job):
        s = self.refresh()
        runtime = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id, session_id=s.session_id)
        def remember(field, value):
            self.controller.session[field] = value
            self.controller.save_session()
        runtime.on_kernel_started = lambda value: remember('kernel_id', value)
        runtime.on_session_started = lambda value: remember('session_id', value)
        source = (BASE / 'remote_h3.py').read_text(encoding='utf-8')
        if job['config'].get('prompt_processing') == 'faithful':
            import base64
            bundle = {name: base64.b64encode((BASE / name).read_bytes()).decode()
                      for name in ('h3_prompt.py', 'h3_prompt_worker.py')}
            source = ('import base64, pathlib, sys\n_prompt_tools = pathlib.Path("/content/h3-prompt-tools")\n'
                      '_prompt_tools.mkdir(exist_ok=True)\n'
                      'for _name, _data in ' + repr(bundle) + '.items():\n'
                      '    (_prompt_tools / _name).write_bytes(base64.b64decode(_data))\n'
                      'sys.path.insert(0, str(_prompt_tools))\nsys.modules.pop("h3_prompt", None)\n' + source)
        if drive_cache.enabled:
            import base64
            cache_source = (BASE / 'drive_cache_remote.py').read_bytes()
            source += '\nimport types, base64\n_drive_module = types.ModuleType("drive_cache_remote")\n'
            source += 'exec(base64.b64decode(' + repr(base64.b64encode(cache_source).decode()) + '), _drive_module.__dict__)\nsys.modules["drive_cache_remote"] = _drive_module\n'
            if drive_cache.backend == 'native':
                from native_drive_mount import install_mount_hook
                install_mount_hook(runtime, self.client(), s.endpoint,
                    lambda stage, message: self.controller.event(job, stage, message), PRIVATE)
                mounted_source = (BASE / 'mounted_drive_cache.py').read_bytes()
                source += '_mounted = types.ModuleType("mounted_drive_cache")\nexec(base64.b64decode(' + repr(base64.b64encode(mounted_source).decode()) + '), _mounted.__dict__)\n'
                source += 'from google.colab import drive\ndrive.mount("/content/drive", timeout_ms=900000)\nMODEL_CACHE = _mounted.MountedDriveModelCache(event)\n'
            else:
                source += 'MODEL_CACHE = _drive_module.DriveModelCache(' + repr(drive_cache.remote_token()) + ', event, quota_project=' + repr(drive_cache.quota_project) + ', token_provider=lambda: input("H3_DRIVE_ACCESS_TOKEN"))\n'
        cfg = dict(job['config'], job_id=job['id'])
        image_names = ([cfg['image_id']] if cfg.get('image_id') else []) + [r['image_id'] for r in cfg.get('references', [])]
        asset_names = image_names + [a['audio_id'] for a in cfg.get('audio_references', [])]
        for name in dict.fromkeys(asset_names):
            image = PRIVATE / name
            # /content already exists; upload here before ComfyUI prepares its input folder.
            TimedContents(s).upload(str(image), 'content/' + image.name)
        if cfg.get('image_id'):
            cfg['image_name'] = cfg['image_id']
        if asset_names:
            source += '\n_orig_prepare = prepare\ndef prepare():\n    _orig_prepare()\n    import shutil\n    (ROOT / "input").mkdir(exist_ok=True)\n    for name in ' + repr(list(dict.fromkeys(asset_names))) + ':\n        shutil.copy2("/content/" + name, ROOT / "input" / name)\n'
        code = source + '\nrun(json.loads(' + repr(json.dumps(cfg)) + '))\n'
        lines_seen = set()
        def output_hook(output):
            self.controller.check_cancelled(job)
            for line in str(output.get('text', '')).splitlines():
                if line.startswith('H3_EVENT ') and line not in lines_seen:
                    try:
                        payload = json.loads(line[9:])
                    except ValueError:
                        continue
                    lines_seen.add(line)
                    if payload.get('gpu_info'):
                        job['gpu_info'] = payload['gpu_info']
                    if payload.get('timings'):
                        job.setdefault('timings', {}).update(payload['timings'])
                    if payload.get('acceleration'):
                        job['acceleration'] = payload['acceleration']
                    if payload.get('prompt_plan'):
                        job['prompt_plan'] = payload['prompt_plan']
                    if payload.get('prompt_diagnostics'):
                        job['prompt_diagnostics'] = payload['prompt_diagnostics']
                    if payload.get('model_load'):
                        job.setdefault('model_loads', []).append({**payload['model_load'], 'message': payload['message']})
                    if payload.get('model_load_summary'):
                        job['model_load_summary'] = payload['model_load_summary']
                    if payload.get('cache_metric'):
                        job.setdefault('model_transfers', []).append(payload['cache_metric'])
                    if payload['stage'] == 'result':
                        job['remote_result'] = payload['result']
                    if payload['stage'] == 'failure':
                        job['safe_release'] = payload.get('safe_release', False)
                    self.controller.event(job, payload['stage'], payload['message'])
        try:
            self.running_runtime = runtime
            enable_cancellation(runtime, lambda: self.controller.check_cancelled(job))
            if s.kernel_id and s.session_id:
                import requests
                response = requests.get(s.url.rstrip('/') + '/api/kernels',
                    params={'authuser':'0','colab-runtime-proxy-token':s.token},
                    headers={'X-Colab-Client-Agent':'colab-cli','X-Colab-Runtime-Proxy-Token':s.token},
                    timeout=(10,20))
                response.raise_for_status()
                kernels = response.json()
                if not isinstance(kernels, list):
                    raise RuntimeError('无法确认原内核状态，运行时保留')
                if any(k.get('id') == s.kernel_id and k.get('execution_state') == 'busy' for k in kernels):
                    self.controller.event(job, 'recover', '远端原任务仍在执行，已重新连接进度；不重复提交')
                    observed = observe_execution(runtime, s.session_id, output_hook)
                    errors = [o for o in observed if o.get('output_type') == 'error']
                    if errors:
                        raise RuntimeError(errors[-1].get('evalue','远端原任务执行失败'))
                    if job.get('remote_result'):
                        return
                    self.controller.event(job, 'recover', '原内核已空闲，检查并恢复本任务已生成的结果')
            outputs = runtime.execute_code(code, allow_stdin=drive_cache.enabled,
                stdin_hook=drive_cache.runtime_stdin_hook(runtime) if drive_cache.enabled and drive_cache.backend == 'api' else None,
                output_hook=output_hook, timeout=4500)
            for output in outputs:
                output_hook(output)
            errors = [o for o in outputs if o.get('output_type') == 'error']
            if errors:
                raise RuntimeError(errors[-1].get('evalue', 'Colab 执行失败'))
            if not job.get('remote_result'):
                raise RuntimeError('远程执行结束，但未收到视频结果；运行时保留以便检查。')
        finally:
            runtime.stop()
            if self.running_runtime is runtime:
                self.running_runtime = None

    def download(self, result, target):
        path = PurePosixPath(result['path'])
        if not path.is_relative_to('/content/ComfyUI/output') or '..' in path.parts:
            raise RuntimeError('远程输出路径不合法')
        TimedContents(self.refresh()).download(str(path).lstrip('/'), str(target))

    def remote_json(self, code, marker, timeout=180):
        s = self.refresh()
        runtime = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id, session_id=s.session_id)
        def remember(field, value):
            self.controller.session[field] = value
            self.controller.save_session()
        runtime.on_kernel_started = lambda value: remember('kernel_id', value)
        runtime.on_session_started = lambda value: remember('session_id', value)
        try:
            self.running_runtime = runtime
            active_job = self.controller.jobs.get(self.controller.active)
            if active_job:
                enable_cancellation(runtime, lambda: self.controller.check_cancelled(active_job))
            outputs = runtime.execute_code(code, timeout=timeout)
            for output in outputs:
                for line in str(output.get('text', '')).splitlines():
                    if line.startswith(marker + ' '):
                        data = json.loads(line[len(marker) + 1:])
                        if data.get('error'):
                            raise RuntimeError(data['error'])
                        return data
            raise RuntimeError('Colab 未返回上传确认，远端结果保留')
        finally:
            runtime.stop()
            if self.running_runtime is runtime:
                self.running_runtime = None

    def upload_drive(self, job):
        storage = self.controller.drive_storage
        result = job['remote_result']
        path = PurePosixPath(result['path'])
        if not path.is_relative_to('/content/ComfyUI/output') or '..' in path.parts:
            raise RuntimeError('远程输出路径不合法')
        code = '''import json, hashlib
from pathlib import Path
try:
    p = Path(PATH)
    with p.open('rb') as f:
        sha = hashlib.file_digest(f, 'sha256').hexdigest()
    with p.open('rb') as f:
        md5 = hashlib.file_digest(f, 'md5').digest()
    value = {'size':p.stat().st_size, 'sha256':sha, 'md5':md5.hex()}
except Exception as exc:
    value = {'error':'读取远端视频失败：' + type(exc).__name__}
print('H3_DRIVE_PROBE ' + json.dumps(value))
'''.replace('PATH', repr(str(path)))
        probe = self.remote_json(code, 'H3_DRIVE_PROBE', timeout=60)
        if probe['size'] != result['size'] or probe['sha256'] != result['sha256']:
            raise RuntimeError('远端视频校验失败，停止上传')
        job['drive_upload_id'] = storage.ensure_file(job)
        self.controller.save_jobs()
        target = storage.upload_target(job)
        code = '''import json, requests
try:
    with open(PATH, 'rb') as f:
        response = requests.put(URL, data=f, headers=HEADERS, timeout=(15,120))
    value = {'ok':response.status_code in (200,201,204), 'status':response.status_code}
    if not value['ok']:
        value['error'] = 'Drive 上传失败（HTTP %s）' % response.status_code
except Exception as exc:
    value = {'error':'Drive 上传未确认：' + type(exc).__name__}
print('H3_DRIVE_UPLOAD ' + json.dumps(value))
'''.replace('PATH', repr(str(path))).replace('URL', repr(target['url'])).replace('HEADERS', repr(target['headers']))
        confirmation = self.remote_json(code, 'H3_DRIVE_UPLOAD', timeout=180)
        if confirmation.get('ok') is not True:
            raise RuntimeError('Drive 上传未确认，Colab 视频保留')
        return storage.verify(job, target, probe['md5'])

    def _unassign_confirmed(self, client, endpoint):
        if any(a.endpoint == endpoint for a in client.list_assignments()):
            client.unassign(endpoint)
        for _ in range(6):
            if not any(a.endpoint == endpoint for a in client.list_assignments()):
                return
            time.sleep(2)
        raise RuntimeError('尚未确认显卡已释放，请稍后重试或在 Colab 删除运行时。')

    def release(self):
        self._unassign_confirmed(self.client(), self.controller.session['endpoint'])
        self.controller.session = None
        self.controller.save_session()

    def release_detected_gpu(self, endpoint):
        client = self.client()
        assignment = next((a for a in client.list_assignments() if a.endpoint == endpoint), None)
        if assignment is None:
            return False
        if assignment.accelerator not in GPU_ACCELERATORS:
            raise HTTPException(409, '选中的运行时不是显卡，未执行释放')
        if not self.check_idle(endpoint):
            raise HTTPException(409, '该显卡仍有任务运行或排队，请完成后再释放')
        self._unassign_confirmed(client, endpoint)
        return True

    def check_idle(self, endpoint=None):
        """Check a listed runtime without allocating a VM or interrupting a kernel."""
        import requests
        endpoint = endpoint or self.controller.session['endpoint']
        managed = (self.controller.session or {}).get('endpoint') == endpoint
        assignment = next((a for a in self.client().list_assignments() if a.endpoint == endpoint), None)
        if assignment is None:
            if managed:
                self.controller.session = None
                if hasattr(self.controller, 'save_session'):
                    self.controller.save_session()
                if hasattr(self.controller, 'on_runtime_recycled'):
                    self.controller.on_runtime_recycled(endpoint)
            return True
        if managed:
            self.controller.session.update(url=assignment.runtime_proxy_info.url, token=assignment.runtime_proxy_info.token)
            self.controller.save_session()
            s = SessionState(**self.controller.session)
        else:
            s = SessionState(endpoint=endpoint, url=assignment.runtime_proxy_info.url,
                             token=assignment.runtime_proxy_info.token, name='h3-gpu-check',
                             variant='GPU', accelerator=assignment.accelerator.value,
                             machine_shape='HIGH_RAM', kernel_id=None, session_id=None)
        response = requests.get(s.url.rstrip('/') + '/api/kernels',
            params={'authuser': '0', 'colab-runtime-proxy-token': s.token},
            headers={'X-Colab-Client-Agent': 'colab-cli', 'X-Colab-Runtime-Proxy-Token': s.token}, timeout=(10, 20))
        response.raise_for_status()
        kernels = response.json()
        if not isinstance(kernels, list) or any(k.get('execution_state') != 'idle' for k in kernels):
            return False
        kernel_id = s.kernel_id if any(k.get('id') == s.kernel_id for k in kernels) else None
        runtime = ColabRuntime(s.url, s.token, kernel_id=kernel_id, session_id=s.session_id)
        def remember(field, value):
            if managed:
                self.controller.session[field] = value
                self.controller.save_session()
        runtime.on_kernel_started = lambda value: remember('kernel_id', value)
        runtime.on_session_started = lambda value: remember('session_id', value)
        code = '''import json, socket, requests, errno
with socket.socket() as probe:
    probe.settimeout(3)
    connection_status = probe.connect_ex(('127.0.0.1', 8188))
    if connection_status not in (0, errno.ECONNREFUSED):
        raise RuntimeError('无法确认生成服务状态')
    listening = connection_status == 0
idle = True
if listening:
    response = requests.get('http://127.0.0.1:8188/queue', timeout=5)
    response.raise_for_status()
    queue = response.json()
    if not all(isinstance(queue.get(k), list) for k in ('queue_running', 'queue_pending')):
        raise RuntimeError('生成队列返回格式无法确认')
    idle = not (queue['queue_running'] or queue['queue_pending'])
print('H3_IDLE_CHECK ' + json.dumps({'idle': idle}))
'''
        try:
            outputs = runtime.execute_code(code, timeout=20)
            for output in outputs:
                for line in str(output.get('text', '')).splitlines():
                    if line.startswith('H3_IDLE_CHECK '):
                        return json.loads(line[14:])['idle'] is True
            raise RuntimeError('无法确认 Colab 生成队列，保留显卡，请稍后检查。')
        finally:
            runtime.stop()


# Reference from the A100 resource panel observed during setup, not a fixed
# Google tariff. Replace with this account's unambiguous live A100 rate.
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
                    rate = single_a100_rate(self.data)
                    if rate is not None:
                        self.data.update(a100_rate_hourly=rate, a100_rate_updated_at=self.data['updated_at'])
                    consumption = self.data['consumption']
                    g4_rate = consumption.get('consumption_rate_hourly')
                    if (len(self.data['runtimes']) == 1 and self.data['runtimes'][0].get('gpu') == 'G4'
                            and consumption.get('assignments_count') == 1 and valid_number(g4_rate) and g4_rate > 0):
                        self.data.update(g4_rate_hourly=g4_rate, g4_rate_updated_at=self.data['updated_at'])
                    self.error = None
                    atomic(self.path, json.dumps(self.data))
                except Exception as exc:
                    self.error = safe_error(exc)
            if self.data is None:
                raise RuntimeError(self.error or '账户余额暂时不可用，请稍后刷新。')
            result = {**self.data, 'stale': bool(self.error) or now - self.data['updated_at'] > 90, 'error': self.error}
            return {**result, 'a100_estimate': a100_estimate(result),
                    'gpu_estimates': {'A100': a100_estimate(result), 'G4': g4_estimate(result)}}


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
        self.history_recycle = HistoryRecycle(directory / 'recovered-works',videos,self.drive_storage)
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
        policy_path = directory / 'release-state.json'
        if policy_path.exists():
            try:
                state = json.loads(policy_path.read_text(encoding='utf-8'))
                self.release_policy = ReleasePolicy(**state['policy'])
                self.release_revision = int(state.get('revision', 0))
                self.idle_since = state.get('idle_since') if valid_number(state.get('idle_since')) else None
            except (ValueError, KeyError, OSError, TypeError):
                pass
        if (directory / 'jobs.json').exists():
            self.jobs = json.loads((directory / 'jobs.json').read_text(encoding='utf-8'))
            for job in self.jobs.values():
                if job['status'] in ('running', 'queued', 'cancelling'):
                    job.update(status='interrupted', message='本地程序中断；运行时保留，可重新取回结果。')
            for batch_id in {j.get('batch_id') for j in self.jobs.values()} - {None}:
                siblings = [j for j in self.jobs.values() if j.get('batch_id') == batch_id]
                if all(j.get('file') for j in siblings):
                    for child in siblings:
                        child['batch_complete'] = True
                        if child['status'] == 'interrupted':
                            child['status'] = 'done'
        if (directory / 'session.dat').exists():
            self.session = json.loads(load_secret(directory / 'session.dat'))
        self.transport = transport or GoogleTransport(self)
        if self.session and self.idle_since is None:
            self.idle_since = self.now()

    def save_release_state(self):
        atomic(self.directory / 'release-state.json', json.dumps({
            'policy': self.release_policy.model_dump(), 'revision': self.release_revision, 'idle_since': self.idle_since}))

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
                    if self.session and not self.active and self.idle_since is not None and self.release_policy.mode == 'idle' else None)
        return {'policy': self.release_policy.model_dump(), 'revision': self.release_revision,
                'deadline': deadline, 'checking': self.release_checking,
                'error': self.release_error, 'message': self.release_message,
                'monitor_running': bool(self.monitor_thread and self.monitor_thread.is_alive())}

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
                        self.event(target, 'cancelled', '排队已取消')
                self.save_jobs()
                return {'ok': True, 'pending': False, 'message': '排队已取消'}
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
            self.event(job,'cancel','正在取消任务并释放显卡；未保存的临时结果将丢弃')
        if active:
            stop = getattr(self.transport,'stop_running',None)
            if stop:
                stop()
        else:
            self.finish_cancel(job)
        return {'ok':True,'pending':bool(self.active),'message':job.get('message')}

    def finish_cancel(self, job):
        with self.lock:
            if job.get('cancel_finishing'):
                return
            job['cancel_finishing'] = True
        try:
            if self.session:
                if job.get('endpoint') and job['endpoint'] != self.session['endpoint']:
                    raise RuntimeError('任务与当前运行时不一致，无法确认取消')
                self.transport.release()
            elif job.get('assignment_pending'):
                self.transport.cancel_pending_assignment(job)
            with self.lock:
                for target in self.cancel_targets(job):
                    saved = bool(target.get('file') or target.get('drive'))
                    target.update(status='done' if saved else 'cancelled',safe_release=True,batch_complete=True)
                    target.pop('cancel_error',None)
                self.release_error = self.release_message = None
                message = ('已取消剩余执行，已保存的视频保留，显卡已释放' if job.get('file') or job.get('drive') else
                           '任务已取消，显卡已释放，可以提交新任务')
                if job.get('auto_cancel_reason'):
                    message += '；失败原因：' + job['auto_cancel_reason']
                self.event(job,'done' if job.get('file') or job.get('drive') else 'cancelled', message)
        except Exception as exc:
            job.update(status='error',safe_release=False,cancel_error=safe_error(exc))
            self.event(job,'cancel','取消尚未确认：' + safe_error(exc) + '；请重试取消并释放显卡')
        finally:
            job.pop('cancel_finishing',None)
            self.save_jobs()

    def resubmit(self, job_id):
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
            job.pop('cancel_requested', None)
            job.pop('auto_cancel_reason', None)
            job.pop('history_hidden', None)
            job.pop('local_download_error', None)
            job.pop('gpu_release_error', None)
            job.pop('cancel_error', None)

            self.event(job, 'connect', '已重新提交任务，加入排队')

            if not self.worker_thread or not self.worker_thread.is_alive():
                self.worker_thread = threading.Thread(target=self._run_queue, daemon=True)
                self.worker_thread.start()

            return {'ok': True, 'id': job_id}

    def verify_saved_results(self, endpoint):
        for job in self.jobs.values():
            if job.get('endpoint') != endpoint:
                continue
            if job.get('drive'):
                self.drive_storage.confirm_stored(job['drive'])
            if job.get('file'):
                path = self.videos / job['file']
                if not path.is_file():
                    raise HTTPException(409, '本机视频文件已丢失，运行时保留，请先取回结果。')
                result = job.get('remote_result')
                if result:
                    with path.open('rb') as handle:
                        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                    if path.stat().st_size != result['size'] or digest != result['sha256']:
                        raise HTTPException(409, '本机视频校验失败，运行时保留，请先取回结果。')

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

    def check_and_release(self, discard_pending=False):
        with self.lock:
            if self.active:
                raise HTTPException(409, '任务执行中，不能释放显卡')
            if not self.session:
                pending = self.pending_jobs()
                for job in pending:
                    job.update(safe_release=True, status='discarded')
                if pending:
                    self.save_jobs()
                self.idle_since = None
                self.release_message = '已检查：当前没有本控制台管理的显卡，无需释放'
                self.release_error = None
                self.save_release_state()
                return {'ok': True, 'message': self.release_message}
            pending = self.pending_jobs()
            if pending and not discard_pending:
                raise HTTPException(409, '存在未取回的视频。先重试取回，再检查并释放。')
            self.release_checking = True
            try:
                self.verify_saved_results(self.session['endpoint'])
                if not self.transport.check_idle():
                    raise HTTPException(409, 'Colab 仍有任务运行或排队，显卡已保留，请稍后再检查。')
                if self.session:
                    self.transport.release()
                for job in pending:
                    job.update(safe_release=True, status='discarded')
                self.idle_since = None
                self.release_error = None
                self.release_message = '已检查并确认显卡已释放'
                self.save_jobs()
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
            if not self.session or self.active or self.release_policy.mode == 'manual' or self.now() < self.release_retry_at:
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
            data = json.dumps(self.jobs, ensure_ascii=False)
        atomic(self.directory / 'jobs.json', data)

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

    def open_history_folder(self, job_id):
        try:
            res = self.load_history_video(job_id)
            target = self.history_recycle.video(res.get('file') or job_id + '.mp4')
            self.open_video_directory(target.parent)
            return {'ok':True}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400,'无法打开视频文件夹：' + safe_error(exc)) from None

    def recycle_history(self, job_id=None):
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
                           and (job.get('file') or job.get('drive')) and self.history_clearable(job)]
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
            job['events'].append({'time': time.strftime('%H:%M:%S'), 'stage': stage, 'message': message})
            job['events'] = job['events'][-120:]
            self.save_jobs()

    def _run_queue(self):
        self._in_queue_worker = True
        try:
            while True:
                batch = None
                target = None
                with self.lock:
                    active_job = self.jobs.get(self.active)
                    if active_job and active_job.get('status') == 'running':
                        target = active_job
                    else:
                        queued = [j for j in self.jobs.values() if j.get('status') == 'queued' and not j.get('cancel_requested')]
                        if not queued:
                            self.active = None
                            self.idle_since = self.now() if self.session else None
                            if self.release_policy.mode == 'always' and self.session:
                                try:
                                    self.transport.release()
                                except Exception as exc:
                                    self.release_error = safe_error(exc)
                            self.save_release_state()
                            self.save_jobs()
                            break
                        queued.sort(key=lambda j: j.get('created', 0))
                        target = queued[0]
                        self.active = target['id']
                        target['status'] = 'running'

                    batch_id = target.get('batch_id')
                    if batch_id:
                        batch = sorted([j for j in self.jobs.values() if j.get('batch_id') == batch_id
                                        and j.get('status') in ('running', 'queued') and not j.get('cancel_requested')],
                                       key=lambda j: j.get('batch_index', 0))
                        for child in batch:
                            child.pop('history_hidden', None)
                    else:
                        target.pop('history_hidden', None)

                    self.cancel_wakeup.clear()
                    self.idle_since = None
                    self.release_error = self.release_message = None
                    self.save_release_state()
                    self.save_jobs()

                try:
                    if batch:
                        self.run_batch(batch)
                    else:
                        self.run(target)
                except Exception:
                    pass
                finally:
                    with self.lock:
                        if target and self.active == target['id']:
                            self.active = None
        finally:
            self._in_queue_worker = False
            with self.lock:
                if not any(j.get('status') == 'queued' and not j.get('cancel_requested') for j in self.jobs.values()):
                    self.active = None
                    self.save_jobs()

    def start(self, config, retry=None):
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
            unretrieved = [j for j in self.pending_jobs() if j['status'] in ('error', 'interrupted')]
            if not self.session:
                for j in unretrieved:
                    j.update(safe_release=True)
                unretrieved = []
            elif self.session:
                unretrieved = [j for j in unretrieved if j.get('endpoint') == self.session['endpoint']]
            if unretrieved and not retry:
                raise HTTPException(409, '有未取回的任务，请先重试取回或明确释放运行时。')
            if retry:
                job = self.jobs.get(retry)
                if not job or job.get('file') or job.get('drive'):
                    raise HTTPException(400, '该任务无需重新取回')
                if job.get('cancel_requested'):
                    raise HTTPException(409,'任务正在取消或取消尚未确认，请重试取消并释放显卡')
                if not self.session or job.get('endpoint') != self.session['endpoint'] or job['status'] == 'discarded':
                    raise HTTPException(409, '原任务的运行时已离线，无法在另一个运行时取回。')
                batch = sorted((j for j in self.jobs.values() if j.get('batch_id') == job.get('batch_id')
                                and job.get('batch_id') and not (j.get('file') or j.get('drive'))),
                               key=lambda j: j['batch_index'])
                if batch:
                    job = batch[0]
                    for child in batch:
                        child['status'] = 'queued'
                        child.pop('history_hidden', None)
                else:
                    job['status'] = 'queued'
                    job.pop('history_hidden', None)
                target_id = job['id']
            else:
                job_id = uuid.uuid4().hex
                self.configure_release(ReleasePolicy(mode=config.release_mode or ('always' if config.auto_release else 'manual'), idle_minutes=config.idle_minutes))
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

            # If no active job is running, start the runner thread immediately
            if self.active is None:
                self.active = target_id
                self.jobs[target_id]['status'] = 'running'
                self.save_jobs()
                self.worker_thread = threading.Thread(target=self._run_queue, daemon=True)
                self.worker_thread.start()
            else:
                self.save_jobs()
            return target_id

    def run_batch(self, batch):
        """Keep ownership throughout the batch; saved variants are never regenerated on retry."""
        try:
            for job in batch:
                if job.get('cancel_requested'):
                    return
                with self.lock:
                    self.active = job['id']
                    job.update(status='running', safe_release=False)
                self.run(job, in_batch=True)
                if job['status'] != 'done' and not (job.get('drive') and job['status'] == 'warning'):
                    for waiting in batch:
                        if waiting['status'] == 'queued':
                            waiting.update(status='interrupted', message='本批暂停，重试后继续生成')
                    return
            last = batch[-1]
            has_more = any(j.get('status') == 'queued' and not j.get('cancel_requested') for j in self.jobs.values())
            if self.release_policy.mode == 'always' and self.session and not last.get('gpu_release_error') and not has_more:
                self.event(last, 'release', '本批视频全部保存并校验，正在释放 Colab 显卡')
                try:
                    self.transport.release()
                except Exception as exc:
                    last['status'] = 'warning'
                    self.release_error = safe_error(exc)
                    self.release_retry_at = self.now() + 60
                    self.event(last, 'release', '本批视频已保存，但释放尚未确认：' + safe_error(exc))
            if not has_more:
                self.wait_drive_downloads([j['id'] for j in batch])
            with self.lock:
                for child in self.jobs.values():
                    if child.get('batch_id') == last['batch_id']:
                        child['batch_complete'] = True
                missing = sum(bool(j.get('local_download_error')) for j in batch)
                self.event(last, 'download' if missing else 'done',
                    f'本批视频已保存；{missing} 条尚未下载到本机，可分别重新下载' if missing else '本批视频全部生成并保存到本机')
        finally:
            with self.lock:
                if not getattr(self, '_in_queue_worker', False):
                    self.active = None
                    self.idle_since = self.now() if self.session else None
                self.save_release_state()
                self.save_jobs()

    def run(self, job, in_batch=False):
        config = JobConfig(**job['config'])
        try:
            if job.get('drive'):
                self.download_drive_result(job)
                return
            self.event(job, 'connect', '正在连接 ' + config.gpu + '；只有提交任务才申请显卡')
            self.connected_operation(job,lambda:self.transport.connect(config), '连接／申请 ' + config.gpu)
            job['endpoint'] = self.session['endpoint']
            self.check_cancelled(job)
            if in_batch:
                for child in self.jobs.values():
                    if child.get('batch_id') == job['batch_id']:
                        child['endpoint'] = job['endpoint']
            self.save_jobs()
            self.event(job, 'connect', config.gpu + ' 运行时已连接，准备执行任务')
            if not job.get('remote_result'):
                job['execution_started'] = True
                self.save_jobs()
                self.connected_operation(job,lambda:self.transport.execute(job))
            self.check_cancelled(job)
            result = job['remote_result']
            if self.drive_storage.enabled:
                self.event(job, 'download', '从 Colab 上传到 Drive，校验后自动下载到本机')
                job['drive'] = self.connected_operation(job,lambda:self.transport.upload_drive(job), '上传视频到 Drive')
                self.save_jobs()
            else:
                self.event(job, 'download', '下载 MP4 到本机并校验 SHA-256')
                partial = self.videos / (job['id'] + '.partial')
                self.connected_operation(job,lambda:self.transport.download(result, partial), '下载视频到本机')
                with partial.open('rb') as handle:
                    digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                if partial.stat().st_size != result['size'] or digest != result['sha256']:
                    raise RuntimeError('下载校验未通过，Colab 运行时已保留，请重试取回。')
                target = self.videos / (job['id'] + '.mp4')
                partial.replace(target)
                job['file'] = target.name
            job['metadata'] = {k: result[k] for k in ['width', 'height', 'duration', 'audio']}
            saved = '视频已上传 Drive 并校验' if job.get('drive') else '视频已保存到本机'
            self.event(job, 'saved', saved)
            self.check_cancelled(job)
            last_in_batch = bool(in_batch and job.get('drive') and job.get('batch_index') == job.get('batch_total')
                and all(j.get('file') or j.get('drive') for j in self.cancel_targets(job)))
            has_more = any(j.get('status') == 'queued' and not j.get('cancel_requested') for j in self.jobs.values())
            release_now = self.release_policy.mode == 'always' and (not in_batch or last_in_batch) and not has_more
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
                next_waiting = (in_batch and not last_in_batch) or has_more
                if next_waiting:
                    job['status'] = 'warning' if job.get('gpu_release_error') else 'done'
                    kept = ('本条视频已上传 Drive，继续本批生成' if in_batch else
                            (f'{saved}，闲置 {self.release_policy.idle_minutes} 分钟后检查并释放显卡'
                             if self.release_policy.mode == 'idle' else saved + '，显卡保留；请手动检查并释放'))
                    self.event(job, 'done', saved + '，显卡已释放' if release_now else kept)
                    self.start_drive_download(job)
                    self.check_cancelled(job)
                    return
                self.download_drive_result(job)
                self.check_cancelled(job)
                return
            job['status'] = 'done'
            kept = ('本条视频已保存，继续本批生成' if in_batch else
                    (f'{saved}，闲置 {self.release_policy.idle_minutes} 分钟后检查并释放显卡'
                     if self.release_policy.mode == 'idle' else saved + '，显卡保留；请手动检查并释放'))
            self.event(job, 'done', saved + '，显卡已释放' if release_now else kept)
        except Exception as exc:
            if job.get('cancel_requested') or isinstance(exc, (TaskCancelled,ReconnectExhausted)):
                job['cancel_requested'] = True
                if isinstance(exc,ReconnectExhausted):
                    job['auto_cancel_reason'] = str(exc)
                    self.event(job,'cancel','自动重连连续失败：' + str(exc) + '；正在取消原任务并释放显卡')
                self.finish_cancel(job)
                return
            job['status'] = 'error'
            message = safe_error(exc)
            if '自动备份模型到 Drive' in message or 'Drive 备用缓存未找到' in message:
                job['drive_backup_needed'] = True
            can_retrieve = bool(self.session and job.get('endpoint') and job.get('endpoint') == self.session['endpoint']
                                and (job.get('remote_result') or job.get('execution_started')))
            if not in_batch and self.release_policy.mode == 'always' and job.get('safe_release') and not job.get('remote_result') and self.session:
                try:
                    self.transport.release()
                    message += '；失败任务的显卡已释放。'
                except Exception as release_exc:
                    message += '；释放失败：' + safe_error(release_exc)
            elif can_retrieve:
                if in_batch:
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
                if not in_batch and not getattr(self, '_in_queue_worker', False):
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

    def public_job(self, job):
        config = job.get('config', {})
        active = self.jobs.get(self.active) or {}
        siblings = sorted((j for j in self.jobs.values() if job.get('batch_id') and j.get('batch_id') == job['batch_id']),
                          key=lambda j: j['batch_index'])
        local_file = bool(job.get('file') and (self.videos / job['file']).is_file() and not job.get('local_download_error'))
        is_preparing = bool(job['id'] in self.preparing_videos or job['id'] in self.download_threads)
        return {**{k: v for k, v in job.items() if k not in ('remote_result', 'config', 'safe_release', 'drive_upload_id')},
                'local_file': local_file,
                'is_preparing': is_preparing,
                'is_downloading': is_preparing,
                'batch': {'id': job['batch_id'], 'total': job['batch_total'],
                          'completed': sum(bool(j.get('file') and not j.get('local_download_error')) for j in siblings),
                          'finished': bool(job.get('batch_complete')), 'ids': [j['id'] for j in siblings]} if siblings else None,
                'creation': {**{k: config[k] for k in ('title', 'prompt', 'prompt_processing', 'performance', 'encoder', 'quantized_model', 'quantized_vae', 'veda', 'aspect_ratio', 'resolution', 'duration', 'mode', 'seed', 'people', 'strength', 'references', 'generation_type', 'reference_size', 'characters') if k in config}, 'count': job.get('batch_total', 1), 'gpu': config.get('gpu', 'A100'), 'has_image': bool(config.get('image_id'))},
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
    yield
    controller.monitor_stop.set()


app = FastAPI(title='H3 本地控制台', docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
oauth_flow = None


@app.middleware('http')
async def local_only(request: Request, call_next):
    hostname = request.headers.get('host', '').split(':')[0]
    origin = request.headers.get('origin')
    if hostname not in ('127.0.0.1', 'localhost') or request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin not in LOCAL_ORIGINS):
        return JSONResponse({'detail': '只允许本机访问'}, status_code=403)
    if request.url.path.startswith('/api/') and not secrets.compare_digest(request.headers.get('X-H3-Key', ''), API_KEY):
        return JSONResponse({'detail': '需要本地 API Key'}, status_code=401)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.get('/', response_class=HTMLResponse)
def index():
    return (BASE / 'studio.html').read_text(encoding='utf-8').replace('__API_KEY__', API_KEY)


@app.get('/api/status')
def status():
    controller.recover_history()
    ordered = sorted((j for j in controller.jobs.values() if not j.get('history_hidden')),
                     key=lambda j: j['created'], reverse=True)
    saved = [j for j in ordered if j.get('file') or j.get('drive')]
    active_or_queued = [j for j in ordered if j['status'] in ('running', 'queued', 'cancelling')]
    recent_ids = {j['id'] for j in active_or_queued + ordered[:20] + saved[:20]}
    recent_batches = {j['batch_id'] for j in ordered if j['id'] in recent_ids and j.get('batch_id')}
    return {'authorized': (PRIVATE / 'google-token.dat').exists(), 'active': controller.active,
            'gpu_online_available': True,
            'drive_cache': drive_cache.status(),
            'managed_gpu': bool(controller.session), 'release': controller.release_status(),
            'history': {'count': len(saved), 'clearable': sum(controller.history_clearable(j) for j in saved)},
            'jobs': [controller.public_job(j) for j in ordered if j['id'] in recent_ids or j.get('batch_id') in recent_batches]}


@app.delete('/api/history')
def clear_history():
    return controller.clear_history()


class HistoryRecycleRequest(BaseModel):
    job_id: str | None = Field(default=None,min_length=1,max_length=128)


@app.post('/api/history/recycle')
def recycle_history(payload: HistoryRecycleRequest):
    return controller.recycle_history(payload.job_id)


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
    return {'dimensions': DIMENSIONS, 'fps': 24, 'alignment': 32,
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
    import seed_drive_cache

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


@app.post('/api/jobs', status_code=202)
def create_job(config: JobConfig):
    if not (PRIVATE / 'google-token.dat').exists():
        raise HTTPException(401, '请先授权 Google 账号')
    if not config.prompt.strip():
        raise HTTPException(400, '请输入提示词')
    if config.image_id and not re.fullmatch(r'[0-9a-f]{32}\.(png|jpg|webp)', config.image_id):
        raise HTTPException(400, '图片编号不合法')
    if config.image_id and not (PRIVATE / config.image_id).is_file():
        raise HTTPException(400, '上传的图片不存在')
    for reference in config.references:
        if not (PRIVATE / reference.image_id).is_file():
            raise HTTPException(400, f'图片{reference.slot}已丢失，请重新上传')
    for audio in config.audio_references:
        try:
            actual = validate_audio_asset(PRIVATE, audio)
            if abs(actual.duration - audio.duration) > .001:
                raise ValueError('声音样本时长不匹配，请重新上传')
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    # Fast duplicate guard: within 3s if identical config submitted, return existing job
    now_ts = time.time()
    for existing in controller.jobs.values():
        if existing.get('status') in ('queued', 'running') and (now_ts - existing.get('created', 0)) < 3.0:
            cfg = existing.get('config') or {}
            if cfg.get('prompt') == config.prompt and cfg.get('count', 1) == config.count and cfg.get('seed') == config.seed and cfg.get('aspect_ratio') == config.aspect_ratio:
                batch_id = existing.get('batch_id')
                return {'id': existing['id'], 'ids': [j['id'] for j in controller.jobs.values() if j.get('batch_id') == batch_id] if batch_id else [existing['id']]}
    job_id = controller.start(config)
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
def resubmit_job(job_id: str):
    return controller.resubmit(job_id)


class ReleaseConfig(BaseModel):
    discard_pending: bool = False


@app.post('/api/release')
def release(config: ReleaseConfig):
    return controller.check_and_release(discard_pending=config.discard_pending)


@app.post('/api/images')
async def upload_image(file: UploadFile):
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
    (PRIVATE / image_id).write_bytes(data)
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
def preview_characters(config: JobConfig):
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
    path = PRIVATE / audio_id
    path.write_bytes(data)
    stat = path.stat()
    atomic(path.with_suffix(path.suffix + '.json'), json.dumps(dict(duration=duration, size=stat.st_size, mtime_ns=stat.st_mtime_ns)))
    return AudioAsset(audio_id=audio_id, name=Path(file.filename or '声音样本').name[:120], duration=duration).model_dump()


@app.get('/api/audios/{audio_id}')
def audio_preview(audio_id: str):
    if not re.fullmatch(AUDIO_ID, audio_id) or not (PRIVATE / audio_id).is_file():
        raise HTTPException(404, '声音样本不存在')
    return FileResponse(PRIVATE / audio_id, media_type={'wav':'audio/wav', 'mp3':'audio/mpeg',
        'm4a':'audio/mp4', 'ogg':'audio/ogg', 'flac':'audio/flac', 'aac':'audio/aac'}[Path(audio_id).suffix[1:]])


@app.get('/api/images/{image_id}')
def image_preview(image_id: str):
    if not re.fullmatch(r'[0-9a-f]{32}\.(png|jpg|webp)', image_id):
        raise HTTPException(404, '图片不存在')
    path = PRIVATE / image_id
    if not path.is_file():
        raise HTTPException(404, '图片不存在')
    media = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp'}[path.suffix[1:]]
    return FileResponse(path, media_type=media)


@app.get('/video/{filename}')
def video(filename: str, request: Request, download: bool = False):
    if not re.fullmatch(r'[0-9a-f]{32}\.mp4|example\.mp4', filename):
        raise HTTPException(404)
    path = VIDEOS / filename
    byte_range = request.headers.get('range')
    if byte_range and (len(byte_range) > 128 or not re.fullmatch(r'bytes=\d*-\d*', byte_range)):
        raise HTTPException(416, '不支持该视频范围请求')
    job = controller.jobs.get(filename.removesuffix('.mp4'))
    if job and job.get('drive'):
        target = controller.history_recycle.video(job.get('file') or filename)
        if not controller.valid_local_video(target, job['drive']) or not job.get('file'):
            result = controller.load_history_video(job['id'])
            target = controller.history_recycle.video(result['file'])
        path = target
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type='video/mp4', filename=filename if download else None)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=7865, access_log=False)

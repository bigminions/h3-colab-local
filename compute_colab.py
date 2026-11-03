"""Colab compute adapter. App deployment and media paths are injected services.

The adapter owns resource acquisition, transport, recovery and release. It asks
an application adapter for executable source rather than selecting a model.
"""
import json
from pathlib import PurePosixPath
import threading
import time
import uuid

class ColabProvider:
    def __init__(self, controller, services):
        self.services = services
        self.controller = controller
        self.running_runtime = None
        self.application_id = 'h3'
        self.assignment_hash = uuid.uuid4()

    def cancel_pending_assignment(self, job):
        if not job.get('assignment_pending'):
            return
        application = self.services.applications.get(job['config'].get('app_id', 'h3'))
        config = application.config_model(**job['config'])
        client = self.client()
        found = client._get_assignment(uuid.UUID(job['assignment_hash']),variant=self.services.Variant.GPU,
            accelerator=self.services.Accelerator(config.gpu),shape=None if config.gpu=='G4' else self.services.Shape.HIGH_RAM)
        endpoint = getattr(found,'endpoint',None)
        if endpoint:
            self._unassign_confirmed(client,endpoint)
        job['assignment_pending'] = False

    def stop_running(self):
        if self.running_runtime is not None:
            self.running_runtime.stop()

    def client(self):
        path = self.services.PRIVATE / 'google-token.dat'
        if not path.exists():
            raise RuntimeError('请先完成 Google 授权')
        creds = self.services.Credentials.from_authorized_user_info(json.loads(self.services.load_secret(path)), scopes=self.services.SCOPES)
        return self.services.Client(self.services.Prod(), self.services.TimedSession(creds))

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
        assignments = [a for a in self.client().list_assignments() if a.accelerator in self.services.GPU_ACCELERATORS]
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
                while not finished.wait(self.services.CONNECT_PROGRESS_INTERVAL):
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
        accelerator = self.services.Accelerator(config.gpu)
        # G4 has one fixed memory profile: Colab rejects shape=hm for G4.
        shape = None if config.gpu == 'G4' else self.services.Shape.HIGH_RAM
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
                released_by_this_app = any(j.get('endpoint') == config.endpoint and j['status'] == 'done' and j['config'].get('release_mode') == 'always' for j in self.controller.jobs.values())
                if not released_by_this_app:
                    raise RuntimeError('选中的 ' + config.gpu + ' 运行时已离线或型号不匹配，请刷新列表。')
                assignment = self.connection_step('申请并启动 ' + config.gpu + ' 显卡',
                    lambda: client.assign(notebook_hash, variant=self.services.Variant.GPU, accelerator=accelerator, shape=shape))
            else:
                assignment = match
        else:
            assignment = self.connection_step('申请并启动 ' + config.gpu + ' 显卡',
                lambda: client.assign(notebook_hash, variant=self.services.Variant.GPU, accelerator=accelerator, shape=shape))
        proxy = assignment.runtime_proxy_info
        self.controller.session = dict(endpoint=assignment.endpoint, url=proxy.url, token=proxy.token,
            name='h3-local', variant='GPU', accelerator=config.gpu, machine_shape='HIGH_RAM',
            kernel_id=None, session_id=None)
        self.controller.save_session()
        if owned_job:
            owned_job['assignment_pending'] = False
        return self.services.SessionState(**self.controller.session)

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
        return self.services.SessionState(**self.controller.session)

    def execute(self, job):
        s = self.refresh()
        runtime = self.services.ColabRuntime(s.url, s.token, kernel_id=s.kernel_id, session_id=s.session_id)
        def remember(field, value):
            self.controller.session[field] = value
            self.controller.save_session()
        runtime.on_kernel_started = lambda value: remember('kernel_id', value)
        runtime.on_session_started = lambda value: remember('session_id', value)
        self.application_id = job['config'].get('app_id', 'h3')
        application = self.services.applications.get(self.application_id)
        source = application.source(self.services.BASE, job['config'])
        if self.services.drive_cache.enabled:
            import base64
            cache_source = (self.services.BASE / 'drive_cache_remote.py').read_bytes()
            source += '\nimport types, base64\n_drive_module = types.ModuleType("drive_cache_remote")\n'
            source += 'exec(base64.b64decode(' + repr(base64.b64encode(cache_source).decode()) + '), _drive_module.__dict__)\nsys.modules["drive_cache_remote"] = _drive_module\n'
            if self.services.drive_cache.backend == 'native':
                from native_drive_mount import install_mount_hook
                install_mount_hook(runtime, self.client(), s.endpoint,
                    lambda stage, message: self.controller.event(job, stage, message), self.services.PRIVATE)
                mounted_source = (self.services.BASE / 'mounted_drive_cache.py').read_bytes()
                source += '_mounted = types.ModuleType("mounted_drive_cache")\nexec(base64.b64decode(' + repr(base64.b64encode(mounted_source).decode()) + '), _mounted.__dict__)\n'
                source += 'from google.colab import drive\ndrive.mount("/content/drive", timeout_ms=900000)\nMODEL_CACHE = _mounted.MountedDriveModelCache(event)\n'
            else:
                source += 'MODEL_CACHE = _drive_module.DriveModelCache(' + repr(self.services.drive_cache.remote_token()) + ', event, quota_project=' + repr(self.services.drive_cache.quota_project) + ', token_provider=lambda: input("H3_DRIVE_ACCESS_TOKEN"))\n'
        for name in application.input_assets(job['config']):
            asset = self.services.asset_library.resolve(name)
            self.services.TimedContents(s).upload(str(asset), 'content/' + asset.name)
        code = application.invocation(source, job)
        lines_seen = set()
        def output_hook(output):
            self.controller.check_cancelled(job)
            for line in str(output.get('text', '')).splitlines():
                if line.startswith(application.event_prefix) and line not in lines_seen:
                    try:
                        payload = json.loads(line[len(application.event_prefix):])
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
            self.services.enable_cancellation(runtime, lambda: self.controller.check_cancelled(job))
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
                    observed = self.services.observe_execution(runtime, s.session_id, output_hook)
                    errors = [o for o in observed if o.get('output_type') == 'error']
                    if errors:
                        raise RuntimeError(errors[-1].get('evalue','远端原任务执行失败'))
                    if job.get('remote_result'):
                        return
                    self.controller.event(job, 'recover', '原内核已空闲，检查并恢复本任务已生成的结果')
            outputs = runtime.execute_code(code, allow_stdin=self.services.drive_cache.enabled,
                stdin_hook=self.services.drive_cache.runtime_stdin_hook(runtime) if self.services.drive_cache.enabled and self.services.drive_cache.backend == 'api' else None,
                output_hook=output_hook, timeout=4500)
            for output in outputs:
                output_hook(output)
            errors = [o for o in outputs if o.get('output_type') == 'error']
            if errors:
                raise RuntimeError(errors[-1].get('evalue', 'Colab 执行失败'))
            if not job.get('remote_result'):
                raise RuntimeError('远程执行结束，但未收到应用结果；运行时保留以便检查。')
        finally:
            runtime.stop()
            if self.running_runtime is runtime:
                self.running_runtime = None

    def download(self, result, target):
        path = PurePosixPath(result['path'])
        application = self.services.applications.get(self.application_id)
        if not path.is_relative_to(application.remote_output) or '..' in path.parts:
            raise RuntimeError('远程输出路径不合法')
        self.services.TimedContents(self.refresh()).download(str(path).lstrip('/'), str(target))

    def remote_json(self, code, marker, timeout=180):
        s = self.refresh()
        runtime = self.services.ColabRuntime(s.url, s.token, kernel_id=s.kernel_id, session_id=s.session_id)
        def remember(field, value):
            self.controller.session[field] = value
            self.controller.save_session()
        runtime.on_kernel_started = lambda value: remember('kernel_id', value)
        runtime.on_session_started = lambda value: remember('session_id', value)
        try:
            self.running_runtime = runtime
            active_job = self.controller.jobs.get(self.controller.active)
            if active_job:
                self.services.enable_cancellation(runtime, lambda: self.controller.check_cancelled(active_job))
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
        application = self.services.applications.get(self.application_id)
        if not path.is_relative_to(application.remote_output) or '..' in path.parts:
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
        if assignment.accelerator not in self.services.GPU_ACCELERATORS:
            raise self.services.HTTPException(409, '选中的运行时不是显卡，未执行释放')
        if not self.check_idle(endpoint):
            raise self.services.HTTPException(409, '该显卡仍有任务运行或排队，请完成后再释放')
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
            s = self.services.SessionState(**self.controller.session)
        else:
            s = self.services.SessionState(endpoint=endpoint, url=assignment.runtime_proxy_info.url,
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
        runtime = self.services.ColabRuntime(s.url, s.token, kernel_id=kernel_id, session_id=s.session_id)
        def remember(field, value):
            if managed:
                self.controller.session[field] = value
                self.controller.save_session()
        runtime.on_kernel_started = lambda value: remember('kernel_id', value)
        runtime.on_session_started = lambda value: remember('session_id', value)
        application = self.services.applications.get(self.application_id)
        code = application.idle_probe
        try:
            outputs = runtime.execute_code(code, timeout=20)
            for output in outputs:
                for line in str(output.get('text', '')).splitlines():
                    if line.startswith(application.idle_marker):
                        return json.loads(line[len(application.idle_marker):])['idle'] is True
            raise RuntimeError('无法确认 Colab 生成队列，保留显卡，请稍后检查。')
        finally:
            runtime.stop()



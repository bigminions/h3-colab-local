"""Seed/benchmark H3 models on a separately owned runtime, or automatically back up to Drive."""
import argparse
import base64
import json
from pathlib import Path
import sys
import time
import uuid

import requests

import app
from colab_cli.client import Variant, Accelerator, Shape
from colab_cli.runtime import ColabRuntime
from runtime_watchdog import execute_monitored


def run_backup(gpu_type='T4', config=None, benchmark=False, reuse=False, concurrent=False, on_progress=None):
    if concurrent:
        reuse = True
    if (app.controller.active and not concurrent) or (app.controller.session and not reuse):
        raise RuntimeError('工作台有任务或保留的显卡，请完成并释放后再运行备份')
    native = app.drive_cache.backend == 'native'
    token = None
    if not native:
        token = app.drive_cache.remote_token()
        quota = app.drive_cache.quota()
        print('Drive available GB:', round((int(quota['limit']) - int(quota.get('usage', 0))) / 1e9, 2)
              if quota.get('limit') else 'unlimited', flush=True)
    else:
        print('Using explicitly authorized Colab native Drive mount.', flush=True)

    if config is None:
        jobs = [j for j in app.controller.jobs.values() if j.get('status') == 'done']
        if jobs:
            config = max(jobs, key=lambda job: job['created'])['config']
        else:
            config = {
                'prompt': 'A cinematic landscape shot with characters.',
                'mode': 'pdd',
                'people': True,
                'prompt_processing': 'faithful',
                'resolution': '720p',
                'duration': 5,
                'seed': 1,
                'job_id': 'backup-seed'
            }

    session_path = app.PRIVATE / 'drive-cache-session.dat'
    if session_path.exists():
        raise RuntimeError('前次备份会话尚未确认释放，请先检查 .private/drive-cache-session.dat')

    def emit_progress(msg, ok=None, running=True):
        print(msg, flush=True)
        app.atomic(app.PRIVATE / 'drive-cache-progress.json', json.dumps({
            'running': running, 'message': msg, 'updated_at': time.time(), 'reuse': reuse, 'ok': ok
        }))
        if on_progress:
            on_progress(msg)

    client = app.controller.transport.client()
    endpoint = None
    runtime = None
    if reuse:
        if not app.controller.session:
            raise RuntimeError('没有可复用的本控制台运行时')
        state = app.controller.transport.refresh()
        endpoint = state.endpoint
        runtime = ColabRuntime(state.url, state.token,
            kernel_id=None if concurrent else state.kernel_id,
            session_id=None if concurrent else state.session_id)
    else:
        emit_progress('正在申请低成本 %s 显卡运行时...' % gpu_type)
        try:
            acc = Accelerator.T4 if gpu_type == 'T4' else (Accelerator(gpu_type) if hasattr(Accelerator, gpu_type) else Accelerator.T4)
            assignment = client.assign(uuid.uuid4(), variant=Variant.GPU, accelerator=acc, shape=Shape.STANDARD)
            endpoint = assignment.endpoint
            runtime = ColabRuntime(assignment.runtime_proxy_info.url, assignment.runtime_proxy_info.token)
            emit_progress('已分配 %s 显卡运行时' % gpu_type)
        except Exception as alloc_exc:
            emit_progress('申请 %s 显卡未成功（%s），尝试分配标准 CPU 运行时...' % (gpu_type, app.safe_error(alloc_exc)))
            assignment = client.assign(uuid.uuid4(), variant=Variant.DEFAULT)
            endpoint = assignment.endpoint
            runtime = ColabRuntime(assignment.runtime_proxy_info.url, assignment.runtime_proxy_info.token)
            emit_progress('已分配标准 CPU 运行时')

    app.save_secret(session_path, json.dumps({'endpoint': endpoint, 'reuse': reuse}).encode())
    result = None
    started = time.monotonic()
    reports = app.PRIVATE / 'drive-cache-reports'
    reports.mkdir(exist_ok=True)
    report_path = reports / ('drive-cache-benchmark.json' if benchmark else 'drive-cache-seed.json')
    seen = set()
    metrics = []

    def output_hook(output):
        nonlocal result
        for line in str(output.get('text', '')).splitlines():
            if line in seen:
                continue
            seen.add(line)
            if line.startswith('H3_EVENT '):
                payload = json.loads(line[9:])
                emit_progress(payload['message'], running=True)
                if payload.get('cache_metric'):
                    metrics.append(payload['cache_metric'])
                    app.atomic(reports / 'drive-cache-transfers.json', json.dumps(metrics, ensure_ascii=False, indent=2))
            elif line.startswith('H3_CACHE_REPORT '):
                result = json.loads(line[len('H3_CACHE_REPORT '):])

    cache_source = (app.BASE / 'drive_cache_remote.py').read_bytes()
    remote_path = Path(__file__).resolve().with_name('remote_h3.py')
    source = remote_path.read_text(encoding='utf-8')
    code = "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'huggingface_hub'], check=True)\n"
    code += source + '\nimport types, base64, shutil\n'
    code += '_module = types.ModuleType("drive_cache_remote")\nexec(base64.b64decode(' + repr(base64.b64encode(cache_source).decode()) + '), _module.__dict__)\nsys.modules["drive_cache_remote"] = _module\n'
    if native:
        from native_drive_mount import install_mount_hook
        def emit(stage, message):
            emit_progress(message, running=True)
        install_mount_hook(runtime, client, endpoint, emit, app.PRIVATE)
        mounted_source = (app.BASE / 'mounted_drive_cache.py').read_bytes()
        code += '_mounted = types.ModuleType("mounted_drive_cache")\nexec(base64.b64decode(' + repr(base64.b64encode(mounted_source).decode()) + '), _mounted.__dict__)\n'
        code += 'from google.colab import drive\ndrive.mount("/content/drive", timeout_ms=900000)\ncache = _mounted.MountedDriveModelCache(event)\n'
    else:
        code += 'cache = _module.DriveModelCache(' + repr(token) + ', event, quota_project=' + repr(app.drive_cache.quota_project) + ', token_provider=lambda: input("H3_DRIVE_ACCESS_TOKEN"))\n'
    code += 'config = json.loads(' + repr(json.dumps(config)) + ')\nbenchmark = ' + repr(benchmark) + '\n'
    code += 'native = ' + repr(native) + '\nreuse = ' + repr(reuse) + '\n'
    code += '''from huggingface_hub import hf_hub_download
ROOT = Path('/content/ComfyUI' if reuse else '/content/h3-drive-cache-test')
if benchmark and not native:
    cache.session.headers['Authorization'] = 'Bearer expired-for-refresh-check'
    cache.request('GET', cache.API + '/about', params={'fields':'storageQuota'})
    event('prepare', 'Drive 自动续期通道验证通过，无需再次 Google 认证')
plan = [(repo, filename, category, _module.model_identity(repo, filename, CLIP_REVISION if filename == CLIP else None))
        for repo, filename, category in files_for(config)]
total = sum(identity['size'] for _, _, _, identity in plan)
event('prepare', '模型备份：%d 个模型，共 %.2f GB' % (len(plan), total / 1e9))
if not reuse and shutil.disk_usage('/content').free < total + 2 * 1024**3:
    raise RuntimeError('运行时本地磁盘不足')
if not native:
    quota = cache.request('GET', cache.API + '/about', params={'fields':'storageQuota'}).json()['storageQuota']
    missing = sum(identity['size'] for _, _, _, identity in plan if not cache.lookup(identity))
    if not benchmark and quota.get('limit') and int(quota['limit']) - int(quota.get('usage', 0)) < missing:
        raise RuntimeError('Drive 剩余空间不足，需要 %.2f GB' % (missing / 1e9))
for repo, filename, category, identity in plan:
    if not benchmark and cache.lookup(identity):
        event('prepare', 'Drive 已有该模型备份，跳过保存：' + filename)
        continue
    target = ROOT / 'models' / category / Path(filename).name
    target.parent.mkdir(parents=True, exist_ok=True)
    if cache.restore(target, identity):
        if filename == CLIP:
            verify_clip(target)
        if benchmark and not reuse:
            if not target.resolve().is_relative_to(ROOT.resolve()):
                raise RuntimeError('临时模型路径越界，停止清理')
            target.unlink()
        continue
    if benchmark:
        raise RuntimeError('缺少已校验 Drive 缓存，停止对比：' + filename)
    if reuse:
        if not target.exists():
            raise RuntimeError('复用运行时缺少模型，停止以避免重复下载：' + filename)
        cache.store(target, identity)
        continue
    event('prepare', '正在从 Hugging Face 下载模型准备备份：' + filename)
    prepare_model(repo, filename, category, hf_hub_download, allow_store=False)
    event('prepare', '正在上传模型到 Google Drive (H3-Model-Cache)：' + filename)
    cache.store(target, identity)
    event('prepare', '模型备份完成并已校验：' + filename)
    actual = target.resolve()
    if not actual.is_relative_to(ROOT.resolve()):
        raise RuntimeError('临时模型路径越界，停止清理')
    target.unlink()
    if actual != target:
        actual.unlink()
if native:
    cache.flush()
report = {'ok':True, 'operation':'benchmark' if benchmark else 'seed', 'total_bytes':total,
          'models':[{'filename':filename, 'category':category, **identity} for repo,filename,category,identity in plan],
          'drive_metrics':cache.metrics, 'backend':'native' if native else 'api',
          'folder_url':'https://drive.google.com/drive/my-drive' if native else 'https://drive.google.com/drive/folders/' + cache.folder_id()}
print('H3_CACHE_REPORT ' + json.dumps(report), flush=True)
'''
    try:
        emit_progress('正在等待运行时 Jupyter 服务就绪...', running=True)
        for attempt in range(30):
            try:
                ready = requests.get(runtime.url + '/api/kernels',
                    params={'authuser':'0','colab-runtime-proxy-token':runtime.token},
                    headers={'X-Colab-Client-Agent':'colab-cli','X-Colab-Runtime-Proxy-Token':runtime.token},
                    timeout=(10,15))
                if ready.ok:
                    emit_progress('Jupyter 服务已就绪，开始执行模型下载与 Drive 备份...', running=True)
                    break
            except requests.RequestException:
                pass
            time.sleep(3)
        else:
            raise RuntimeError('Jupyter 服务未就绪，已停止本次备份任务')
        outputs = execute_monitored(runtime, client, endpoint, code, allow_stdin=True,
            stdin_hook=None if native else app.drive_cache.runtime_stdin_hook(runtime),
            output_hook=output_hook, timeout=7200)
        for output in outputs:
            output_hook(output)
        if any(output.get('output_type') == 'error' for output in outputs):
            error = next(output for output in reversed(outputs) if output.get('output_type') == 'error')
            raise RuntimeError('模型备份操作失败：' + error.get('evalue', '未知错误'))
        if not result:
            raise RuntimeError('没有收到模型备份完成确认')
        result['total_seconds'] = round(time.monotonic() - started, 2)
        result['transfers'] = metrics
        app.atomic(report_path, json.dumps(result, ensure_ascii=False, indent=2))
        emit_progress('所有模型已全部成功备份至 Google Drive，显卡已自动释放。', ok=True, running=False)
        return result
    except Exception as exc:
        err = app.safe_error(exc)
        emit_progress('自动备份失败：%s（显卡已自动释放）' % err, ok=False, running=False)
        raise
    finally:
        try:
            if runtime is not None:
                runtime.stop(shutdown_kernel=concurrent)
        finally:
            if reuse:
                if not concurrent:
                    try:
                        requests.post('http://127.0.0.1:7865/api/release', json={},
                            headers={'X-LStudio-Key': app.API_KEY}, timeout=60)
                    except Exception:
                        pass
                original_path = app.PRIVATE / 'drive-cache-original-release.json'
                if original_path.exists():
                    try:
                        policy = json.loads(original_path.read_text())
                        requests.put('http://127.0.0.1:7865/api/release-policy',
                            headers={'X-LStudio-Key': app.API_KEY}, json=policy, timeout=10)
                    except Exception:
                        pass
            else:
                try:
                    if endpoint and any(a.endpoint == endpoint for a in client.list_assignments()):
                        client.unassign(endpoint)
                except Exception as unassign_exc:
                    print('释放运行时异常:', unassign_exc, flush=True)
            session_path.unlink(missing_ok=True)
            app.atomic(app.PRIVATE / 'drive-native-auth.json', json.dumps({'pending': False}))
            print('Concurrent cache kernel closed; workbench GPU release policy restored.' if concurrent
                  else 'Owned reused GPU released.' if reuse else 'Owned runtime released.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--benchmark', action='store_true', help='Read existing Drive cache on a fresh VM; no HF fallback')
    parser.add_argument('--reuse', action='store_true', help='Reuse the idle GPU owned by the workbench for the initial save')
    parser.add_argument('--concurrent', action='store_true', help='Copy already downloaded files in a separate CPU-only kernel on the generating GPU VM')
    parser.add_argument('--gpu', default='T4', help='Minimal GPU to request (default: T4)')
    args = parser.parse_args()
    try:
        run_backup(gpu_type=args.gpu, benchmark=args.benchmark, reuse=args.reuse, concurrent=args.concurrent)
    except Exception:
        sys.exit(1)


if __name__ == '__main__':
    main()

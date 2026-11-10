"""Runs inside the owned Colab kernel, with llama-server bound to loopback and exposed via Cloudflare Tunnel."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import threading
import time

import requests


ROOT = Path('/content/lstudio-gemma')
SERVER = 'http://127.0.0.1:18829'
CONTEXT = 8192
REPLY_TOKENS = 1536
RELEASE = 'b11577'
CUDA_ARCHIVES = {
    f'llama-{RELEASE}-bin-ubuntu-cuda-12.8-x64.tar.gz':
        '97f565404b734b4da785953099a38d5554727f8816212debfe33846bfb89a9fc',
    f'cudart-llama-{RELEASE}-bin-ubuntu-cuda-12.8-x64.tar.gz':
        'b4372b9e608e20bede6808d42105ca4d8b5c22b9777fca52d3040c3816cce5f0',
}
MODELS = {
    'native': {
        'alias': 'lstudio-gemma-native',
        'label': 'Gemma 4 31B 原生版',
        'multimodal': True,
        'files': [
            ('https://huggingface.co/google/gemma-4-31B-it-qat-q4_0-gguf/resolve/59dde24573e7e61570dba08b18a2e1fe246955ed/gemma-4-31B_q4_0-it.gguf',
             'gemma-4-31B_q4_0-it.gguf', '179cfb99212709597eae5929112cfca677e1bbf566178b479ae1da0c4772874b', '17.7'),
            ('https://huggingface.co/google/gemma-4-31B-it-qat-q4_0-gguf/resolve/59dde24573e7e61570dba08b18a2e1fe246955ed/gemma-4-31B-it-mmproj.gguf',
             'gemma-4-31B-it-mmproj.gguf', None, '1.2'),
        ],
        'main_file': 'gemma-4-31B_q4_0-it.gguf',
        'mmproj': 'gemma-4-31B-it-mmproj.gguf',
        'min_vram_gb': 24,
    },
    'heretic': {
        'alias': 'lstudio-gemma-heretic',
        'label': 'Gemma 4 31B ARA Heretic 破甲版',
        'multimodal': True,
        'files': [
            ('https://huggingface.co/mradermacher/gemma-4-31B-it-ARA-heretic-GGUF/resolve/1b49bbdcb3dfaa67cc4daced7da7d16552959f97/gemma-4-31B-it-ARA-heretic.Q4_K_M.gguf',
             'gemma-4-31B-it-ARA-heretic.Q4_K_M.gguf', '305b953a3909ff806b2e9ffee6d8775074ad1eae413a3d1bcd2b09a7cd1ee353', '18.7'),
            ('https://huggingface.co/google/gemma-4-31B-it-qat-q4_0-gguf/resolve/59dde24573e7e61570dba08b18a2e1fe246955ed/gemma-4-31B-it-mmproj.gguf',
             'gemma-4-31B-it-mmproj.gguf', None, '1.2'),
        ],
        'main_file': 'gemma-4-31B-it-ARA-heretic.Q4_K_M.gguf',
        'mmproj': 'gemma-4-31B-it-mmproj.gguf',
        'min_vram_gb': 24,
    },
    'gpt_oss_120b': {
        'alias': 'lstudio-gpt-oss-120b',
        'label': 'GPT-OSS 120B (OpenAI)',
        'multimodal': False,
        'files': [
            ('https://huggingface.co/unsloth/gpt-oss-120b-GGUF/resolve/main/Q4_K_M/gpt-oss-120b-Q4_K_M-00001-of-00002.gguf',
             'gpt-oss-120b-Q4_K_M-00001-of-00002.gguf', None, '49.6'),
            ('https://huggingface.co/unsloth/gpt-oss-120b-GGUF/resolve/main/Q4_K_M/gpt-oss-120b-Q4_K_M-00002-of-00002.gguf',
             'gpt-oss-120b-Q4_K_M-00002-of-00002.gguf', None, '13.1'),
        ],
        'main_file': 'gpt-oss-120b-Q4_K_M-00001-of-00002.gguf',
        'min_vram_gb': 80,
    },
}
CLOUDFLARED_URL = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64'
DIAGNOSTIC_PREFIXES = (
    'ggml_cuda_init:', 'load_tensors: offload', 'load_tensors:   ', 'llama_context:',
    'llama_kv_cache:', 'llama_model_params_fit:', 'srv  load_model:', 'slot print_timing:',
    'prompt eval time', 'eval time', 'total time',
)


def event(stage, message, **data):
    sys.stdout.write('GEMMA_EVENT ' + json.dumps(dict(stage=stage, message=message, **data)) + '\n')
    sys.stdout.flush()


def gpu_sample():
    try:
        output = subprocess.run([
            'nvidia-smi', '--query-gpu=name,utilization.gpu,memory.used,memory.total',
            '--format=csv,noheader,nounits',
        ], capture_output=True, text=True, check=True, timeout=2).stdout
        name, usage, used, total = (value.strip() for value in output.splitlines()[0].split(','))
        return {'name': name, 'usage': int(usage) if usage.isdecimal() else None,
                'used_mib': int(used), 'total_mib': int(total)}
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def server_diagnostics():
    try:
        with (ROOT / 'server.log').open('rb') as log:
            size = os.fstat(log.fileno()).st_size
            log.seek(max(0, size - 32768))
            lines = log.read(32768).decode('utf-8', errors='replace').splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip().startswith(DIAGNOSTIC_PREFIXES)][-120:]


def monitor_debug(finished):
    while not finished.is_set():
        gpu = gpu_sample()
        if not finished.is_set():
            event('debug', '', gpu=gpu, lines=server_diagnostics())
        if finished.wait(3):
            return


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def download_verified(url, path, sha256=None, label='', size=None):
    if path.is_file():
        return
    partial = path.with_suffix('.partial')
    event('prepare', label)
    with (ROOT / 'download.log').open('w') as log:
        process = subprocess.Popen([
            'curl', '--fail', '--location', '--silent', '--show-error', '--retry', '3',
            '--connect-timeout', '30', '--speed-time', '90', '--speed-limit', '1024',
            '--continue-at', '-', '--output', str(partial), url,
        ], stdout=log, stderr=log)
        started = time.monotonic()
        try:
            while True:
                try:
                    code = process.wait(timeout=8)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() - started > 3000:
                        raise RuntimeError(label + '超时，可重新开启并继续下载')
                    downloaded = partial.stat().st_size if partial.exists() else 0
                    progress = f'{downloaded / 1e9:.2f} / {size} GB' if size else f'{downloaded / 1e6:.0f} MB'
                    event('prepare', label + ' · ' + progress)
        finally:
            stop_process(process)
    if code not in (0, 22, 33) or not partial.is_file():
        raise RuntimeError(f'{label}失败（curl {code}），可重新尝试')
    if sha256:
        event('prepare', '正在校验 ' + path.name)
        with partial.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != sha256:
            if not code or code == 33:
                partial.unlink()
            raise RuntimeError(f'{label}未通过校验，可重新尝试（curl {code}）')
    partial.replace(path)


def install_server():
    folder = ROOT / RELEASE
    ready = folder / '.ready'
    if not ready.exists():
        folder.mkdir(exist_ok=True)
        for name, digest in CUDA_ARCHIVES.items():
            archive = ROOT / name
            download_verified(f'https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/{name}',
                              archive, digest, '正在准备 llama.cpp CUDA 环境')
            with tarfile.open(archive) as package:
                package.extractall(folder, filter='data')
        ready.touch()
        for name in CUDA_ARCHIVES:
            (ROOT / name).unlink(missing_ok=True)
    return next(folder.rglob('llama-server')), sorted({str(p.parent) for p in folder.rglob('*.so*')})


def ensure_model(variant, secret=None):
    spec = MODELS.get(variant)
    if not spec:
        raise ValueError(f'未知模型版本：{variant}')
    alias = spec['alias']
    label = spec.get('label', alias)
    event('prepare', f'正在检查 {label} 服务')
    headers = {'Authorization': f'Bearer {secret}'} if secret else {}
    try:
        ready = requests.get(SERVER + '/v1/models', headers=headers, timeout=3)
    except requests.ConnectionError:
        ready = None
    if ready is not None:
        if ready.ok and any(item['id'] == alias for item in ready.json().get('data', [])):
            need_mmproj = bool(spec.get('mmproj'))
            has_vision = False
            if need_mmproj:
                try:
                    props_resp = requests.get(SERVER + '/props', headers=headers, timeout=3)
                    if props_resp.ok:
                        has_vision = bool(props_resp.json().get('modalities', {}).get('vision'))
                except Exception:
                    has_vision = False
            if not need_mmproj or has_vision:
                event('prepare', f'复用已加载的 {label}')
                return alias
            event('prepare', '检测到服务未挂载多模态视觉投影，正在重启')
            try:
                subprocess.run(['pkill', '-f', 'llama-server'], timeout=5)
                time.sleep(1)
            except Exception:
                pass
        else:
            raise RuntimeError('推理服务仍在启动或占用了端口，请关闭应用后重试')
    binary, lib_dirs = install_server()
    files = spec['files']
    for idx, (url, filename, digest, size) in enumerate(files, 1):
        target = ROOT / filename
        part_tag = f' ({idx}/{len(files)})' if len(files) > 1 else ''
        download_verified(url, target, digest, f'正在下载模型权重{part_tag}', size)
    model = ROOT / spec['main_file']
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = ':'.join(lib_dirs + ['/usr/lib64-nvidia', env.get('LD_LIBRARY_PATH', '')]).rstrip(':')
    args = [str(binary), '--model', str(model), '--alias', alias, '--host', '127.0.0.1',
            '--port', '18829', '--ctx-size', str(CONTEXT), '--parallel', '1',
            '--device', 'CUDA0', '--fit', 'on', '--jinja', '--reasoning', 'off',
            '--no-context-shift', '--no-webui', '--slots']
    if spec.get('mmproj'):
        mmproj_file = ROOT / spec['mmproj']
        if mmproj_file.is_file():
            args.extend(['--mmproj', str(mmproj_file)])
    if secret:
        args.extend(['--api-key', secret])
    event('prepare', '正在把模型加载到 Colab 显卡')
    log_path = ROOT / 'server.log'
    with log_path.open('w') as log:
        process = subprocess.Popen(args, env=env, stdout=log, stderr=log, start_new_session=True)
    try:
        started = time.monotonic()
        next_report = started + 10
        while time.monotonic() - started < 300:
            if process.poll() is not None:
                with log_path.open('rb') as log:
                    log.seek(max(0, log_path.stat().st_size - 2000))
                    tail = log.read().decode(errors='replace').strip().splitlines()
                raise RuntimeError('模型加载失败：' + '；'.join(tail[-4:]))
            try:
                if requests.get(SERVER + '/health', headers=headers, timeout=2).ok:
                    return alias
            except (requests.ConnectionError, requests.Timeout):
                pass
            now = time.monotonic()
            if now >= next_report:
                event('prepare', f'正在加载模型和分配上下文 · 已等待 {now - started:.0f} 秒')
                next_report = now + 10
            time.sleep(1)
        raise RuntimeError('模型加载超时')
    except BaseException:
        stop_process(process)
        raise


def ensure_tunnel(port=18829, secret=None):
    event('prepare', '正在准备 Cloudflare 快速隧道')
    binary = ROOT / 'cloudflared'
    if not binary.is_file():
        download_verified(CLOUDFLARED_URL, binary, None, '正在下载 Cloudflare 隧道组件')
        try:
            os.chmod(binary, 0o755)
        except OSError:
            pass

    tunnel_file = ROOT / 'tunnel.url'
    headers = {'Authorization': f'Bearer {secret}'} if secret else {}
    if tunnel_file.is_file():
        saved = tunnel_file.read_text(encoding='utf-8').strip()
        if saved:
            try:
                if requests.get(saved + '/health', headers=headers, timeout=5).ok:
                    event('prepare', f'复用已有公网端点：{saved}')
                    return saved
            except Exception:
                pass

    log_path = ROOT / 'tunnel.log'
    cmd = [str(binary), 'tunnel', '--url', f'http://127.0.0.1:{port}', '--no-autoupdate']
    with log_path.open('w') as log:
        process = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)

    event('prepare', '正在分配公网临时安全端点')
    started = time.monotonic()
    url_pattern = re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')
    tunnel_url = None

    while time.monotonic() - started < 60:
        if process.poll() is not None:
            raise RuntimeError('Cloudflare 隧道异常退出')
        if log_path.exists():
            content = log_path.read_text(encoding='utf-8', errors='replace')
            matches = url_pattern.findall(content)
            if matches:
                candidate = matches[-1]
                try:
                    if requests.get(candidate + '/health', headers=headers, timeout=5).ok:
                        tunnel_url = candidate
                        break
                except Exception:
                    pass
        time.sleep(1)

    if not tunnel_url:
        stop_process(process)
        raise RuntimeError('分配 Cloudflare 隧道超时，请稍后重试')

    tunnel_file.write_text(tunnel_url, encoding='utf-8')
    event('prepare', f'公网安全端点就绪：{tunnel_url}')
    return tunnel_url


def update_reply_metrics(metrics, chunk):
    sources = (
        (chunk.get('timings'), {'predicted_per_second': 'tokens_per_second', 'predicted_n': 'output_tokens',
                                'prompt_ms': 'prompt_ms'}),
        (chunk.get('usage'), {'prompt_tokens': 'input_tokens', 'completion_tokens': 'output_tokens'}),
    )
    for source, fields in sources:
        if not isinstance(source, dict):
            continue
        for key, label in fields.items():
            value = source.get(key)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                metrics[label] = value


def stream_reply(variant, messages, *, reuse_model=False, secret=None):
    spec = MODELS.get(variant, {})
    default_alias = spec.get('alias', 'lstudio-gemma-' + variant)
    alias = default_alias if reuse_model else ensure_model(variant, secret=secret)
    event('prepare', '正在准备对话上下文')
    context_started = time.monotonic()
    headers = {'Authorization': f'Bearer {secret}'} if secret else {}
    while True:
        text_messages = []
        for m in messages:
            content = m.get('content')
            if isinstance(content, list):
                txt = ' '.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
                text_messages.append({'role': m.get('role'), 'content': txt})
            else:
                text_messages.append(m)

        try:
            template = requests.post(SERVER + '/apply-template', json={'messages': text_messages}, headers=headers, timeout=15)
        except requests.ConnectionError:
            if not reuse_model:
                raise
            reuse_model = False
            event('prepare', '模型服务连接中断，正在恢复')
            alias = ensure_model(variant, secret=secret)
            continue
        template.raise_for_status()
        if reuse_model:
            event('prepare', f'复用已加载的 {spec["label"]} 模型')
            reuse_model = False
        tokens = requests.post(SERVER + '/tokenize', json={'content': template.json()['prompt'],
                               'add_special': True, 'parse_special': True}, headers=headers, timeout=15)
        tokens.raise_for_status()
        if len(tokens.json()['tokens']) + REPLY_TOKENS + 64 <= CONTEXT:
            break
        has_system = bool(messages and messages[0].get('role') == 'system')
        if len(messages) < (4 if has_system else 3):
            raise ValueError('这条消息太长，请缩短后重试')
        if has_system:
            messages = [messages[0]] + messages[3:]
        else:
            messages = messages[2:]
    event('chat', f'{spec["label"]} 正在回复（上下文准备 {time.monotonic() - context_started:.1f} 秒）')
    body = {'model': alias, 'messages': messages, 'stream': True, 'max_tokens': REPLY_TOKENS,
            'timings_per_token': True, 'stream_options': {'include_usage': True}}
    parts = []
    metrics = {}
    started = time.monotonic()
    last_update = started - 1
    with requests.post(SERVER + '/v1/chat/completions', json=body, headers=headers, stream=True, timeout=(5, 180)) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith(b'data: '):
                continue
            if line == b'data: [DONE]':
                text = ''.join(parts).strip()
                if text:
                    metrics['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                    return {'text': text, 'metrics': metrics}
                raise RuntimeError('模型未返回可显示的文本')
            chunk = json.loads(line[6:])
            if chunk.get('error'):
                raise RuntimeError('推理服务无法完成回复')
            update_reply_metrics(metrics, chunk)
            choices = chunk.get('choices') or []
            now = time.monotonic()
            first_text = False
            if choices and (content := choices[0]['delta'].get('content')):
                parts.append(content)
                first_text = 'first_text_ms' not in metrics and bool(content.strip())
                if first_text:
                    metrics['first_text_ms'] = round((now - started) * 1000)
            if first_text or ('first_text_ms' in metrics and now - last_update >= 0.4):
                metrics['elapsed_ms'] = round((now - started) * 1000)
                event('draft', '', draft=''.join(parts), metrics=metrics)
                last_update = now
    raise RuntimeError('回复流中断，请重试')


def run(config):
    event('prepare', 'Colab 内核已接收大模型任务')
    ROOT.mkdir(exist_ok=True)
    result_path = ROOT / (config['job_id'] + '.json')
    finished = threading.Event()
    monitor = threading.Thread(target=monitor_debug, args=(finished,), daemon=True)
    monitor.start()
    try:
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding='utf-8'))
        else:
            secret = config.get('secret')
            if config['operation'] == 'open':
                ensure_model(config['variant'], secret=secret)
                tunnel_url = ensure_tunnel(18829, secret=secret)
                result = {'ready': True, 'endpoint': tunnel_url, 'secret': secret}
            else:
                result = stream_reply(config['variant'], config['messages'],
                                      reuse_model=config.get('reuse_model', False), secret=secret)
            partial = result_path.with_suffix('.tmp')
            partial.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            partial.replace(result_path)
        event('result', '模型与安全端点已就绪' if config['operation'] == 'open' else '回复已完成', result=result)
    except requests.HTTPError as exc:
        event('failure', '推理服务请求失败', safe_release=True)
        try:
            detail = exc.response.json().get('error', {}).get('message')
        except (ValueError, AttributeError):
            detail = None
        raise RuntimeError(detail or f'推理服务返回 HTTP {exc.response.status_code}') from None
    except Exception:
        event('failure', '模型任务执行失败', safe_release=True)
        raise
    finally:
        finished.set()
        monitor.join()
        lines = server_diagnostics()
        if lines:
            event('debug', '', lines=lines)

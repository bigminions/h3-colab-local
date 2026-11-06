"""Qwen-Image-2.1 ComfyUI orchestration and inference on Colab.

Supports both Qwen-Image-2.1 Turbo (Text-to-Image) and Qwen-Image-Edit (Instruction-based Image Editing),
utilizing community quantized GGUF weights via ComfyUI-GGUF and native safetensors formats.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path('/content/ComfyUI')
REPO_ID = 'abenzerps/Qwen-Image-2.1-Turbo-Quantized'
EDIT_REPO_ID = 'unsloth/Qwen-Image-Edit-2511-GGUF'
COMFY_ORG_IMAGE_REPO = 'Comfy-Org/Qwen-Image_ComfyUI'
COMFY_ORG_EDIT_REPO = 'Comfy-Org/Qwen-Image-Edit_ComfyUI'
GGUF_NODE_REPO = 'https://github.com/leejet/ComfyUI-GGUF.git'

DIFFUSION_MODELS = (
    'qwen-image-2.1-turbo-Q4_K_M.gguf',
    'qwen-image-2.1-turbo-fp8.safetensors',
    'qwen-image-2.1-turbo-int8_convrot.safetensors',
    'qwen-image-2.1-turbo-Q5_K_M.gguf',
    'qwen-image-2.1-turbo-Q8_0.gguf',
    'qwen-image-2.1-turbo-Q4_0.gguf',
    'qwen-image-2.1-turbo-Q6_K.gguf',
    'qwen-image-edit-2511-Q4_K_M.gguf',
    'qwen-image-edit-2511-Q5_K_M.gguf',
    'qwen-image-edit-2511-Q8_0.gguf',
    'qwen_image_edit_2511_fp8mixed.safetensors',
    'qwen_image_edit_fp8_e4m3fn.safetensors',
)

EDIT_MODELS = (
    'qwen-image-edit-2511-Q4_K_M.gguf',
    'qwen-image-edit-2511-Q5_K_M.gguf',
    'qwen-image-edit-2511-Q8_0.gguf',
    'qwen_image_edit_2511_fp8mixed.safetensors',
    'qwen_image_edit_fp8_e4m3fn.safetensors',
)

MODEL_ALIASES = {
    'qwen-image-2.1-UC-Q4_K_M.gguf': 'qwen-image-2.1-turbo-Q4_K_M.gguf',
    'qwen-image-2.1-UC-fp8.safetensors': 'qwen-image-2.1-turbo-fp8.safetensors',
    'qwen-image-2.1-UC-int8_convrot.safetensors': 'qwen-image-2.1-turbo-int8_convrot.safetensors',
    'qwen-image-2.1-UC-Q5_K_M.gguf': 'qwen-image-2.1-turbo-Q5_K_M.gguf',
    'qwen-image-2.1-UC-Q8_0.gguf': 'qwen-image-2.1-turbo-Q8_0.gguf',
    'qwen-image-2.1-UC-BF16.gguf': 'qwen-image-2.1-turbo-Q8_0.gguf',
}


def normalize_model_file(name: str | None, is_edit: bool = False) -> str:
    if not name:
        return 'qwen-image-edit-2511-Q4_K_M.gguf' if is_edit else 'qwen-image-2.1-turbo-Q4_K_M.gguf'
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    if name.startswith('qwen-image-2.1-UC-'):
        return name.replace('qwen-image-2.1-UC-', 'qwen-image-2.1-turbo-')
    return name


def is_edit_job(c: dict) -> bool:
    if not c.get('references'):
        return False
    model = str(c.get('model_file', '')).lower()
    if any(k in model for k in ('edit', '2511', '2509')):
        return True
    if c.get('edit_type') == 'instruction':
        return True
    if not any(k in model for k in ('turbo', 't2i')):
        return True
    return False


ENCODER_MODELS = {
    'int8_convrot': 'text_encoders/qwen3vl_8b_int8_convrot.safetensors',
    'bf16': 'text_encoders/qwen3vl_8b_bf16.safetensors',
}

VAE_MODEL = 'vae/qwen_image_2.1_vae_bf16.safetensors'

DIMENSIONS = {
    '1:1': (1024, 1024),
    '16:9': (1344, 768),
    '9:16': (768, 1344),
    '4:3': (1152, 864),
    '3:4': (864, 1152),
    '3:2': (1216, 832),
    '2:3': (832, 1216),
}

MODEL_CACHE = None
CURRENT_CONFIG = {}
EXPECTED_GPU = 'A100'


class PreserveRuntimeError(RuntimeError):
    pass


def event(stage, message, **extra):
    payload = {'stage': stage, 'message': message, **extra}
    print('QWEN_IMAGE_EVENT ' + json.dumps(payload, ensure_ascii=False), flush=True)


def files_for(c):
    if is_edit_job(c):
        raw_model = c.get('model_file', 'qwen-image-edit-2511-Q4_K_M.gguf')
        if not raw_model or 'turbo' in raw_model:
            model = 'qwen-image-edit-2511-Q4_K_M.gguf'
        else:
            model = normalize_model_file(raw_model, is_edit=True)
        if model.startswith('qwen_image_edit_'):
            model_entry = (COMFY_ORG_EDIT_REPO, 'split_files/diffusion_models/' + model, 'diffusion_models')
        else:
            model_entry = (EDIT_REPO_ID, model, 'diffusion_models')
        encoder_entry = (COMFY_ORG_IMAGE_REPO, 'split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors', 'text_encoders')
        vae_entry = (COMFY_ORG_IMAGE_REPO, 'split_files/vae/qwen_image_vae.safetensors', 'vae')
        return [model_entry, encoder_entry, vae_entry]

    model = normalize_model_file(c.get('model_file', 'qwen-image-2.1-turbo-Q4_K_M.gguf'))
    encoder_key = c.get('encoder', 'int8_convrot')
    encoder = ENCODER_MODELS.get(encoder_key, ENCODER_MODELS['int8_convrot'])
    return [
        (REPO_ID, model, 'diffusion_models'),
        (REPO_ID, encoder, 'text_encoders'),
        (REPO_ID, VAE_MODEL, 'vae'),
    ]


def normalize_edit_prompt(prompt: str) -> str:
    if not prompt:
        return prompt
    prompt = re.sub(r'(?<![A-Za-z0-9_])@?图片\s*(\d+)', r'Picture \1', prompt)
    prompt = re.sub(r'(?<![A-Za-z0-9_])@?图\s*(\d+)', r'Picture \1', prompt)
    prompt = re.sub(r'(?<![A-Za-z0-9_])@?[pP]icture\s*(\d+)', r'Picture \1', prompt)
    prompt = re.sub(r'(?<![A-Za-z0-9_])@?[iI]mage\s*(\d+)', r'Picture \1', prompt)
    return prompt

def build_graph(c):
    def node(cls, **inputs):
        return {'class_type': cls, 'inputs': inputs}

    aspect_ratio = c.get('aspect_ratio', '1:1')
    width, height = DIMENSIONS.get(aspect_ratio, (1024, 1024))
    prompt = c.get('prompt', '')
    negative_prompt = c.get('negative_prompt', '')
    seed = c.get('seed', 0)
    steps = c.get('steps', 8)
    cfg = c.get('cfg', 1.0)
    denoise = c.get('denoise', 0.75)
    job_id = c.get('job_id', 'preview')
    refs = c.get('references', [])

    if is_edit_job(c):
        raw_model = c.get('model_file', 'qwen-image-edit-2511-Q4_K_M.gguf')
        if not raw_model or 'turbo' in raw_model:
            model_file = 'qwen-image-edit-2511-Q4_K_M.gguf'
        else:
            model_file = normalize_model_file(raw_model, is_edit=True)
        is_gguf = model_file.endswith('.gguf')

        encoder_file = 'qwen_2.5_vl_7b_fp8_scaled.safetensors'
        vae_file = 'qwen_image_vae.safetensors'

        edit_steps = steps if steps > 8 else 20
        edit_cfg = cfg if cfg > 1.01 else 4.0
        sorted_refs = sorted(refs, key=lambda r: r.get('slot', 1)) if refs else []
        ref_id = sorted_refs[0]['image_id'] if sorted_refs else 'input.png'

        g = {}
        if is_gguf:
            g['1'] = node('UnetLoaderGGUF', unet_name=model_file)
        else:
            g['1'] = node('UNETLoader', unet_name=model_file, weight_dtype='default')

        g['2'] = node('CLIPLoader', clip_name=encoder_file, type='qwen_image', device='default')
        g['3'] = node('VAELoader', vae_name=vae_file)

        g['20'] = node('LoadImage', image=ref_id)
        g['21'] = node('ImageScale', image=['20', 0], upscale_method='bicubic', width=width, height=height, crop='center')

        norm_pos_prompt = normalize_edit_prompt(prompt)
        norm_neg_prompt = normalize_edit_prompt(negative_prompt)

        inputs_pos = {'clip': ['2', 0], 'prompt': norm_pos_prompt, 'image1': ['21', 0], 'vae': ['3', 0]}
        inputs_neg = {'clip': ['2', 0], 'prompt': norm_neg_prompt, 'image1': ['21', 0], 'vae': ['3', 0]}

        if len(sorted_refs) > 1:
            g['22'] = node('LoadImage', image=sorted_refs[1]['image_id'])
            g['23'] = node('ImageScale', image=['22', 0], upscale_method='bicubic', width=width, height=height, crop='center')
            inputs_pos['image2'] = ['23', 0]
            inputs_neg['image2'] = ['23', 0]
        if len(sorted_refs) > 2:
            g['24'] = node('LoadImage', image=sorted_refs[2]['image_id'])
            g['25'] = node('ImageScale', image=['24', 0], upscale_method='bicubic', width=width, height=height, crop='center')
            inputs_pos['image3'] = ['25', 0]
            inputs_neg['image3'] = ['25', 0]

        g['4'] = node('TextEncodeQwenImageEditPlus', **inputs_pos)
        g['5'] = node('TextEncodeQwenImageEditPlus', **inputs_neg)
        g['40'] = node('FluxKontextMultiReferenceLatentMethod', conditioning=['4', 0], reference_latents_method='index_timestep_zero')
        g['41'] = node('FluxKontextMultiReferenceLatentMethod', conditioning=['5', 0], reference_latents_method='index_timestep_zero')
        g['6'] = node('VAEEncode', pixels=['21', 0], vae=['3', 0])

        g['30'] = node('ModelSamplingAuraFlow', model=['1', 0], shift=3.0)
        g['31'] = node('CFGNorm', model=['30', 0], strength=1.0)

        g['7'] = node('KSampler',
                      model=['31', 0],
                      positive=['40', 0],
                      negative=['41', 0],
                      latent_image=['6', 0],
                      seed=seed,
                      steps=edit_steps,
                      cfg=edit_cfg,
                      sampler_name='euler',
                      scheduler='simple',
                      denoise=1.0)

        g['8'] = node('VAEDecode', samples=['7', 0], vae=['3', 0])
        g['9'] = node('SaveImage', images=['8', 0], filename_prefix='QwenImage/local_' + job_id)
        return g

    model_file = normalize_model_file(c.get('model_file', 'qwen-image-2.1-turbo-Q4_K_M.gguf'))
    is_gguf = model_file.endswith('.gguf')

    encoder_key = c.get('encoder', 'int8_convrot')
    encoder_file = Path(ENCODER_MODELS.get(encoder_key, ENCODER_MODELS['int8_convrot'])).name
    vae_file = Path(VAE_MODEL).name

    g = {}
    if is_gguf:
        g['1'] = node('UnetLoaderGGUF', unet_name=model_file)
    else:
        g['1'] = node('UNETLoader', unet_name=model_file, weight_dtype='default')

    g['2'] = node('CLIPLoader', clip_name=encoder_file, type='qwen_image', device='default')
    g['3'] = node('VAELoader', vae_name=vae_file)
    g['4'] = node('CLIPTextEncode', clip=['2', 0], text=prompt)
    g['5'] = node('CLIPTextEncode', clip=['2', 0], text=negative_prompt)

    if refs:
        g['20'] = node('LoadImage', image=refs[0]['image_id'])
        g['21'] = node('ImageScale', image=['20', 0], upscale_method='bicubic', width=width, height=height, crop='center')
        g['6'] = node('VAEEncode', pixels=['21', 0], vae=['3', 0])
        effective_denoise = denoise
    else:
        g['6'] = node('EmptyLatentImage', width=width, height=height, batch_size=1)
        effective_denoise = 1.0

    TURBO_SIGMAS = '1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0'

    g['10'] = node('RandomNoise', noise_seed=seed)
    g['11'] = node('KSamplerSelect', sampler_name='euler')

    if steps == 8 and effective_denoise >= 1.0:
        g['12'] = node('ManualSigmas', sigmas=TURBO_SIGMAS)
        sigmas_ref = ['12', 0]
    elif steps == 8 and effective_denoise < 1.0:
        g['12'] = node('ManualSigmas', sigmas=TURBO_SIGMAS)
        g['14'] = node('SplitSigmasDenoise', sigmas=['12', 0], denoise=effective_denoise)
        sigmas_ref = ['14', 1]
    else:
        g['12'] = node('BasicScheduler', model=['1', 0], scheduler='simple', steps=steps, denoise=effective_denoise)
        sigmas_ref = ['12', 0]

    if cfg <= 1.01 or not negative_prompt:
        g['13'] = node('BasicGuider', model=['1', 0], conditioning=['4', 0])
    else:
        g['13'] = node('CFGGuider', model=['1', 0], positive=['4', 0], negative=['5', 0], cfg=cfg)

    g['7'] = node('SamplerCustomAdvanced',
                  noise=['10', 0],
                  guider=['13', 0],
                  sampler=['11', 0],
                  sigmas=sigmas_ref,
                  latent_image=['6', 0])

    g['8'] = node('VAEDecode', samples=['7', 0], vae=['3', 0])
    g['9'] = node('SaveImage', images=['8', 0], filename_prefix='QwenImage/local_' + job_id)
    return g


def result_for(path):
    from PIL import Image
    path = Path(path).resolve()
    if not path.is_relative_to((ROOT / 'output').resolve()):
        raise RuntimeError('输出路径不合法')
    with Image.open(str(path)) as img:
        width, height = img.size
    metadata = dict(width=width, height=height)
    digest = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
    result = dict(path=str(path), size=path.stat().st_size, sha256=digest, **metadata)
    event('result', '图片已生成，准备保存结果', result=result)
    return result


def prepare_model(repo, filename, category, downloader, cache=None, loads=None):
    target = ROOT / 'models' / category / Path(filename).name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        event('prepare', '运行时本地复用模型文件：%s' % target.name)
        if loads is not None:
            loads.append({'filename': target.name, 'source': 'local', 'bytes': target.stat().st_size})
        return target
    event('prepare', '从 Hugging Face 获取模型文件：%s (%s)' % (Path(filename).name, repo))
    started = time.monotonic()
    downloader(repo_id=repo, filename=filename, local_dir=str(target.parent))
    nested = target.parent / filename
    if nested.exists() and nested != target:
        nested.replace(target)
    elapsed = max(time.monotonic() - started, 0.001)
    size = target.stat().st_size if target.exists() else 0
    event('prepare', '模型文件准备就绪：%s（%.1f 秒）' % (target.name, elapsed))
    if loads is not None:
        loads.append({'filename': target.name, 'source': 'huggingface', 'bytes': size, 'seconds': elapsed})
    return target


def prepare():
    global CURRENT_CONFIG
    c = CURRENT_CONFIG
    gpu_output = ''
    try:
        gpu_output = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'],
            text=True
        ).strip()
        name, memory = gpu_output.splitlines()[0].rsplit(',', 1)
        event('prepare', '检查 GPU 资源：' + name.strip(), gpu_info={'name': name.strip(), 'memory_mib': int(memory.strip())})
    except Exception:
        event('prepare', '未检测到 nvidia-smi，使用默认配置')

    if not ROOT.exists():
        event('prepare', '克隆 ComfyUI 核心代码…')
        subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/Comfy-Org/ComfyUI.git', str(ROOT)], check=True)

    gguf_node_dir = ROOT / 'custom_nodes' / 'ComfyUI-GGUF'
    if not gguf_node_dir.exists():
        event('prepare', '安装 ComfyUI-GGUF 扩展以支持 Qwen-Image GGUF 量化…')
        subprocess.run(['git', 'clone', '--depth', '1', GGUF_NODE_REPO, str(gguf_node_dir)], check=True)

    ready_marker = ROOT / '.qwen-image-ready'
    if not ready_marker.exists():
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(ROOT / 'requirements.txt'), 'gguf', 'huggingface_hub', 'pillow'], check=True)
        ready_marker.touch()

    from huggingface_hub import hf_hub_download
    for repo, filename, category in files_for(c):
        prepare_model(repo, filename, category, hf_hub_download, MODEL_CACHE)

    import requests
    serving = False
    try:
        requests.get('http://127.0.0.1:8188/object_info', timeout=3).raise_for_status()
        serving = True
    except Exception:
        serving = False

    if not serving:
        start_comfy()
    event('prepare', 'Qwen-Image 模型与服务已就绪')


def start_comfy():
    import requests
    args = [sys.executable, str(ROOT / 'main.py'), '--listen', '127.0.0.1', '--port', '8188']
    log_path = Path('/content/qwen-image-comfy.log')
    with open(log_path, 'a') as log:
        subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
    for _ in range(120):
        try:
            requests.get('http://127.0.0.1:8188/object_info', timeout=3).raise_for_status()
            return
        except requests.RequestException:
            time.sleep(2)
    raise RuntimeError('ComfyUI 启动超时，请检查 /content/qwen-image-comfy.log')


def generate(c):
    import requests
    base = 'http://127.0.0.1:8188'
    queue = requests.get(base + '/queue', timeout=20).json()
    if queue.get('queue_running') or queue.get('queue_pending'):
        raise PreserveRuntimeError('ComfyUI 仍有任务在排队或运行中，请等待完成后提交')

    candidates = sorted((ROOT / 'output' / 'QwenImage').glob('local_' + c['job_id'] + '*.png'))
    if candidates:
        event('generate', '找到已完成的生成产物，直接取回')
        return result_for(candidates[-1])

    graph = build_graph(c)
    event('generate', '向 ComfyUI 提交生图工作流…')
    res = requests.post(base + '/prompt', json={'prompt': graph}, timeout=30)
    if not res.ok:
        err_msg = f'ComfyUI 拒绝任务请求 ({res.status_code})'
        try:
            err_json = res.json()
            node_errors = err_json.get('node_errors', {})
            details = []
            for nid, nerr in node_errors.items():
                for e in nerr.get('errors', []):
                    details.append(f"Node {nid} ({nerr.get('class_type', '')}): {e.get('message', '')}")
            if details:
                err_msg += ': ' + '; '.join(details)
            else:
                err_msg += ': ' + json.dumps(err_json, ensure_ascii=False)
        except Exception:
            err_msg += ': ' + res.text
        raise RuntimeError(err_msg)
    prompt_id = res.json().get('prompt_id')
    event('generate', '任务已进入 ComfyUI 队列，正在生成…', prompt_id=prompt_id)

    started = time.monotonic()
    timeout = 1800
    while time.monotonic() - started < timeout:
        time.sleep(2)
        try:
            history = requests.get(base + '/history/' + prompt_id, timeout=10).json()
        except Exception:
            continue
        if prompt_id in history:
            outputs = history[prompt_id].get('outputs', {})
            save_node = outputs.get('9', {})
            images = save_node.get('images', [])
            if images:
                img_info = images[0]
                subfolder = img_info.get('subfolder', 'QwenImage')
                filename = img_info.get('filename')
                output_path = ROOT / 'output' / subfolder / filename
                if output_path.exists():
                    return result_for(output_path)
            status = history[prompt_id].get('status', {})
            if status.get('status_str') == 'error':
                messages = status.get('messages', [])
                err = '; '.join(str(m) for m in messages) or 'ComfyUI 内部生图错误'
                raise RuntimeError(err)

    raise RuntimeError('生图任务超时（超过 30 分钟）')


def run(c):
    global EXPECTED_GPU, CURRENT_CONFIG
    CURRENT_CONFIG = c
    EXPECTED_GPU = c.get('gpu', 'A100')
    stop = threading.Event()

    def heartbeat():
        started = time.monotonic()
        while not stop.wait(15):
            event('heartbeat', 'Qwen-Image 正在处理中，已用时 %d 秒' % (time.monotonic() - started))

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        started = time.monotonic()
        prepare()
        event('prepare', '环境与模型准备完成，用时 %.1f 秒' % (time.monotonic() - started))
        return generate(c)
    except Exception as exc:
        event('failure', str(exc), safe_release=not isinstance(exc, PreserveRuntimeError))
        raise
    finally:
        stop.set()

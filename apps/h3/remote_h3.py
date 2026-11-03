"""Runs inside Colab. No public server or tunnel is exposed."""
import hashlib
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path('/content/ComfyUI')
EXPECTED_GPU = None
CURRENT_CONFIG = {}
MODEL_CACHE = None
UNET = 'minimax_h3_fl2va_int8_convrot.safetensors'
REF_UNET = 'minimax_h3_ref2va_pruned_int8_convrot.safetensors'
# Community H3-compatible conditioning encoder. This is not a replacement
# for the H3 diffusion model and does not guarantee unrestricted video output.
CLIP = 'qwen3vl_32b_h3_ultra_uncensored_heretic_int8_convrot.safetensors'
CLIP_REPO = 'ethanfel/Qwen3-VL-32B-Ultra-Heretic-H3-ComfyUI-INT8-ConvRot'
CLIP_REVISION = 'e8967f6a39ea5b4939a1aff81be3e8706490c0e8'
CLIP_SIZE = 26363476151
CLIP_SHA256 = 'd84547412144b7c50a6ec77437a889b869d3ace88da77ef1775d3d2a4901c192'
TAIL = 'qwen3vl_32b_h3_generation_tail_50_63_int8_convrot.safetensors'
TAIL_SIZE = 7609128707
TAIL_SHA256 = 'b5bb9bb8dc87cf11cbee241a2d95d6d42fe52cf695ed26c093ac321f31160b20'
TEXTGEN_REVISION = '0d8504cafeeef15e5bcd4a8d85c089737f156bcd'
TEXTGEN_FILES = {
    '__init__.py': '088a55db8c047e2dc4dcd3fd80567c2d9da26266b46a64027d52de6a270cf372',
    'nodes.py': 'bedb5c2b27bb52f2cb47c0b87ec6f2f5cf77d3434f98af707303626d4be4810f',
    'hybrid_tail.py': 'bf14591066e3a3d4b186e21eae778084b95302951ada2b1492e770a76f4ac920',
}
VAE = 'minimax_h3_video_vae_int8_convrot.safetensors'
AVAE = 'minimax_h3_audio_vae_fp32.safetensors'
PDD = 'MiniMax-H3-FL2VA-Acc-8Step_comfy.safetensors'
REF_PDD = 'MiniMax-H3-Ref2VA-Acc-8Step_pruned_comfy.safetensors'
REF_PDD_REVISION = '137541ff66fbe22d35beb73e09bca5d663b70cc5'
REF_PDD_SIZE = 1725921392
REF_PDD_SHA256 = '6f18e1c2eccb14b37322607730f26b16bf1169b56cd098ea006cffaec43d1e39'
PEOPLE = 'h3-realism-people-t2v-i2v-r2v.safetensors'
VEDA_REVISION = '67e4eb5541b648555563144401ebaba832bd73ec'
VEDA_FOLDER = 'Veda-on-ComfyUI'
VEDA_PREDICTORS = {
    't2va': dict(repo='Veda-Sparse/Minimax-H3-T2VA-Veda-8NFE-600Step-Preview',
                 filename='minimax_h3_t2va_veda_8nfe_600step_preview_fp8.safetensors',
                 revision='9a1fd3a41b4a754a7886e64e82edbddf599fd1bd', size=275415648,
                 sha256='2a8d8845c5342756a2781e8e69563940e4bb573c9a40ebb534915ff8fd76573a'),
    'r2va': dict(repo='Veda-Sparse/Minimax-H3-R2VA-Veda-Preview',
                 filename='minimax_h3_r2va_veda_preview_fp8.safetensors',
                 revision='1598b407905b7c0af85aa0c13acdfa6f01ff2982', size=275415752,
                 sha256='23edace948693772be33d5cc945d302d2b037a29bd44bd57071bfbdd8b02517d'),
}
# Native H3 requires 32px alignment. Keep the proven landscape canvas and
# never exceed its pixel budget when selecting another creator format.
DIMENSIONS = {
    '768p': {'16:9': (1344, 768), '9:16': (768, 1344), '1:1': (768, 768), '4:5': (768, 960), '3:4': (768, 1024)},
    '480p': {'16:9': (864, 480), '9:16': (480, 864), '1:1': (480, 480), '4:5': (480, 608), '3:4': (480, 640)},
}
class PreserveRuntimeError(RuntimeError):
    pass
FILES = [
    ('Comfy-Org/MiniMax-H3', 'diffusion_models/' + UNET, 'diffusion_models'),
    (CLIP_REPO, CLIP, 'text_encoders'),
    ('Comfy-Org/MiniMax-H3', 'vae/' + VAE, 'vae'),
    ('Comfy-Org/MiniMax-H3', 'vae/' + AVAE, 'vae'),
    ('Kijai/MiniMax-H3-experimental', 'loras/' + PDD, 'loras'),
    ('fal/MiniMax-H3-Realism-People-LoRA', PEOPLE, 'loras'),
]


# Pinned public assets for the selectable original-model path.
MODEL_IDENTITIES = {
    ('Comfy-Org/MiniMax-H3', 'diffusion_models/minimax_h3_fl2va_bf16.safetensors'): {
        "revision": 'e5eb578a89295337b8ff433a035929ce0279e0b6',
        "size": 66280487368,
        "sha256": '907d4add438438ec1544f5240c3b38532ed934fe6be75677a6bbda2a6fdd6182',
    },
    ('Comfy-Org/MiniMax-H3', 'diffusion_models/minimax_h3_ref2va_bf16.safetensors'): {
        "revision": 'e5eb578a89295337b8ff433a035929ce0279e0b6',
        "size": 66280487368,
        "sha256": 'e32c54c1a7b4f5f397f195cea267ccb18806303bb665678c4bee60953bdf3026',
    },
    ('Comfy-Org/MiniMax-H3', 'text_encoders/qwen3vl_32b_minimax_h3_bf16.safetensors'): {
        "revision": 'e5eb578a89295337b8ff433a035929ce0279e0b6',
        "size": 51506295256,
        "sha256": '600d567f6a9629c8574e8e7041b199bdd9c59a986afa7906910a81919610607d',
    },
    ('Comfy-Org/MiniMax-H3', 'text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors'): {
        "revision": 'e5eb578a89295337b8ff433a035929ce0279e0b6',
        "size": 27141342152,
        "sha256": 'bc2ced0fbea64757fa9acddccfc0b3f4819d1dcf1da6c124d690d368be283923',
    },
    ('Comfy-Org/MiniMax-H3', 'vae/minimax_h3_video_vae_fp16.safetensors'): {
        "revision": 'e5eb578a89295337b8ff433a035929ce0279e0b6',
        "size": 5207808496,
        "sha256": '7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522',
    },
    ('Kijai/MiniMax-H3-experimental', 'loras/MiniMax-H3-Ref2VA-Acc-8Step_comfy.safetensors'): {
        "revision": '137541ff66fbe22d35beb73e09bca5d663b70cc5',
        "size": 1723713944,
        "sha256": 'd7cd9480c000d9fc85a4beb7e50fd961d89aea8e2efd5ffefb200ccdcdaf7460',
    },
    ('ethanfel/Qwen3-VL-32B-Ultra-Heretic-H3-ComfyUI-INT8-ConvRot', 'qwen3vl_32b_h3_instruct_generation_tail_50_63_bf16.safetensors'): {
        "revision": 'e8967f6a39ea5b4939a1aff81be3e8706490c0e8',
        "size": 15208606744,
        "sha256": 'c3bd172983077787f4c29a2113ff7ce1964778cbec51401048375dc9141b5899',
    },
    ('ethanfel/Qwen3-VL-32B-Ultra-Heretic-H3-ComfyUI-INT8-ConvRot', 'qwen3vl_32b_h3_instruct_generation_tail_50_63_int8_convrot.safetensors'): {
        "revision": 'e8967f6a39ea5b4939a1aff81be3e8706490c0e8',
        "size": 7609128659,
        "sha256": '8af81d26e9cd62b3f45b42df92f33247459e5c9e46c82752a26632ad9540e0d4',
    },
}

REFERENCE_ROLES = {'character': 'Character identity', 'scene': 'Scene and environment',
    'clothing': 'Clothing', 'style': 'Visual style', 'product': 'Product appearance', 'other': 'Reference'}


def uses_reference_model(c):
    return bool(c.get('characters') or c.get('audio_references')) or any(r.get('frame', 'reference') == 'reference' for r in c.get('references', []))


def ordered_images(c):
    refs = c.get('references', [])
    # FL2VA tokenizes first then last; Ref2VA tokenizes by input slot.
    return sorted(refs, key=(lambda r: r['slot']) if uses_reference_model(c)
                  else (lambda r: 0 if r.get('frame') == 'first' else 1))


def reference_prompt(c):
    """Keep UI slots stable while mapping to the native, contiguous Picture tags."""
    refs = ordered_images(c)
    mapping = {r['slot']: index + 1 for index, r in enumerate(refs)}
    pattern = r'<Picture\s+(\d+)>|(?<![A-Za-z0-9_@])@图片\s*(\d+)'
    def convert(text):
        def replace(match):
            slot = int(match.group(1) or match.group(2))
            if slot not in mapping:
                raise ValueError('提示词引用了未上传的图片' + str(slot))
            return '<Picture ' + str(mapping[slot]) + '>'
        return re.sub(pattern, replace, text, flags=re.I)
    descriptions = [f'<Picture {index + 1}>: {REFERENCE_ROLES[r["role"]]}. {convert(r.get("description", ""))}'
                    for index, r in enumerate(refs)]
    for index, ref in enumerate(refs):
        if ref.get('frame') in ('first', 'last'):
            descriptions[index] += ' Use this composition as the ' + ref['frame'] + ' frame of the video.'
    bindings = []
    prompt = convert(c['prompt'])
    chars = c.get('characters', [])
    for index, character in enumerate(chars):
        subject = '<Subject ' + str(index + 1) + '>'
        pictures = ['<Picture ' + str(i + 1) + '>' for i, r in enumerate(refs) if r['image_id'] in character['image_ids']]
        if not pictures:
            raise ValueError('角色参考图未加入本次输入：' + character['name'])
        bindings.append(subject + ' is the character named ' + character['name'] + ' from ' + ', '.join(pictures) + '. ' + character.get('description', ''))
        bindings.append('When ' + subject + ' speaks, keep the same speaker identity throughout the video. ' + character.get('voice', ''))
        pattern = r'(?<![A-Za-z0-9_@])@' + re.escape(character['name']) + r'(?![\w-])'
        prompt = re.sub(pattern, subject, prompt)
        for audio_index, audio in enumerate(c.get('audio_references', [])):
            if audio['character_id'] == character['id']:
                bindings.append('<Audio ' + str(audio_index + 1) + '> is a voice-timbre reference for ' + subject + '. Use the timbre and vocal identity, not the source words or background music. All audio samples bound to this subject describe the same voice.')
    return ('Reference assignments:\n' + '\n'.join(descriptions + bindings) +
            '\nUse each reference for its assigned purpose.\nScene, action and audio:\n' + prompt)


def effective_config(c):
    if c.get('performance', True):
        return c
    return dict(c, encoder='official_bf16', quantized_model=False, quantized_vae=False,
                mode='base', veda=False, people=False)


def encoder_files(c):
    choice = effective_config(c).get('encoder', 'heretic_int8')
    if choice == 'heretic_int8':
        return (CLIP_REPO, CLIP, 'text_encoders'), (CLIP_REPO, TAIL, 'text_encoders')
    precision = 'bf16' if choice == 'official_bf16' else 'int8_convrot'
    return (('Comfy-Org/MiniMax-H3', 'text_encoders/qwen3vl_32b_minimax_h3_' + precision + '.safetensors', 'text_encoders'),
            (CLIP_REPO, 'qwen3vl_32b_h3_instruct_generation_tail_50_63_' + precision + '.safetensors', 'text_encoders'))


def model_files(c):
    c = effective_config(c)
    refs = uses_reference_model(c)
    unet = (REF_UNET if refs else UNET) if c.get('quantized_model', True) else (
        'minimax_h3_ref2va_bf16.safetensors' if refs else 'minimax_h3_fl2va_bf16.safetensors')
    vae = VAE if c.get('quantized_vae', True) else 'minimax_h3_video_vae_fp16.safetensors'
    pdd = (REF_PDD if c.get('quantized_model', True) else 'MiniMax-H3-Ref2VA-Acc-8Step_comfy.safetensors') if refs else PDD
    return unet, vae, pdd


CURRENT_PROMPT_PLAN = None


def files_for(c):
    c = effective_config(c)
    unet, vae, pdd = model_files(c)
    encoder, tail = encoder_files(c)
    files = [('Comfy-Org/MiniMax-H3', 'diffusion_models/' + unet, 'diffusion_models'),
             encoder, ('Comfy-Org/MiniMax-H3', 'vae/' + vae, 'vae'), FILES[3]]
    if c.get('mode') == 'pdd':
        files.append(('Kijai/MiniMax-H3-experimental', 'loras/' + pdd, 'loras'))
    if c.get('people') and not uses_reference_model(c):
        files.append(FILES[5])
    if c.get('prompt_processing') == 'faithful':
        files.append(tail)
    return files


def pipeline_files(c):
    c = effective_config(c)
    all_files = files_for(c)
    if c.get('prompt_processing') != 'faithful':
        return [], all_files
    encoder, tail = encoder_files(c)
    qwen_files = [encoder, tail]
    other_files = [f for f in all_files if f not in qwen_files]
    return qwen_files, other_files


def event(stage, message, **extra):
    print('H3_EVENT ' + json.dumps(dict(stage=stage, message=message, **extra), ensure_ascii=False), flush=True)


def verify_clip(path):
    """Check the pinned encoder once; reuse validation while the file is unchanged."""
    verify_weight(path, CLIP_SIZE, CLIP_SHA256, '社区编码器')


def write_verified_stamp(path, sha256):
    stat = path.stat()
    stamp = path.with_name(path.name + '.verified.json')
    stamp.write_text(json.dumps(dict(sha256=sha256, size=stat.st_size, mtime_ns=stat.st_mtime_ns)))


def verify_weight(path, size, sha256, label):
    stat = path.stat()
    if stat.st_size != size:
        raise RuntimeError(label + '文件大小不符，请检查或重新下载：' + str(path))
    stamp = path.with_name(path.name + '.verified.json')
    expected = dict(sha256=sha256, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    try:
        if json.loads(stamp.read_text()) == expected:
            return
    except (OSError, ValueError):
        pass
    event('prepare', '校验 ' + label + ' SHA-256（首次加载）')
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != sha256:
        raise RuntimeError(label + ' SHA-256 不符，停止加载：' + str(path))
    stamp.write_text(json.dumps(expected))


def download_model(downloader, **options):
    try:
        return downloader(**options)
    except Exception as exc:
        detail = str(exc).lower()
        if '429' not in detail or not any(term in detail for term in ('cas', 'xet', 'xorbs')):
            raise
        from huggingface_hub import constants
        constants.HF_HUB_DISABLE_XET = True
        os.environ['HF_HUB_DISABLE_XET'] = '1'
        event('prepare', 'Xet 分块下载被限流，10 秒后改用 HTTP 下载当前未完成模型')
        time.sleep(10)
        # A failed Xet reconstruction can leave noncontiguous bytes. Restart
        # only this unfinished file before using sequential HTTP transfer.
        return downloader(force_download=True, **options)


def cache_backend(cache):
    if cache is None:
        return '关闭'
    return '原生挂载' if type(cache).__name__ == 'MountedDriveModelCache' else 'Drive API'


def model_load_summary(loads, seconds, sync_seconds):
    parts = []
    for source, label in [('drive', 'Drive 恢复'), ('huggingface', 'Hugging Face 获取'), ('local', '运行时本地复用')]:
        selected = [item for item in loads if item['source'] == source]
        parts.append('%s %d 个 / %.2f GB' % (label, len(selected), sum(item['bytes'] for item in selected) / 1e9))
    message = ('模型文件准备汇总：' + '；'.join(parts) +
          ('；总计 %.1f 秒（含版本检查、校验、缓存保存及同步）；其中缓存保存检查/写入 %.1f 秒，同步 %.1f 秒。'
          '不含 Veda、依赖安装和 GPU 权重加载。' %
          (seconds, sum(item['store_seconds'] for item in loads), sync_seconds)))
    event('prepare', message, model_load_summary=message,
          timings={'model_files_seconds': round(seconds, 3), 'drive_sync_seconds': round(sync_seconds, 3)})


def prepare_model(repo, filename, category, downloader, cache=None, loads=None, allow_store=False):
    load_started = time.monotonic()
    backend = cache_backend(cache)
    cache_status = 'disabled' if cache is None else 'standby'
    folder = ROOT / 'models' / category
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / Path(filename).name
    identity = None
    pinned = MODEL_IDENTITIES.get((repo, filename))
    revision = {'revision': pinned['revision']} if pinned else ({'revision': CLIP_REVISION} if filename in (CLIP, TAIL) else {})
    if filename == 'loras/' + REF_PDD:
        revision = {'revision': REF_PDD_REVISION}
    if cache is not None:
        from drive_cache_remote import model_identity
        try:
            identity = model_identity(repo, filename, revision.get('revision'))
        except Exception as exc:
            event('prepare', '无法确认 Drive 缓存的源版本，仅使用模型源：' + str(exc))
            cache = None
            cache_status = 'identity_failed'
        if identity:
            revision = {'revision': identity['revision']}
            if pinned and any(identity[k] != pinned[k] for k in ('size', 'sha256')):
                raise RuntimeError('模型源版本与固定校验值不符：' + filename)
            if filename == CLIP and (identity['size'] != CLIP_SIZE or identity['sha256'] != CLIP_SHA256):
                raise RuntimeError('编码器源版本与固定的校验值不符')
            if filename == TAIL and (identity['size'] != TAIL_SIZE or identity['sha256'] != TAIL_SHA256):
                raise RuntimeError('Qwen 生成尾部源版本与固定校验值不符')
            if filename == 'loras/' + REF_PDD and (identity['size'] != REF_PDD_SIZE or identity['sha256'] != REF_PDD_SHA256):
                raise RuntimeError('参考图 PDD 源版本与固定的校验值不符')
    existing = target.exists() and target.stat().st_size > 1024 * 1024
    if existing and identity:
        existing = target.stat().st_size == identity['size']
    restored = False
    if existing:
        event('prepare', '模型来源：运行时本地复用，不计为 Drive 命中：' + target.name)
        if cache is not None:
            cache_status = 'local_reuse'
    if not existing:
        hf_failed = False
        hf_exc = None
        started = time.monotonic()
        try:
            event('prepare', '模型来源（优先）：Hugging Face 下载：%s' % target.name)
            source = Path(download_model(downloader, repo_id=repo, filename=filename, local_dir=str(folder), **revision))
            if source != target:
                if target.is_symlink():
                    target.unlink()
                target.symlink_to(source)
            elapsed = max(time.monotonic() - started, .001)
            metric = dict(operation='Hugging Face 下载', filename=target.name,
                          bytes=target.stat().st_size, seconds=round(elapsed, 3),
                          mb_per_second=round(target.stat().st_size / 1e6 / elapsed, 2))
            event('prepare', '模型下载完成：%s，%.1f 秒，%.1f MB/s' %
                  (target.name, elapsed, metric['mb_per_second']), cache_metric=metric)
            if cache_status != 'identity_failed':
                cache_status = 'disabled' if cache is None else 'standby'
        except Exception as exc:
            hf_failed = True
            hf_exc = exc
            event('prepare', 'Hugging Face 获取失败（%s），尝试 Drive 备用缓存：%s' % (str(exc), target.name))

        if hf_failed:
            if cache is not None:
                try:
                    event('prepare', '检查 Drive 备用缓存（%s）：%s' % (backend, target.name))
                    restored = cache.restore(target, identity)
                    if restored and identity and identity.get('sha256'):
                        write_verified_stamp(target, identity['sha256'])
                    cache_status = 'hit' if restored else 'miss'
                    if restored:
                        event('prepare', 'Drive 备用缓存命中，已成功恢复并校验：' + target.name)
                    else:
                        event('prepare', 'Drive 备用缓存未找到模型备份：' + target.name)
                except Exception as exc:
                    cache_status = 'read_failed'
                    event('prepare', 'Drive 备用缓存读取失败：' + str(exc))
            else:
                cache_status = 'disabled'

            if not restored:
                detail = ('未启用 Drive 缓存' if cache_status == 'disabled'
                          else 'Drive 备用缓存未找到备份' if cache_status == 'miss'
                          else 'Drive 缓存读取失败' if cache_status == 'read_failed'
                          else '源版本确认失败')
                raise RuntimeError(
                    '模型 %s 从 Hugging Face 下载失败（%s），且 %s。'
                    '可在“账号与连接”中点击【自动备份模型到 Drive】申请低成本算力完成离线备份。' %
                    (target.name, str(hf_exc), detail)
                )

    acquired = time.monotonic()
    if pinned:
        verify_weight(target, pinned['size'], pinned['sha256'], Path(filename).name)
    elif filename == CLIP:
        verify_clip(target)
    elif filename == TAIL:
        verify_weight(target, TAIL_SIZE, TAIL_SHA256, 'Qwen 生成尾部')
    elif filename == 'loras/' + REF_PDD:
        verify_weight(target, REF_PDD_SIZE, REF_PDD_SHA256, '参考图 PDD')
    verified = time.monotonic()
    store_status = 'not_needed'
    if allow_store and cache is not None and not restored:
        store_status = 'checked'
        try:
            cache.store(target, identity)
        except Exception as exc:
            store_status = 'failed'
            event('prepare', 'Drive 模型保存失败，本次继续使用本地模型：' + str(exc))
    finished = time.monotonic()
    source = 'local' if existing else 'drive' if restored else 'huggingface'
    metric = dict(operation='模型文件准备', filename=target.name, source=source,
                  repo=repo, revision=revision.get('revision'),
                  drive_backend=backend, cache_status=cache_status, store_status=store_status,
                  bytes=target.stat().st_size, seconds=round(finished - load_started, 3),
                  acquire_seconds=round(acquired - load_started, 3),
                  verify_seconds=round(verified - acquired, 3),
                  store_seconds=round(finished - verified, 3))
    if loads is not None:
        loads.append(metric)
    status_label = {
        'disabled': '未启用',
        'standby': '备用就绪（已走 HF）',
        'hit': '命中（HF 失败后已从 Drive 恢复）',
        'miss': '未命中',
        'local_reuse': '本地复用，未读取外部源',
        'read_failed': '读取失败，已回退',
        'identity_failed': '源版本确认失败，已跳过'
    }.get(cache_status, cache_status)
    event('prepare', '模型文件就绪：%s；来源=%s；Drive=%s / %s；大小 %.2f GB；获取（含版本检查）%.1f 秒；'
          '额外校验 %.1f 秒；缓存保存检查/写入 %.1f 秒（%s）；合计 %.1f 秒' %
          (target.name, {'local': '运行时本地', 'drive': 'Drive', 'huggingface': 'Hugging Face'}[source],
           backend, status_label,
           metric['bytes'] / 1e9, metric['acquire_seconds'], metric['verify_seconds'],
           metric['store_seconds'], {'not_needed': '未执行', 'checked': '检查完成，挂载写入以同步结果为准',
                                     'failed': '失败'}[store_status], metric['seconds']), model_load=metric)
    return target


def prepare_textgen():
    import requests
    folder = ROOT / '.h3-textgen'
    folder.mkdir(exist_ok=True)
    for name, expected in TEXTGEN_FILES.items():
        path = folder / name
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
            continue
        url = 'https://raw.githubusercontent.com/ethanfel/ComfyUI-H3-Qwen3VL-TextGen/' + TEXTGEN_REVISION + '/' + name
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != expected:
            raise RuntimeError('Qwen 推理代码校验失败：' + name)
        path.write_bytes(response.content)


def veda_predictor(c):
    return VEDA_PREDICTORS['r2va' if uses_reference_model(c) else 't2va']


def prepare_veda(downloader):
    """Install only on request; pin both the node and verified predictor."""
    if not CURRENT_CONFIG.get('veda', False):
        return
    folder = ROOT / 'custom_nodes' / VEDA_FOLDER
    marker = ROOT / '.h3-veda-ready'
    event('prepare', '准备 Veda 视频加速，版本 ' + VEDA_REVISION[:7])
    folder.parent.mkdir(exist_ok=True)
    with open('/content/h3-install.log', 'a') as log:
        if not folder.exists():
            subprocess.run(['git', 'clone', '--no-checkout', 'https://github.com/veda-sparse/Veda-on-ComfyUI.git', str(folder)],
                           stdout=log, stderr=log, check=True)
            subprocess.run(['git', '-C', str(folder), 'checkout', '--detach', VEDA_REVISION], stdout=log, stderr=log, check=True)
        revision = subprocess.check_output(['git', '-C', str(folder), 'rev-parse', 'HEAD'], text=True).strip()
        if revision != VEDA_REVISION:
            raise RuntimeError('Veda 版本与本项目固定版本不符，请使用新的运行时')
        if not marker.exists() or marker.read_text().strip() != VEDA_REVISION:
            subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(folder / 'requirements.txt')],
                           stdout=log, stderr=log, check=True)
            marker.write_text(VEDA_REVISION)
    predictor = veda_predictor(CURRENT_CONFIG)
    target = ROOT / 'models' / 'veda' / predictor['filename']
    target.parent.mkdir(parents=True, exist_ok=True)
    event('prepare', 'Veda 预测器未接入 Drive 缓存；来源=%s：%s' %
          ('运行时本地复用' if target.exists() else 'Hugging Face', target.name))
    if not target.exists():
        started = time.monotonic()
        download_model(downloader, repo_id=predictor['repo'], filename=predictor['filename'],
                       revision=predictor['revision'], local_dir=str(target.parent))
        event('prepare', 'Veda 预测器下载完成', timings={'veda_download_seconds': round(time.monotonic() - started, 2)})
    verify_weight(target, predictor['size'], predictor['sha256'], 'Veda 预测器')


def ensure_veda_service():
    """A retained service may have started before the node was installed."""
    import requests
    base = 'http://127.0.0.1:8188'
    info = requests.get(base + '/object_info', timeout=30).json()
    if 'VedaSparseAttention' in info:
        return
    queue = requests.get(base + '/queue', timeout=20).json()
    if queue.get('queue_running') or queue.get('queue_pending'):
        raise PreserveRuntimeError('现有 ComfyUI 仍有任务，保留运行时；空闲后才能加载 Veda')
    event('prepare', '重启空闲 ComfyUI 以加载 Veda')
    stop_owned_comfy()
    start_comfy()
    if 'VedaSparseAttention' not in requests.get(base + '/object_info', timeout=30).json():
        raise RuntimeError('Veda 节点加载失败，请检查 /content/h3-comfy.log；未回退到普通生成')


def prepare():
    gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'], text=True).strip()
    name, memory = gpu.splitlines()[0].rsplit(',', 1)
    if EXPECTED_GPU == 'G4' and not ('RTX PRO 6000' in name and 'Blackwell' in name):
        raise RuntimeError('申请 G4 但分配到其他型号，停止测试：' + gpu)
    if EXPECTED_GPU == 'A100' and 'A100' not in name:
        raise RuntimeError('申请 A100 但分配到其他型号，停止测试：' + gpu)
    if not ('A100' in name or ('RTX PRO 6000' in name and 'Blackwell' in name)) or int(memory.strip()) < 70000:
        raise RuntimeError('当前配置支持 A100 80GB 或 G4 96GB；当前显卡：' + gpu)
    event('prepare', '检查依赖和模型：' + name.strip(), gpu_info={'name': name.strip(), 'memory_mib': int(memory.strip())})
    ready = ROOT / '.h3-local-ready'
    if not ROOT.exists():
        subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/Comfy-Org/ComfyUI.git', str(ROOT)], check=True)
    import requests
    try:
        requests.get('http://127.0.0.1:8188/object_info', timeout=5).raise_for_status()
        serving = True
    except requests.RequestException:
        serving = False
    if not serving and not ready.exists():
        log = open('/content/h3-install.log', 'a')
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(ROOT / 'requirements.txt'), 'huggingface_hub', 'av'], stdout=log, stderr=log, check=True)
        ready.touch()
    import shutil
    (ROOT / 'input').mkdir(exist_ok=True)
    image_names = [r['image_id'] for r in CURRENT_CONFIG.get('references', [])]
    for name in dict.fromkeys(image_names + [a['audio_id'] for a in CURRENT_CONFIG.get('audio_references', [])]):
        if name:
            src = Path('/content') / name
            dst = ROOT / 'input' / name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
    from huggingface_hub import hf_hub_download
    event('prepare', 'Drive 模型缓存：%s。%s' % (cache_backend(MODEL_CACHE),
          '优先从 Hugging Face 获取，失败时从 Drive 备用恢复；生成过程不回写 Drive。'
          if MODEL_CACHE is not None else '仅复用运行时本地文件或从 Hugging Face 获取，本次不使用 Drive 备用。'))
    qwen_files, other_files = pipeline_files(CURRENT_CONFIG)
    loads = []
    model_started = time.monotonic()
    prompt_plan = None
    if qwen_files:
        prepare_textgen()
        if serving:
            event('prepare', '停止先前空闲的 ComfyUI 服务以释放 GPU 显存用于 Qwen 整理')
            stop_owned_comfy()
            serving = False
        event('prepare', '【流水线第 1 阶段】优先拉取 Qwen 文本编码与提示词整理模型')
        for repo, filename, category in qwen_files:
            prepare_model(repo, filename, category, hf_hub_download, MODEL_CACHE, loads)
        event('prepare', '【流水线第 2 阶段】Qwen 模型就绪，双轨并行：前台执行提示词整理，后台继续流式拉取扩散模型')
        cancel_event = threading.Event()
        if MODEL_CACHE is not None:
            MODEL_CACHE.cancel_check = cancel_event.is_set
        other_error = []
        sync_seconds = [0.0]

        def download_other_worker():
            try:
                for repo, filename, category in other_files:
                    if cancel_event.is_set():
                        return
                    prepare_model(repo, filename, category, hf_hub_download, MODEL_CACHE, loads)
                if cancel_event.is_set():
                    return
                prepare_veda(hf_hub_download)
                if MODEL_CACHE is not None and hasattr(MODEL_CACHE, 'flush'):
                    sync_started = time.monotonic()
                    MODEL_CACHE.flush()
                    sync_seconds[0] = time.monotonic() - sync_started
            except Exception as exc:
                other_error.append(exc)

        download_thread = threading.Thread(target=download_other_worker, name='model-downloader', daemon=True)
        download_thread.start()

        try:
            prompt_plan = enhance_prompt(CURRENT_CONFIG)
            if prompt_plan is not None:
                graph = build_graph(CURRENT_CONFIG, prompt_plan)
                prompt_plan['compiled_prompt'] = graph['5']['inputs']['prompt']
                prompt_plan['original_prompt'] = CURRENT_CONFIG['prompt']
                folder = ROOT / 'output' / 'H3'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / ('local_' + CURRENT_CONFIG['job_id'] + '.prompt.json')).write_text(json.dumps(prompt_plan, ensure_ascii=False), encoding='utf-8')
                event('prompt', 'Qwen 两步整理完成：第 1 步理解剧本结构，第 2 步忠实翻译为官方英文格式',
                      prompt_plan=prompt_plan, timings={'prompt_seconds': prompt_plan['seconds']})
                for summary_line in prompt_plan.get('understanding_summary', []):
                    event('prompt', '剧本理解：' + summary_line)
                for warning in prompt_plan.get('warnings', []):
                    event('prompt', warning)
        except Exception:
            cancel_event.set()
            download_thread.join(timeout=15)
            raise

        event('prepare', '提示词整理完成，等待后台扩散模型准备就绪…')
        download_thread.join()
        if other_error:
            raise other_error[0]
        model_load_summary(loads, time.monotonic() - model_started, sync_seconds[0])
    else:
        prepare_veda(hf_hub_download)
        event('prepare', ('性能加速配置' if CURRENT_CONFIG.get('performance', True) else '原版配置') + '；编码器：' + Path(encoder_files(CURRENT_CONFIG)[0][1]).name)
        for repo, filename, category in other_files:
            prepare_model(repo, filename, category, hf_hub_download, MODEL_CACHE, loads)
        sync_seconds = 0
        if MODEL_CACHE is not None and hasattr(MODEL_CACHE, 'flush'):
            sync_started = time.monotonic()
            MODEL_CACHE.flush()
            sync_seconds = time.monotonic() - sync_started
        model_load_summary(loads, time.monotonic() - model_started, sync_seconds)

    global CURRENT_PROMPT_PLAN
    CURRENT_PROMPT_PLAN = prompt_plan
    if not serving:
        start_comfy()
    if CURRENT_CONFIG.get('veda', False):
        ensure_veda_service()
    info = requests.get('http://127.0.0.1:8188/object_info', timeout=30).json()
    required_nodes = ['MiniMaxH3ReferenceToVideo' if uses_reference_model(CURRENT_CONFIG) else 'MiniMaxH3ImageToVideo', 'MiniMaxH3SigmaShift', 'VAEDecodeAudio', 'SaveVideo']
    if uses_reference_model(CURRENT_CONFIG) and any(r.get('frame') in ('first', 'last') for r in CURRENT_CONFIG.get('references', [])):
        required_nodes.append('MiniMaxH3AddGuide')
    if CURRENT_CONFIG.get('audio_references'):
        required_nodes.append('LoadAudio')
    for required in required_nodes:
        if required not in info:
            raise RuntimeError('现有 ComfyUI 缺少节点：' + required + '。请在 Colab 更新 ComfyUI 并重启服务。')
    event('prepare', '模型和 ComfyUI 已就绪')
    return prompt_plan


def start_comfy():
    import requests
    args = [sys.executable, str(ROOT / 'main.py'), '--listen', '127.0.0.1', '--port', '8188', '--disable-all-custom-nodes']
    if (ROOT / '.h3-veda-ready').exists():
        args += ['--whitelist-custom-nodes', VEDA_FOLDER]
    with open('/content/h3-comfy.log', 'a') as log:
        subprocess.Popen(args,
                         cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
    for _ in range(180):
        try:
            requests.get('http://127.0.0.1:8188/object_info', timeout=3).raise_for_status()
            return
        except requests.RequestException:
            time.sleep(2)
    raise RuntimeError('ComfyUI 启动失败，详情在 /content/h3-comfy.log')


def stop_owned_comfy():
    """Stop only our exact idle service, so its retained GPU weights are freed."""
    import signal
    found = []
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            args = (proc / 'cmdline').read_bytes().decode().split('\0')
        except (OSError, UnicodeError):
            continue
        if str(ROOT / 'main.py') in args and '--listen' in args and '--port' in args:
            if args[args.index('--listen') + 1] == '127.0.0.1' and args[args.index('--port') + 1] == '8188':
                found.append(int(proc.name))
    if len(found) != 1:
        raise PreserveRuntimeError('无法确认当前 ComfyUI 进程归属，停止服务重启并保留运行时')
    os.kill(found[0], signal.SIGTERM)
    for _ in range(60):
        state = Path('/proc') / str(found[0]) / 'stat'
        try:
            if state.read_text().split(') ', 1)[1].startswith('Z'):
                return
        except FileNotFoundError:
            return
        time.sleep(.5)
    raise PreserveRuntimeError('ComfyUI 未及时停止，运行时保留以便检查')


def build_graph(c, prompt_plan=None):
    c = effective_config(c)
    unet, vae, pdd = model_files(c)
    encoder = Path(encoder_files(c)[0][1]).name
    def node(cls, **inputs):
        return {'class_type': cls, 'inputs': inputs}
    width, height = DIMENSIONS[c['resolution']][c.get('aspect_ratio', '16:9')]
    length = math.ceil((c['duration'] * 24 - 5) / 17) * 17 + 5
    refs = ordered_images(c)
    use_refs = uses_reference_model(c)
    steps = 8 if c['mode'] == 'pdd' else 20
    prompt = prompt_plan['prompt'] if prompt_plan else (reference_prompt(c) if refs else c['prompt'])
    if c['people'] and not use_refs:
        prompt = 'r34l1sm, ' + prompt
    g = {
        '1': node('UNETLoader', unet_name=unet, weight_dtype='default'),
        '2': node('CLIPLoader', clip_name=encoder, type='minimax', device='default'),
        '3': node('VAELoader', vae_name=vae),
        '4': node('VAELoader', vae_name=AVAE),
        '5': node('MiniMaxH3ImageToVideo', clip=['2', 0], vae=['3', 0], prompt=prompt, width=width, height=height, length=length),
        '8': node('RandomNoise', noise_seed=c['seed']),
        '9': node('KSamplerSelect', sampler_name='euler' if steps == 8 else 'res_multistep'),
        '12': node('SamplerCustomAdvanced', noise=['8', 0], guider=['10', 0], sampler=['9', 0], sigmas=['11', 0], latent_image=['5', 1]),
        '13': node('VAEDecode', samples=['12', 0], vae=['3', 0]),
        '14': node('VAEDecodeAudio', samples=['12', 0], vae=['4', 0]),
        '15': node('CreateVideo', images=['13', 0], audio=['14', 0], fps=24),
        '16': node('SaveVideo', video=['15', 0], filename_prefix='H3/local_' + c['job_id'], format='mp4', **{'format.codec': 'h264'}),
    }
    model = ['1', 0]
    if steps == 8:
        g['6'] = node('LoraLoaderModelOnly', model=model, lora_name=pdd, strength_model=1.0)
        model = ['6', 0]
    if c['people'] and not use_refs:
        g['7'] = node('LoraLoaderModelOnly', model=model, lora_name=PEOPLE, strength_model=c['strength'])
        model = ['7', 0]
    conditioning = ['5', 0]
    if use_refs:
        inputs = g['5']['inputs']
        g['5']['class_type'] = 'MiniMaxH3ReferenceToVideo'
        inputs['audio_vae'] = ['4', 0]
        inputs['ref_image_size'] = c.get('reference_size', 'match')
        for index, ref in enumerate(refs):
            node_id = str(30 + index)
            g[node_id] = node('LoadImage', image=ref['image_id'])
            inputs[f'ref_images.ref_image_{index}'] = [node_id, 0]
            if ref.get('frame') in ('first', 'last'):
                guide_id = str(50 + index)
                g[guide_id] = node('MiniMaxH3AddGuide', positive=conditioning, latent=['5', 1],
                                  vae=['3', 0], image=[node_id, 0], frame_idx=0 if ref['frame'] == 'first' else -1)
                conditioning = [guide_id, 0]
        audios = c.get('audio_references', [])
        if prompt_plan is not None:
            audios = [audios[i] for i in prompt_plan['input_audio_indices']]
        for index, audio in enumerate(audios):
            node_id = str(70 + index)
            g[node_id] = node('LoadAudio', audio=audio['audio_id'])
            inputs[f'ref_audios.ref_audio_{index}'] = [node_id, 0]
    elif refs:
        for index, ref in enumerate(refs):
            node_id, scale_id = str(30 + index), str(40 + index)
            g[node_id] = node('LoadImage', image=ref['image_id'])
            g[scale_id] = node('ImageScale', image=[node_id, 0], upscale_method='lanczos', width=width, height=height, crop='center')
            g['5']['inputs']['first_frame' if ref['frame'] == 'first' else 'last_frame'] = [scale_id, 0]
    g['18'] = node('MiniMaxH3SigmaShift', model=model, shift_video=12.0, shift_audio=3.0)
    model = ['18', 0]
    if c.get('veda', False):
        g['20'] = node('VedaSparseAttention', model=model, predictor=veda_predictor(c)['filename'],
                       generated_sparsity='trained', reference_sparsity='0%',
                       full_attention_layers='', full_attention_steps='', verbose=True)
        model = ['20', 0]
    g['10'] = node('BasicGuider', model=model, conditioning=conditioning)
    g['11'] = node('BasicScheduler', model=model, scheduler='simple', steps=steps, denoise=1.0)
    return g


def result_for(path):
    import av
    path = path.resolve()
    if not path.is_relative_to((ROOT / 'output').resolve()):
        raise RuntimeError('输出路径不合法')
    with av.open(str(path)) as container:
        video = next(iter(container.streams.video), None)
        if video is None or not container.streams.audio:
            raise RuntimeError('生成结果缺少视频或音轨')
        metadata = dict(width=video.width, height=video.height, duration=float(video.duration * video.time_base) if video.duration else 0, audio=len(container.streams.audio))
    digest = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
    result = dict(path=str(path), size=path.stat().st_size, sha256=digest, **metadata)
    event('result', '音视频已生成，准备保存结果', result=result)
    return result


def generate(c, prompt_plan=None):
    c = effective_config(c)
    import requests
    base = 'http://127.0.0.1:8188'
    queue = requests.get(base + '/queue', timeout=20).json()
    if queue.get('queue_running') or queue.get('queue_pending'):
        raise PreserveRuntimeError('Colab 已有任务运行，请等现有任务完成后再提交。')
    # Recover a completed output if the local controller disconnected after submission.
    candidates = sorted((ROOT / 'output' / 'H3').glob('local_' + c['job_id'] + '*.mp4'))
    if candidates:
        audit = ROOT / 'output' / 'H3' / ('local_' + c['job_id'] + '.prompt.json')
        if audit.exists():
            event('prompt', '恢复本次实际生成提示词', prompt_plan=json.loads(audit.read_text(encoding='utf-8')))
        return result_for(candidates[-1])
    if prompt_plan is None:
        plan = enhance_prompt(c) if c.get('prompt_processing') == 'faithful' else dict(version='direct-h3', warnings=[], cached=False, seconds=0)
    else:
        plan = prompt_plan
    graph = build_graph(c, plan if c.get('prompt_processing') == 'faithful' else None)
    acceleration = dict(enabled=bool(c.get('veda')), method='veda' if c.get('veda') else 'default')
    if c.get('veda'):
        acceleration.update(revision=VEDA_REVISION, predictor=veda_predictor(c)['filename'],
                            generated_sparsity='trained', reference_sparsity='0%')
    event('generate', 'Veda 视频加速已开启，参考素材保留完整注意力' if c.get('veda') else '使用默认注意力生成',
          acceleration=acceleration)
    log_path = Path('/content/h3-comfy.log')
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    # Save the exact H3 text before submission, for both comparison routes.
    if 'compiled_prompt' not in plan:
        plan['compiled_prompt'] = graph['5']['inputs']['prompt']
        plan['original_prompt'] = c['prompt']
        folder = ROOT / 'output' / 'H3'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ('local_' + c['job_id'] + '.prompt.json')).write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        event('prompt', 'Qwen 两步整理完成：第 1 步理解剧本结构，第 2 步忠实翻译为官方英文格式' if c.get('prompt_processing') == 'faithful' else '已记录实际提示词：直接使用原文，仅映射素材引用',
              prompt_plan=plan, timings={'prompt_seconds': plan['seconds']})
        for summary_line in plan.get('understanding_summary', []):
            event('prompt', '剧本理解：' + summary_line)
        for warning in plan.get('warnings', []):
            event('prompt', warning)
    response = requests.post(base + '/prompt', json={'prompt': graph, 'client_id': str(uuid.uuid4())}, timeout=60)
    if not response.ok:
        raise RuntimeError('工作流校验失败：' + response.text[:1500])
    prompt_id = response.json()['prompt_id']
    event('generate', '任务已提交，加载模型并生成音视频', prompt_id=prompt_id)
    started = time.monotonic()
    for _ in range(720):
        history = requests.get(base + '/history/' + prompt_id, timeout=30).json().get(prompt_id, {})
        status = history.get('status', {})
        if status.get('status_str') == 'error':
            raise RuntimeError('生成失败：' + json.dumps(status.get('messages', []), ensure_ascii=False)[-2000:])
        if status.get('completed'):
            acceleration['sampler_cached'] = any(
                message[0] == 'execution_cached' and '12' in message[1].get('nodes', [])
                for message in status.get('messages', []))
            if c.get('veda') and log_path.exists():
                with log_path.open('rb') as log:
                    log.seek(log_offset)
                    lines = [re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
                             for line in log.read().decode('utf-8', errors='replace').splitlines()]
                acceleration['log'] = [line for line in lines if any(term in line.lower() for term in
                    ('veda', 'attention computed:', 'sparsity:', 'triton-int8', 'attention calls:'))][-30:]
            event('generate', '本次采样命中缓存' if acceleration['sampler_cached'] else 'Veda 加速生成完成' if c.get('veda') else '默认注意力生成完成',
                  acceleration=acceleration)
            event('generate', '生成完成，用时 %.1f 秒（含本次模型加载）' % (time.monotonic() - started), timings={'generation_seconds': round(time.monotonic() - started, 2)})
            for output in history.get('outputs', {}).values():
                for entries in output.values():
                    if isinstance(entries, list):
                        for entry in entries:
                            if isinstance(entry, dict) and entry.get('filename', '').endswith('.mp4'):
                                return result_for(ROOT / 'output' / entry.get('subfolder', '') / entry['filename'])
            raise RuntimeError('任务完成但没有找到 MP4')
        time.sleep(5)
        if int(time.monotonic() - started) % 10 < 5:
            event('generate', f'生成中，已用时 {int(time.monotonic() - started)} 秒')
    raise PreserveRuntimeError('生成超过 60 分钟；运行时保留，可检查或重试取回结果。')


def enhance_prompt(c):
    """Run Qwen in an isolated process, release its CUDA memory, then run H3."""
    import requests
    try:
        from .h3_prompt import prompt_request, request_fingerprint, load_plan
    except (ImportError, ValueError):
        from h3_prompt import prompt_request, request_fingerprint, load_plan
    context = prompt_request(c)
    encoder_asset, tail_asset = encoder_files(c)
    encoder, tail = Path(encoder_asset[1]).name, Path(tail_asset[1]).name
    encoder_revision = MODEL_IDENTITIES.get(encoder_asset[:2], {}).get('revision', CLIP_REVISION)
    fingerprint = request_fingerprint(context, encoder + ':' + encoder_revision + ':' + tail, TEXTGEN_REVISION)
    folder = ROOT / '.h3-prompt-cache'
    folder.mkdir(exist_ok=True)
    result = folder / (fingerprint + '.result.json')
    if result.exists():
        try:
            plan = load_plan(json.loads(result.read_text(encoding='utf-8')))
            event('prompt', '复用已缓存的提示词整理结果')
            return {**plan, 'cached': True, 'seconds': 0, 'fingerprint': fingerprint,
                    'encoder_revision': encoder_revision, 'encoder': encoder, 'tail': tail, 'textgen_revision': TEXTGEN_REVISION}
        except (ValueError, KeyError, TypeError):
            pass
    comfy_was_running = False
    try:
        queue = requests.get('http://127.0.0.1:8188/queue', timeout=3).json()
        if queue.get('queue_running') or queue.get('queue_pending'):
            raise PreserveRuntimeError('已有 ComfyUI 任务运行，请等待后再整理提示词')
        comfy_was_running = True
    except (requests.RequestException, ValueError):
        comfy_was_running = False
    if comfy_was_running:
        stop_owned_comfy()
    request_path = folder / (fingerprint + '.request.json')
    request_path.write_text(json.dumps({'context': context, 'encoder': encoder}, ensure_ascii=False), encoding='utf-8')
    worker = ROOT.parent / 'h3-prompt-tools' / 'h3_prompt_worker.py'
    event('prompt', 'Qwen 正在分两步整理提示词：先按原文理解剧本结构，再翻译为 H3 官方英文格式')
    started = time.monotonic()
    log_path = Path('/content/h3-prompt-worker.log')
    try:
        with log_path.open('w') as log:
            try:
                process = subprocess.run([sys.executable, str(worker), str(request_path), str(result), str(ROOT), tail],
                                         cwd=ROOT, stdout=log, stderr=log, timeout=1800)
            except subprocess.TimeoutExpired:
                raise RuntimeError('Qwen 提示词整理超过 30 分钟，已停止整理进程') from None
    finally:
        try:
            saved = []
            for suffix in ('.dialogue.json', '.diagnostics.json'):
                diagnostics = result.with_suffix(suffix)
                if diagnostics.exists():
                    try:
                        records = json.loads(diagnostics.read_text(encoding='utf-8'))
                    except (OSError, ValueError) as exc:
                        event('prompt', '读取 Qwen 诊断记录失败：' + str(exc))
                    else:
                        saved.extend([{**item, 'phase':'dialogue-extraction'} for item in records]
                                     if suffix == '.dialogue.json' else records)
            if saved:
                event('prompt', '已保存 Qwen 原始输出与解析记录', prompt_diagnostics=saved)
        finally:
            if comfy_was_running:
                start_comfy()
    if process.returncode:
        errors = list(dict.fromkeys(item['error'] for item in saved if item.get('error')))
        detail = '；'.join(errors) if errors else log_path.read_text(errors='replace')[-1800:]
        raise RuntimeError('Qwen 提示词整理失败：' + detail)
    plan = load_plan(json.loads(result.read_text(encoding='utf-8')))
    return {**plan, 'cached': False, 'seconds': round(time.monotonic() - started, 2),
            'fingerprint': fingerprint, 'encoder_revision': encoder_revision, 'encoder': encoder,
            'tail': tail, 'textgen_revision': TEXTGEN_REVISION}


def run(c):
    global EXPECTED_GPU, CURRENT_CONFIG
    c = effective_config(c)
    CURRENT_CONFIG = c
    EXPECTED_GPU = c.get('gpu', 'A100')
    stop = threading.Event()
    def heartbeat():
        started = time.monotonic()
        while not stop.wait(15):
            event('heartbeat', 'Colab 正在处理任务，已用时 %d 秒' % (time.monotonic() - started))
    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        started = time.monotonic()
        prepare()
        event('prepare', '环境准备用时 %.1f 秒' % (time.monotonic() - started), timings={'prepare_seconds': round(time.monotonic() - started, 2)})
        return generate(c, prompt_plan=CURRENT_PROMPT_PLAN)
    except Exception as exc:
        event('failure', str(exc), safe_release=not isinstance(exc, PreserveRuntimeError))
        raise
    finally:
        stop.set()

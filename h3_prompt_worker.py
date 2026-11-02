"""Short-lived Colab Qwen inference process; no H3 diffusion or public server."""
import importlib.util
import json
from pathlib import Path
import sys
import time

from h3_prompt import (TRANSLATE_PROMPT, UNDERSTAND_PROMPT, decode_plan, prepare_understanding, render,
                       translation_context, understanding_context)


def verify_dialogue_tokens(clip):
    result = {}
    for text, expected in [('<d>', 151669), ('</d>', 151670)]:
        batches = clip.tokenize(text, skip_template=True, min_length=1)
        ids = [token[0] for values in batches.values() for batch in values for token in batch
               if isinstance(token[0], int)]
        if expected not in ids:
            raise RuntimeError('H3 对白特殊 token 不正确，请更新 ComfyUI：' + text)
        result[text] = expected
    return result


def load_images(root, pictures):
    import numpy as np
    import torch
    from PIL import Image, ImageOps
    tensors = []
    for picture in pictures:
        path = (root / 'input' / picture['image_id']).resolve()
        if not path.exists():
            candidate = Path('/content') / picture['image_id']
            if candidate.exists():
                path = candidate.resolve()
        if not (path.is_relative_to((root / 'input').resolve()) or path.is_relative_to(Path('/content').resolve())):
            raise ValueError('参考图路径不合法')
        with Image.open(path) as source:
            # Preserve the whole image for semantic inspection. H3 still receives
            # the original file and uses its existing reference/anchor handling.
            image = ImageOps.pad(ImageOps.exif_transpose(source).convert('RGB'), (768, 768),
                                 method=Image.Resampling.LANCZOS, color=(0, 0, 0))
            tensors.append(torch.from_numpy(np.asarray(image).copy()).float().div(255).unsqueeze(0))
    return torch.cat(tensors) if tensors else None


def save_diagnostics(path, attempts):
    if path is None:
        return
    destination = Path(path)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(attempts, ensure_ascii=False), encoding='utf-8')
    temporary.replace(destination)


def rewrite(request, clip, generator, tail, image=None, diagnostics_path=None, max_attempts=2):
    """Pass 1 understands the script (no pictures); pass 2 translates (with pictures).

    Each pass attempts greedy generation first. If parsing fails, it retries with perturbed
    sampling parameters up to max_attempts.
    """
    records = []

    def call(phase, system_prompt, payload, pictures, picture_count, max_new_tokens,
             attempt=1, sampling='greedy', temperature=0.2, seed=0):
        started = time.monotonic()
        output = generator.generate_text(
            clip=clip, tail_clip={'tail_name': tail}, system_prompt=system_prompt,
            prompt=json.dumps(payload, ensure_ascii=False),
            max_new_tokens=max_new_tokens, sampling=sampling, temperature=temperature, top_k=20, top_p=0.9,
            min_p=0.0, repetition_penalty=1.0, presence_penalty=0.0, seed=seed, thinking=False,
            image_batch_mode='all images from start', max_images=max(1, picture_count),
            clean_output=True, image=pictures, decode_backend='standard KV cache')
        record = dict(attempt=attempt, phase=phase, seconds=round(time.monotonic() - started, 2),
                      output=output[0][:40000], raw_output=output[1][:40000], report=output[4], error=None)
        records.append(record)
        save_diagnostics(diagnostics_path, records)
        return output, record

    def fail(record, label, exc):
        record['error'] = label + str(exc)
        save_diagnostics(diagnostics_path, records)
        raise ValueError(record['error']) from exc

    # Pass 1: Understanding
    understanding = None
    first = None
    first_record = None
    for attempt in range(1, max_attempts + 1):
        sampling_mode = 'greedy' if attempt == 1 else 'sample'
        temp = 0.2 if attempt == 1 else 0.4
        seed = 0 if attempt == 1 else 42
        first, first_record = call('understanding', UNDERSTAND_PROMPT, understanding_context(request),
                                   None, 0, 4096, attempt=attempt, sampling=sampling_mode,
                                   temperature=temp, seed=seed)
        try:
            understanding = prepare_understanding(decode_plan(first[0]), request)
            break
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            if attempt < max_attempts:
                first_record['error'] = f'第 {attempt} 次解析失败（自动重试中）：{exc}'
                save_diagnostics(diagnostics_path, records)
            else:
                fail(first_record, 'Qwen 第 1 步（理解剧本）输出无法使用：', exc)

    # Pass 2: Translation & Rendering
    result = None
    second = None
    second_record = None
    for attempt in range(1, max_attempts + 1):
        sampling_mode = 'greedy' if attempt == 1 else 'sample'
        temp = 0.2 if attempt == 1 else 0.4
        seed = 0 if attempt == 1 else 42
        second, second_record = call('translation', TRANSLATE_PROMPT, translation_context(understanding, request),
                                     image, len(request['pictures']), 6144,
                                     attempt=attempt, sampling=sampling_mode, temperature=temp, seed=seed)
        try:
            result = render(understanding, decode_plan(second[0]), request, strict_english=(attempt < max_attempts))
            break
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            if attempt < max_attempts:
                second_record['error'] = f'第 {attempt} 次解析失败（自动重试中）：{exc}'
                save_diagnostics(diagnostics_path, records)
            else:
                fail(second_record, 'Qwen 第 2 步（翻译描述）输出无法使用：', exc)

    result['generation_report'] = [first[4], second[4]]
    result['attempt_seconds'] = [first_record['seconds'], second_record['seconds']]
    return result


def main(request_path, result_path, root_path, tail):
    root = Path(root_path).resolve()
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    sys.path.insert(0, str(root))
    # Keep ComfyUI's CLI parser from consuming worker file arguments.
    sys.argv = [sys.argv[0]]
    package_path = root / '.h3-textgen' / '__init__.py'
    spec = importlib.util.spec_from_file_location('h3_private_textgen', package_path,
                                                 submodule_search_locations=[str(package_path.parent)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    generator = package.NODE_CLASS_MAPPINGS['H3QwenVLGenerateText']()
    import nodes
    clip = nodes.CLIPLoader().load_clip(request['encoder'], 'minimax', 'default')[0]
    token_ids = verify_dialogue_tokens(clip)
    started = time.monotonic()
    import torch
    # ComfyUI's executor normally supplies this context. This standalone
    # process must do the same for Qwen's out= operations and KV cache writes.
    with torch.inference_mode():
        result = rewrite(request['context'], clip, generator, tail, load_images(root, request['context']['pictures']),
                         diagnostics_path=Path(result_path).with_suffix('.diagnostics.json'))
        result['execution_mode'] = {'inference': torch.is_inference_mode_enabled(),
                                    'grad_enabled': torch.is_grad_enabled()}
    result['tokenizer_ids'] = token_ids
    result['inference_seconds'] = round(time.monotonic() - started, 2)
    path = Path(result_path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


if __name__ == '__main__':
    main(*sys.argv[1:])

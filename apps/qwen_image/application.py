"""Qwen-Image-2.1 application deployment and configuration."""
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from workspace_runtime import ApplicationSpec
from .remote_qwen_image import DIMENSIONS


class ReferenceImage(BaseModel):
    slot: int = Field(default=1, ge=1, le=4)
    image_id: str = Field(pattern=r'^[0-9a-f]{32}\.(png|jpg|webp)$')
    description: str = Field(default='', max_length=500)


MODEL_ALIASES = {
    'qwen-image-2.1-UC-Q4_K_M.gguf': 'qwen-image-2.1-turbo-Q4_K_M.gguf',
    'qwen-image-2.1-UC-fp8.safetensors': 'qwen-image-2.1-turbo-fp8.safetensors',
    'qwen-image-2.1-UC-int8_convrot.safetensors': 'qwen-image-2.1-turbo-int8_convrot.safetensors',
    'qwen-image-2.1-UC-Q5_K_M.gguf': 'qwen-image-2.1-turbo-Q5_K_M.gguf',
    'qwen-image-2.1-UC-Q8_0.gguf': 'qwen-image-2.1-turbo-Q8_0.gguf',
    'qwen-image-2.1-UC-BF16.gguf': 'qwen-image-2.1-turbo-Q8_0.gguf',
}

class QwenImageJobConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    app_id: Literal['qwen_image_2_1'] = 'qwen_image_2_1'
    provider_id: Literal['colab'] = 'colab'
    prompt: str = Field(min_length=1, max_length=8000)
    negative_prompt: str = Field(default='', max_length=2000)
    title: str = Field(default='', max_length=80)
    aspect_ratio: Literal['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3'] = '1:1'
    model_file: Literal[
        'qwen-image-2.1-turbo-Q4_K_M.gguf',
        'qwen-image-2.1-turbo-fp8.safetensors',
        'qwen-image-2.1-turbo-int8_convrot.safetensors',
        'qwen-image-2.1-turbo-Q5_K_M.gguf',
        'qwen-image-2.1-turbo-Q8_0.gguf',
        'qwen-image-2.1-turbo-Q4_0.gguf',
        'qwen-image-2.1-turbo-Q6_K.gguf',
        'qwen-image-2.1-UC-Q4_K_M.gguf',
        'qwen-image-2.1-UC-fp8.safetensors',
        'qwen-image-2.1-UC-int8_convrot.safetensors',
        'qwen-image-2.1-UC-BF16.gguf',
        'qwen-image-2.1-UC-Q8_0.gguf',
        'qwen-image-2.1-UC-Q5_K_M.gguf',
    ] = 'qwen-image-2.1-turbo-Q4_K_M.gguf'
    encoder: Literal['int8_convrot', 'bf16'] = 'int8_convrot'
    steps: int = Field(default=8, ge=1, le=60)
    cfg: float = Field(default=1.0, ge=1.0, le=20.0)
    count: int = Field(default=1, ge=1, le=8, strict=True)
    seed: int = Field(default=20261103, ge=0, le=2**53 - 1)
    release_mode: Literal['always', 'idle', 'manual'] = 'idle'
    idle_minutes: int = Field(default=10, ge=1, le=120)
    gpu: str = 'T4'
    endpoint: str | None = None
    generation_type: Literal['text', 'image'] | None = None
    references: list[ReferenceImage] = Field(default_factory=list, max_length=4)
    denoise: float = Field(default=0.75, ge=0.0, le=1.0)

    @field_validator('model_file', mode='before')
    @classmethod
    def normalize_model_file(cls, v: str) -> str:
        if isinstance(v, str):
            if v in MODEL_ALIASES:
                return MODEL_ALIASES[v]
            if v.startswith('qwen-image-2.1-UC-'):
                return v.replace('qwen-image-2.1-UC-', 'qwen-image-2.1-turbo-')
        return v

    @field_validator('prompt')
    @classmethod
    def validate_prompt(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('请输入提示词')
        return v

    @model_validator(mode='after')
    def validate_generation(self):
        inferred = 'image' if self.references else 'text'
        if self.generation_type and self.generation_type != inferred:
            raise ValueError('生成方式与上传的图片不匹配；图生图至少需要一张图片')
        self.generation_type = inferred
        if self.references:
            if len({r.slot for r in self.references}) != len(self.references):
                raise ValueError('参考图编号不能重复')
        return self


class QwenImageApplication:
    config_model = QwenImageJobConfig
    web_dir = Path(__file__).parent / 'web'
    spec = ApplicationSpec(
        id='qwen_image_2_1',
        name='Qwen-Image-2.1',
        description='文生图与图生图 · 社区量化加速版 (abenzerps/Qwen-Image-2.1-Turbo-Quantized)',
        output_kind='image',
        providers=('colab',),
        tabs=({'id': 'tool', 'name': '创作'},),
        asset_purposes=(
            {'id': 'references', 'name': '创作参考', 'kinds': ('images',)},
            {'id': 'outputs', 'name': '生成图片', 'kinds': ('images',)},
        ),
    )
    event_prefix = 'QWEN_IMAGE_EVENT '
    remote_output = '/content/ComfyUI/output'
    output_extension = '.png'
    metadata_fields = ('width', 'height')

    idle_marker = 'QWEN_IMAGE_IDLE_CHECK '
    idle_probe = '''import json, socket, requests, errno
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
print('QWEN_IMAGE_IDLE_CHECK ' + json.dumps({'idle': idle}))
'''

    def source(self, base, config):
        app_dir = Path(__file__).resolve().parent
        remote_path = app_dir / 'remote_qwen_image.py'
        if not remote_path.exists():
            remote_path = Path(base) / 'apps' / 'qwen_image' / 'remote_qwen_image.py'
        return remote_path.read_text(encoding='utf-8')

    def validate_assets(self, config, asset_library):
        for reference in getattr(config, 'references', []):
            if not asset_library.exists(reference.image_id):
                raise ValueError(f'图片{reference.slot}已丢失，请重新上传')

    def settings(self):
        return {'dimensions': DIMENSIONS}

    def input_assets(self, config):
        return list(dict.fromkeys(ref['image_id'] for ref in config.get('references', [])))

    def invocation(self, source, job):
        config = dict(job['config'], job_id=job['id'])
        assets = self.input_assets(config)
        if assets:
            source += (
                '\n_orig_prepare = prepare\n'
                'def prepare():\n'
                '    _orig_prepare()\n'
                '    import shutil\n'
                '    (ROOT / "input").mkdir(exist_ok=True)\n'
                '    for name in ' + repr(assets) + ':\n'
                '        shutil.copy2("/content/" + name, ROOT / "input" / name)\n'
            )
        return source + '\nrun(json.loads(' + repr(json.dumps(config)) + '))\n'

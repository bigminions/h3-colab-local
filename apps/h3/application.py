"""H3 application deployment. Compute adapters supply transport and storage."""
import base64
import json
from pathlib import Path
from typing import Callable, ClassVar, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from workspace_runtime import ApplicationSpec
from .characters import CharacterProfile, VoiceReference, expand_characters, validate_audio_asset
from .remote_h3 import reference_prompt, uses_reference_model, DIMENSIONS


class ReferenceImage(BaseModel):
    slot: int = Field(ge=1, le=9)
    image_id: str = Field(pattern=r'^[0-9a-f]{32}\.(png|jpg|webp)$')
    role: Literal['character', 'scene', 'clothing', 'style', 'product', 'other'] = 'other'
    description: str = Field(default='', max_length=500)
    frame: Literal['reference', 'first', 'last'] = 'reference'
    character_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')


class H3JobConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    character_profiles: ClassVar[Callable[[], list[CharacterProfile]]] = lambda: []
    app_id: Literal['h3'] = 'h3'
    provider_id: Literal['colab'] = 'colab'
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
    release_mode: Literal['always', 'idle', 'manual'] = 'idle'
    idle_minutes: int = Field(default=10, ge=1, le=120)
    gpu: str = 'A100'
    endpoint: str | None = None
    generation_type: Literal['text', 'image'] | None = None
    references: list[ReferenceImage] = Field(default_factory=list, max_length=9)
    reference_size: Literal['match', 'max'] = 'match'
    characters: list[CharacterProfile] = Field(default_factory=list, max_length=9)
    audio_references: list[VoiceReference] = Field(default_factory=list, max_length=3)

    @model_validator(mode='before')
    @classmethod
    def resolve_mentions(cls, data):
        if isinstance(data, dict) and '@' in str(data.get('prompt', '')):
            return expand_characters(data, cls.character_profiles())
        return data

    @model_validator(mode='after')
    def validate_references(self):
        if not self.performance:
            self.encoder = 'official_bf16'
            self.quantized_model = self.quantized_vae = self.veda = self.people = False
            self.mode = 'base'
        if self.characters and (not self.references or len({c.id for c in self.characters}) != len(self.characters)):
            raise ValueError('角色配置需要有效参考图，且角色不能重复')
        if self.audio_references:
            if not self.references:
                raise ValueError('声音参考需要搭配角色参考图')
            if sum(a.duration for a in self.audio_references) > 15.0001:
                raise ValueError('声音参考总长不能超过 15 秒')
            if any(a.character_id not in {c.id for c in self.characters} for a in self.audio_references):
                raise ValueError('声音参考未绑定有效角色')
        inferred = 'image' if self.references else 'text'
        if self.generation_type and self.generation_type != inferred:
            raise ValueError('生成方式与上传的图片不匹配；图生视频至少需要一张图片')
        if self.references:
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


class H3Application:
    config_model = H3JobConfig
    web_dir = Path(__file__).parent / 'web'
    spec = ApplicationSpec(
        id='h3', name='H3 视频', description='文生视频、图生视频与角色参考',
        output_kind='video', providers=('colab',),
        tabs=({'id': 'tool', 'name': '创作'}, {'id': 'characters', 'name': '角色管理'}),
        asset_purposes=({'id': 'references', 'name': '创作参考', 'kinds': ('images',)},
                        {'id': 'characters', 'name': '角色素材', 'kinds': ('images', 'audios')},
                        {'id': 'outputs', 'name': '生成作品', 'kinds': ('videos',)}),
    )
    event_prefix = 'H3_EVENT '
    remote_output = '/content/ComfyUI/output'
    output_extension = '.mp4'
    metadata_fields = ('width', 'height', 'duration', 'audio')

    idle_marker = 'H3_IDLE_CHECK '
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
print('H3_IDLE_CHECK ' + json.dumps({'idle': idle}))
'''

    def source(self, base, config):
        app_dir = Path(__file__).resolve().parent
        remote_path = app_dir / 'remote_h3.py'
        if not remote_path.exists():
            remote_path = Path(base) / 'remote_h3.py'
        source = remote_path.read_text(encoding='utf-8')
        if config.get('prompt_processing') == 'faithful':
            bundle = {}
            for name in ('h3_prompt.py', 'h3_prompt_worker.py'):
                file_path = app_dir / name
                if not file_path.exists():
                    file_path = Path(base) / name
                bundle[name] = base64.b64encode(file_path.read_bytes()).decode()
            source = ('import base64, pathlib, sys\n_prompt_tools = pathlib.Path("/content/h3-prompt-tools")\n'
                      '_prompt_tools.mkdir(exist_ok=True)\n'
                      'for _name, _data in ' + repr(bundle) + '.items():\n'
                      '    (_prompt_tools / _name).write_bytes(base64.b64decode(_data))\n'
                      'sys.path.insert(0, str(_prompt_tools))\nsys.modules.pop("h3_prompt", None)\n' + source)
        return source

    def validate_assets(self, config, asset_library):
        for reference in getattr(config, 'references', []):
            if not asset_library.exists(reference.image_id):
                raise ValueError(f'图片{reference.slot}已丢失，请重新上传')
        for audio in getattr(config, 'audio_references', []):
            actual = validate_audio_asset(asset_library, audio)
            if abs(actual.duration - audio.duration) > .001:
                raise ValueError('声音样本时长不匹配，请重新上传')

    def settings(self):
        return {'dimensions': DIMENSIONS, 'fps': 24, 'alignment': 32}

    def input_assets(self, config):
        images = [ref['image_id'] for ref in config.get('references', [])]
        return list(dict.fromkeys(images + [audio['audio_id'] for audio in config.get('audio_references', [])]))

    def invocation(self, source, job):
        config = dict(job['config'], job_id=job['id'])
        assets = self.input_assets(config)
        if assets:
            source += '\n_orig_prepare = prepare\ndef prepare():\n    _orig_prepare()\n    import shutil\n    (ROOT / "input").mkdir(exist_ok=True)\n    for name in ' + repr(assets) + ':\n        shutil.copy2("/content/" + name, ROOT / "input" / name)\n'
        return source + '\nrun(json.loads(' + repr(json.dumps(config)) + '))\n'

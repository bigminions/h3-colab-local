"""Gemma's application descriptor and Colab execution contract."""
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from workspace_runtime import ApplicationSpec


APP_ID = 'gemma_chat'
Variant = Literal['native', 'heretic', 'gpt_oss_120b']
ChatId = Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{32}$')]
Gpu = Literal['G4', 'A100', 'H100', 'L4', 'T4']


class OpenRequest(BaseModel):
    variant: Variant = 'native'
    gpu: Gpu = 'L4'
    endpoint: str | None = Field(default=None, max_length=240)


class CharacterPayload(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=40)
    avatar: str = Field(default='', max_length=2000000)
    tag: str = Field(default='', max_length=80)
    desc: str = Field(default='', max_length=800)
    placeholder: str = Field(default='', max_length=240)
    system_prompt: str = Field(default='', max_length=8000)
    builtin: bool = False
    is_customized: bool = False
    persona: str = Field(default='', max_length=60)
    cover: str = Field(default='', max_length=2000000)


class SendRequest(BaseModel):
    prompt: str = Field(default='', max_length=12000)
    images: list[str] = Field(default_factory=list)
    existing_chat: bool = False
    character_id: str | None = None
    character_name: str | None = None
    system_prompt: str | None = None

    @model_validator(mode='after')
    def validate_content(self):
        self.prompt = (self.prompt or '').strip()
        if not self.prompt and not self.images:
            raise ValueError('请输入聊天内容或上传图片')
        return self


class GemmaJobConfig(OpenRequest):
    app_id: Literal['gemma_chat'] = APP_ID
    provider_id: Literal['colab'] = 'colab'
    operation: Literal['open', 'reply']
    chat_id: ChatId | None = None
    prompt: str = ''
    images: list[str] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    secret: str | None = None
    character_id: str | None = None
    character_name: str | None = None
    system_prompt: str | None = None


class GemmaApplication:
    spec = ApplicationSpec(APP_ID, '大模型对话', '支持 Gemma 4 与 GPT-OSS 120B 等开源大模型的多模态图文与会话聊天',
                           'text', ('colab',), ({'id': 'chat', 'name': '聊天'},))
    config_model = GemmaJobConfig
    web_dir = Path(__file__).parent / 'web'
    uses_drive_cache = False
    event_prefix = 'GEMMA_EVENT '
    idle_marker = 'GEMMA_IDLE_CHECK '
    idle_probe = '''import json, socket, errno, requests
with socket.socket() as probe:
    probe.settimeout(3)
    connection = probe.connect_ex(('127.0.0.1', 18829))
idle = connection == errno.ECONNREFUSED
if connection == 0:
    response = requests.get('http://127.0.0.1:18829/slots', timeout=5)
    if response.ok:
        slots = response.json()
        idle = bool(slots) and isinstance(slots, list) and all(slot.get('is_processing') is False for slot in slots)
sample_gpu = globals().get('gpu_sample')
gpu = sample_gpu() if idle and callable(sample_gpu) else None
print('GEMMA_IDLE_CHECK ' + json.dumps({'idle': idle, 'gpu': gpu}))
'''

    def source(self, base, config):
        return (self.web_dir.parent / 'remote_gemma.py').read_text(encoding='utf-8')

    def input_assets(self, config):
        return []

    def invocation(self, source, job):
        config = dict(job['config'], job_id=job['id'])
        if job.get('reuse_model'):
            config['reuse_model'] = True
        return source + '\nrun(json.loads(' + repr(json.dumps(config)) + '))\n'

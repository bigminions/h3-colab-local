"""Local Gemma lifecycle and conversations backed by the shared job store."""
import base64
import json
from pathlib import Path
import secrets
import threading
import time
from typing import Literal
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
import requests

from .application import APP_ID, ChatId, GemmaJobConfig, OpenRequest, SendRequest, CharacterPayload
from job_queue import waiting_jobs


PENDING = {'queued', 'running', 'cancelling'}
SUCCESS = {'done', 'warning'}

MODELS_META = {
    'native': {
        'id': 'native',
        'name': 'Gemma 4 31B 原生版',
        'multimodal': True,
        'min_vram_gb': 24,
        'recommended_gpu': 'L4',
        'badge': '多模态 (图文)',
    },
    'heretic': {
        'id': 'heretic',
        'name': 'Gemma 4 31B 破甲版',
        'multimodal': True,
        'min_vram_gb': 24,
        'recommended_gpu': 'L4',
        'badge': '多模态 (图文)',
    },
    'gpt_oss_120b': {
        'id': 'gpt_oss_120b',
        'name': 'GPT-OSS 120B',
        'multimodal': False,
        'min_vram_gb': 80,
        'recommended_gpu': 'A100',
        'badge': '纯文本',
    },
}

def _load_default_avatar_data_url(filename: str) -> str:
    path = Path(__file__).parent / 'web' / 'characters' / filename
    if path.exists():
        raw = path.read_bytes()
        return f"data:image/jpeg;base64,{base64.b64encode(raw).decode('ascii')}"
    return ''


BUILTIN_CHARACTERS = [
    {
        'id': 'assistant',
        'name': '小A',
        'avatar': _load_default_avatar_data_url('assistant_avatar.jpg'),
        'cover': _load_default_avatar_data_url('assistant_avatar.jpg'),
        'tag': 'AI 助手 · 默认',
        'desc': '知识渊博、理性条理且富有温度的拟人化数智伙伴，擅长各类答疑、逻辑推理与日常协作。',
        'placeholder': '向小A提问，Shift + Enter 换行，Enter 发送...',
        'system_prompt': '你是小A，一位拟人化的数智 AI 助手。你专业、敏锐、真诚且知识渊博，善于用清晰条理、逻辑严密且富有温度的语言回答用户的各类问题。在阐述专业知识时深入浅出，遇到复杂问题善于分步骤剖析。保持礼貌友善、谦逊温和的交谈姿态。',
        'builtin': True,
    },
    {
        'id': 'scarlett',
        'name': '林薇',
        'avatar': _load_default_avatar_data_url('beauty_avatar.jpg'),
        'cover': _load_default_avatar_data_url('beauty_avatar.jpg'),
        'tag': '热情奔放 · 元气满满',
        'desc': '性格开朗明媚、热情直爽的大美女，笑容治愈、活力四射，喜欢分享美好事物，永远给你满满的正能量！',
        'placeholder': '哈喽！今天有什么开心的事想和我聊聊嘛？...',
        'system_prompt': '你是林薇（Scarlett），一位性格热情奔放、阳光明媚且极具感染力的年轻美女。你说话直接爽朗、生动有趣，常常带着灿烂的笑容和满满的正能量。你善于倾听、真诚共情，当朋友遇到烦恼时你会以大方热烈的态度鼓励对方，带给用户无尽的温暖与快乐。',
        'builtin': True,
    },
    {
        'id': 'leo',
        'name': '陆扬',
        'avatar': _load_default_avatar_data_url('handsome_avatar.jpg'),
        'cover': _load_default_avatar_data_url('handsome_avatar.jpg'),
        'tag': '阳光爽朗 · 靠谱温暖',
        'desc': '热爱运动与生活的阳光大男孩，眼神清澈、真诚坦率，随和风趣又靠谱，随时为你排忧解难！',
        'placeholder': '嘿，有什么烦恼或者计划？随时跟我说说吧！...',
        'system_prompt': '你是陆扬（Leo），一位阳光帅气、开朗幽默且真诚靠谱的青年。你热爱运动与生活，性格随和开朗，说话幽默得体，像个温暖热心的大男孩。面对朋友的倾诉或求助，你既能陪着轻松调侃，又能在关键时刻给出务实稳重的建议与毫无保留的支持。',
        'builtin': True,
    },
]


BUILTIN_MAP = {c['id']: c for c in BUILTIN_CHARACTERS}


class GemmaWorkspace:
    CLOSE_TIMEOUT = 180

    def __init__(self, controller, authorized):
        self.controller = controller
        self.authorized = authorized
        self.closed_id = controller.store.get_state(APP_ID)
        self.closing = False
        self.close_error = None
        self.router = APIRouter(prefix='/api/gemma')
        self.router.add_api_route('', self.snapshot, methods=['GET'])
        self.router.add_api_route('/debug', self.debug, methods=['GET'])
        self.router.add_api_route('/characters', self.get_characters_route, methods=['GET'])
        self.router.add_api_route('/characters', self.save_character_route, methods=['POST'])
        self.router.add_api_route('/characters/{char_id}/reset', self.reset_character_route, methods=['POST'])
        self.router.add_api_route('/characters/{char_id}', self.delete_character_route, methods=['DELETE'])
        self.router.add_api_route('/open', self.open, methods=['POST'], status_code=202)
        self.router.add_api_route('/close', self.close, methods=['POST'], status_code=202)
        # Messages never enter the queue: the open chat task already owns the GPU.
        self.router.add_api_route('/chats/{chat_id}/stream', self.stream, methods=['POST'])
        self.router.add_api_route('/chats/{chat_id}', self.remove_chat, methods=['DELETE'])

    def get_characters(self):
        overrides = self.controller.store.get_state('gemma_character_overrides', {})
        default_avatar_map = {
            'assistant': 'assistant_avatar.jpg',
            'scarlett': 'beauty_avatar.jpg',
            'leo': 'handsome_avatar.jpg',
        }
        builtins = []
        for b in BUILTIN_CHARACTERS:
            item = dict(b)
            if isinstance(overrides, dict) and b['id'] in overrides and isinstance(overrides[b['id']], dict):
                user_mod = overrides[b['id']]
                item.update({k: v for k, v in user_mod.items() if k != 'id'})
                item['builtin'] = True
                item['is_customized'] = True
            else:
                item['builtin'] = True
                item['is_customized'] = False
            if not item.get('avatar') or item['avatar'].startswith('/apps/'):
                item['avatar'] = _load_default_avatar_data_url(default_avatar_map.get(b['id'], 'assistant_avatar.jpg'))
            item['cover'] = item['avatar']
            item.pop('persona', None)
            builtins.append(item)

        custom = self.controller.store.get_state('gemma_custom_characters', [])
        clean_custom = []
        for c in custom:
            if isinstance(c, dict):
                item = dict(c)
                item['builtin'] = False
                item['is_customized'] = True
                if not item.get('avatar') or item['avatar'].startswith('/apps/'):
                    item['avatar'] = _load_default_avatar_data_url('assistant_avatar.jpg')
                item['cover'] = item['avatar']
                item.pop('persona', None)
                clean_custom.append(item)
        return builtins + clean_custom

    def get_characters_route(self):
        return self.get_characters()

    def save_character_route(self, payload: CharacterPayload):
        char_data = payload.model_dump()
        char_id = char_data.get('id')
        if not char_data.get('avatar') or char_data['avatar'].startswith('/apps/'):
            char_data['avatar'] = _load_default_avatar_data_url('assistant_avatar.jpg')
        char_data['cover'] = char_data['avatar']
        char_data.pop('persona', None)

        if char_id and char_id in BUILTIN_MAP:
            # Editing a built-in default character
            char_data['builtin'] = True
            char_data['is_customized'] = True
            overrides = dict(self.controller.store.get_state('gemma_character_overrides', {}) or {})
            overrides[char_id] = char_data
            self.controller.store.set_state('gemma_character_overrides', overrides)
            return char_data

        # Creating or editing a custom character
        char_data['builtin'] = False
        char_data['is_customized'] = True
        if not char_data.get('id'):
            char_data['id'] = f"custom_{uuid.uuid4().hex[:12]}"

        custom = list(self.controller.store.get_state('gemma_custom_characters', []))
        idx = next((i for i, c in enumerate(custom) if c.get('id') == char_data['id']), None)
        if idx is not None:
            custom[idx] = char_data
        else:
            custom.append(char_data)

        self.controller.store.set_state('gemma_custom_characters', custom)
        return char_data

    def reset_character_route(self, char_id: str):
        if char_id not in BUILTIN_MAP:
            raise HTTPException(400, '只有内置默认角色支持重置为默认预设')
        overrides = dict(self.controller.store.get_state('gemma_character_overrides', {}) or {})
        if char_id in overrides:
            del overrides[char_id]
            self.controller.store.set_state('gemma_character_overrides', overrides)
        res = dict(BUILTIN_MAP[char_id])
        res['is_customized'] = False
        return res

    def delete_character_route(self, char_id: str):
        if char_id in BUILTIN_MAP:
            raise HTTPException(400, '内置默认角色不可删除，若需还原可点击“重置为默认”')
        custom = list(self.controller.store.get_state('gemma_custom_characters', []))
        new_custom = [c for c in custom if c.get('id') != char_id]
        self.controller.store.set_state('gemma_custom_characters', new_custom)
        return {'ok': True, 'id': char_id}

    def jobs(self):
        return [job for job in self.controller.jobs.values() if job.get('config', {}).get('app_id') == APP_ID]

    def chat_jobs(self, chat_id):
        return [job for job in self.jobs() if job['config'].get('chat_id') == chat_id]

    def chat_job_in_progress(self, job):
        return (job['id'] == self.controller.active or job['status'] in PENDING
                or job['status'] in {'error', 'interrupted'} and not job.get('safe_release'))

    def deployment(self):
        """The current chat task: queued, loading, holding the GPU, or failed.

        A chat task holds the GPU while ``running`` with ``chat_session='ready'``;
        once it finishes (closed, idle, runtime lost) the app is closed.
        """
        latest = max((job for job in self.jobs() if job['config']['operation'] == 'open'),
                     key=lambda job: job['created'], default={})
        if latest.get('id') == self.closed_id or latest.get('status') in SUCCESS:
            return {}
        return latest

    @staticmethod
    def ready(deployment):
        return deployment.get('status') == 'running' and deployment.get('chat_session') == 'ready'

    def last_session_notice(self):
        latest = max((job for job in self.jobs() if job['config']['operation'] == 'open'),
                     key=lambda job: job['created'], default={})
        if latest.get('id') != self.closed_id and latest.get('chat_end') in {'idle', 'lost', 'restart'}:
            return latest.get('message')
        return None

    def submit(self, config, placement):
        if not self.authorized():
            raise HTTPException(401, '请先在设置中授权 Google 账号')
        return self.controller.start(config, placement=placement)

    def snapshot(self, chat_id: ChatId | None = None):
        with self.controller.lock:
            deployment = self.deployment()
            state = 'closed'
            if self.closing:
                state = 'closing'
            elif self.ready(deployment):
                state = 'ready'
            elif deployment.get('status') in PENDING:
                state = 'loading'
            elif deployment:
                state = 'error'
            config = deployment.get('config', {})
            session = self.controller.session or {}
            plan = self.controller.queue_snapshot()
            queue = {entry['id']: entry['position'] for entry in plan}
            waiting_others = sum(1 for entry in plan if entry['app_id'] != APP_ID)
            jobs = sorted(self.jobs(), key=lambda job: job['created'])
            chats = {}
            turns = []
            for job in jobs:
                turn = job['config']
                if not turn.get('chat_id') or job.get('chat_removed'):
                    continue
                cid = turn['chat_id']
                p_text = turn.get('prompt') or ''
                title = ' '.join(p_text.split())[:44] if p_text else ('🖼️ [图片对话]' if turn.get('images') else '新会话')
                chat = chats.setdefault(cid, {'id': cid, 'title': title})
                chat['updated'] = job['created']
                chat['status'] = job['status']
                chat['can_remove'] = chat.get('can_remove', True) and not self.chat_job_in_progress(job)
                if turn.get('character_name'):
                    chat['character_name'] = turn.get('character_name')
                if turn.get('character_id'):
                    chat['character_id'] = turn.get('character_id')
                if cid == chat_id:
                    turns.append({
                        'id': job['id'], 'prompt': turn.get('prompt') or '',
                        'images': turn.get('images') or [],
                        'variant': turn['variant'],
                        'status': job['status'], 'stage': job['stage'], 'message': job['message'],
                        'position': queue.get(job['id']),
                        'reply': (job.get('remote_result') or {}).get('text') if job['status'] in SUCCESS else None,
                        'draft': ((job.get('remote_result') or {}).get('text') or self.controller.text_draft)
                        if self.controller.active == job['id'] or job['status'] in PENDING else '',
                        'can_stop': job['status'] in PENDING | {'error', 'interrupted'} and not job.get('safe_release'),
                        'character_name': turn.get('character_name'),
                        'character_id': turn.get('character_id'),
                    })
            remote_res = deployment.get('remote_result') or {}
            variant = config.get('variant')
            variant_meta = MODELS_META.get(variant)
            multimodal = variant_meta['multimodal'] if variant_meta else True
            return {
                'state': state, 'variant': variant, 'gpu': config.get('gpu'),
                'multimodal': multimodal,
                'models': list(MODELS_META.values()),
                'resident': bool(session.get('app_id') == APP_ID and session.get('gemma_variant') == variant),
                'message': self.close_error or deployment.get('message'),
                'events': deployment.get('events', []) if state in {'loading', 'error'} else [],
                'position': queue.get(deployment.get('id')), 'close_error': self.close_error,
                'has_runtime': session.get('app_id') == APP_ID,
                'endpoint': remote_res.get('endpoint'),
                'waiting_others': waiting_others if state == 'ready' else 0,
                'idle_deadline': self.controller.release_status()['deadline'] if state == 'ready' else None,
                'notice': self.last_session_notice() if state == 'closed' else None,
                'chats': sorted(chats.values(), key=lambda chat: chat['updated'], reverse=True),
                'turns': turns,
                'characters': self.get_characters(),
            }

    def debug(self):
        controller = self.controller
        with controller.lock:
            active = controller.jobs.get(controller.active) or {}
            is_active = active.get('config', {}).get('app_id') == APP_ID
            waiting = [item for item in controller.queue_snapshot() if item['app_id'] == APP_ID]
            queued = waiting[0] if waiting else None
            if is_active:
                job = active
            elif queued:
                job = controller.jobs[queued['id']]
            else:
                job = max((item for item in self.jobs() if not item.get('chat_removed')),
                          key=lambda item: item['created'], default={})
            latest_reply = max(
                (item for item in self.jobs()
                 if not item.get('chat_removed') and item.get('config', {}).get('operation') == 'reply'
                 and (not is_active or item.get('created', 0) >= active.get('created', 0))),
                key=lambda item: item['created'], default={}
            )
            session = controller.session or {}
            connected = session.get('app_id') == APP_ID
            endpoint = session.get('endpoint') if connected else None
            consumer = controller.consumer if endpoint and controller.consumer.get('endpoint') == endpoint else {}
            debug = controller.text_debug
            same_job = bool(job and debug.get('job_id') == job['id'])
            same_runtime = bool(endpoint and debug.get('endpoint') == endpoint)
            show_sample = same_runtime or (same_job and not connected)
            metrics = (
                (job.get('remote_result') or {}).get('metrics')
                or (latest_reply.get('remote_result') or {}).get('metrics')
                or (debug.get('metrics') if same_job or same_runtime else None)
            )
            reply_running = latest_reply.get('status') == 'running'
            return {
                'connected': connected, 'active': is_active, 'closing': self.closing,
                'consumer': consumer.get('state'), 'observed_at': consumer.get('checked_at'),
                'probe_retry': consumer.get('probe_retry'),
                'heartbeat': consumer.get('heartbeat') if consumer.get('app_id') == APP_ID else None,
                'gpu': debug.get('gpu') if show_sample else None,
                'gpu_name': session.get('accelerator') if connected else job.get('config', {}).get('gpu'),
                'sampled_at': debug.get('sampled_at') if show_sample else None,
                'position': queued['position'] if queued and not is_active else None,
                'waiting_count': len(waiting), 'waiting_positions': [item['position'] for item in waiting[:10]],
                'operation': 'reply' if reply_running else job.get('config', {}).get('operation'),
                'chat_session': job.get('chat_session'),
                'status': job.get('status'), 'job_id': job.get('id'),
                'events': job.get('events', []),
                'server_lines': debug.get('lines', []) if same_job or same_runtime else [],
                'metrics': metrics,
                'error': self.close_error or consumer.get('error'),
            }

    def open(self, payload: OpenRequest, placement: Literal['back', 'front'] = 'back'):
        if payload.variant == 'gpt_oss_120b' and payload.gpu in ('L4', 'T4'):
            raise HTTPException(400, 'GPT-OSS 120B 模型（~63GB 权重）需要至少 80GB 显存，不支持 L4 (24GB) / T4 (16GB)，请选用 A100 (80GB) 或 H100 显卡')
        with self.controller.lock:
            old = self.deployment()
            if self.closing or old.get('status') in PENDING | SUCCESS:
                raise HTTPException(409, '大模型对话已开启或正在处理')
            if old and not old.get('safe_release') and old.get('status') != 'discarded':
                raise HTTPException(409, '请先关闭应用，确认上次部署已结束')
            session = self.controller.session or {}
            if (session.get('app_id') == APP_ID and session.get('gemma_variant')
                    and session['gemma_variant'] != payload.variant):
                raise HTTPException(409, '切换模型前请先关闭应用')
            secret = secrets.token_hex(16)
            config = GemmaJobConfig(operation='open', secret=secret, **payload.model_dump())
            job_id = self.submit(config, placement)
            self.close_error = None
            return {'id': job_id}

    def _find_character(self, char_id: str | None, char_name: str | None):
        all_chars = self.get_characters()
        if char_id:
            for c in all_chars:
                if c.get('id') == char_id:
                    return c
        if char_name:
            for c in all_chars:
                if c.get('name') == char_name:
                    return c
        return None

    def _prepare_history(self, chat_id: ChatId, payload: SendRequest):
        deployment = self.deployment()
        if self.closing or self.close_error or not self.ready(deployment):
            raise HTTPException(409, '请先开启应用，等待模型加载完成')
        history = sorted(self.chat_jobs(chat_id), key=lambda job: job['created'])
        if (payload.existing_chat and not history) or any(job.get('chat_removed') for job in history):
            raise HTTPException(404, '这段聊天已移除，请新建聊天')
        if any(job['status'] in PENDING for job in history):
            raise HTTPException(409, '这段对话已有消息正在处理')
        if any(job['status'] in {'error', 'interrupted'} and not job.get('safe_release') for job in history):
            raise HTTPException(409, '上一条消息状态待确认，请先停止回复或关闭应用')

        effective_sys = payload.system_prompt
        effective_char_name = payload.character_name
        effective_char_id = payload.character_id
        if history:
            first_cfg = history[0]['config']
            effective_sys = effective_sys or first_cfg.get('system_prompt')
            effective_char_name = effective_char_name or first_cfg.get('character_name')
            effective_char_id = effective_char_id or first_cfg.get('character_id')

        if not effective_sys:
            char = self._find_character(effective_char_id, effective_char_name)
            if char:
                effective_char_id = char.get('id')
                effective_char_name = char.get('name')
                effective_sys = char.get('system_prompt')

        variant = deployment.get('config', {}).get('variant')
        meta = MODELS_META.get(variant)
        is_multimodal = meta['multimodal'] if meta else True
        if payload.images and not is_multimodal:
            model_name = meta['name'] if meta else variant
            raise HTTPException(400, f'{model_name} 为纯文本模型，不支持多模态图片输入，请使用支持多模态的 Gemma 4 模型')

        messages = []
        if effective_sys:
            messages.append({'role': 'system', 'content': effective_sys})
        for job in [item for item in history if item['status'] in SUCCESS][-12:]:
            cfg = job['config']
            prompt = cfg.get('prompt') or ''
            imgs = cfg.get('images') or []
            if imgs and is_multimodal:
                content = []
                if prompt:
                    content.append({'type': 'text', 'text': prompt})
                for img in imgs:
                    content.append({'type': 'image_url', 'image_url': {'url': img}})
                messages.append({'role': 'user', 'content': content})
            elif imgs and not is_multimodal:
                content_text = prompt or '[用户发送了图片]'
                messages.append({'role': 'user', 'content': content_text})
            else:
                messages.append({'role': 'user', 'content': prompt})
            messages.append({'role': 'assistant', 'content': job['remote_result']['text']})

        curr_prompt = payload.prompt or ('请描述这张图片' if payload.images else '')
        if payload.images:
            content = []
            if curr_prompt:
                content.append({'type': 'text', 'text': curr_prompt})
            for img in payload.images:
                content.append({'type': 'image_url', 'image_url': {'url': img}})
            messages.append({'role': 'user', 'content': content})
        else:
            messages.append({'role': 'user', 'content': curr_prompt})
        return deployment, messages, effective_char_id, effective_char_name, effective_sys

    def stream(self, chat_id: ChatId, payload: SendRequest):
        if not self.authorized():
            raise HTTPException(401, '请先在设置中授权 Google 账号')
        with self.controller.lock:
            deployment, messages, char_id, char_name, sys_prompt = self._prepare_history(chat_id, payload)
            remote_res = deployment.get('remote_result') or {}
            endpoint = remote_res.get('endpoint')
            secret = remote_res.get('secret') or deployment.get('config', {}).get('secret')
            if not endpoint:
                raise HTTPException(502, '模型端点未就绪')
            config = deployment['config']
            job_id = uuid.uuid4().hex
            job = dict(
                id=job_id,
                config=dict(app_id=APP_ID, provider_id='colab', operation='reply',
                            variant=config['variant'], gpu=config['gpu'],
                            chat_id=chat_id, prompt=payload.prompt,
                            images=payload.images, messages=messages,
                            character_id=char_id, character_name=char_name,
                            system_prompt=sys_prompt),
                events=[], status='running', stage='chat',
                message=f'{char_name or "AI"} 正在回复',
                created=time.time(), safe_release=True
            )
            self.controller.jobs[job_id] = job
            self.controller.save_jobs()
            self.controller.touch_chat_session(deployment)

        def stream_generator():
            parts = []
            metrics = {}
            started = time.monotonic()
            headers = {'Authorization': f'Bearer {secret}'} if secret else {}
            model_alias = 'lstudio-gpt-oss-120b' if config['variant'] == 'gpt_oss_120b' else 'lstudio-gemma-' + config['variant']
            body = {
                'model': model_alias,
                'messages': messages,
                'stream': True,
                'max_tokens': 1536,
                'timings_per_token': True,
                'stream_options': {'include_usage': True}
            }
            try:
                with requests.post(f'{endpoint}/v1/chat/completions', json=body, headers=headers, stream=True, timeout=(10, 180)) as resp:
                    if resp.status_code != 200:
                        err_text = ''
                        try:
                            err_json = resp.json()
                            err_text = err_json.get('error', {}).get('message') or str(err_json)
                        except Exception:
                            err_text = resp.text
                        if 'image input is not supported' in err_text or 'mmproj' in err_text:
                            err_msg = '当前远端模型服务未加载视觉投影(mmproj)。请在右上角「关闭应用」后重新「开启应用」，系统将自动加载多模态视觉模型。'
                        else:
                            err_msg = f'模型回复失败({resp.status_code}): {err_text[:200]}'
                        raise RuntimeError(err_msg)

                    for line in resp.iter_lines():
                        if not line.startswith(b'data: '):
                            continue
                        line_str = line.decode('utf-8', errors='replace')
                        if line == b'data: [DONE]':
                            break
                        try:
                            chunk = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if chunk.get('error'):
                            err_text = chunk.get('error', {}).get('message') or str(chunk['error'])
                            if 'image input is not supported' in err_text or 'mmproj' in err_text:
                                err_msg = '当前远端模型服务未加载视觉投影(mmproj)。请在右上角「关闭应用」后重新「开启应用」，系统将自动加载多模态视觉模型。'
                            else:
                                err_msg = f'模型回复失败: {err_text[:200]}'
                            raise RuntimeError(err_msg)

                        choices = chunk.get('choices') or []
                        now = time.monotonic()
                        if choices and (content := choices[0].get('delta', {}).get('content')):
                            parts.append(content)
                            if 'first_text_ms' not in metrics and content.strip():
                                metrics['first_text_ms'] = round((now - started) * 1000)

                        sources = (
                            (chunk.get('timings'), {'predicted_per_second': 'tokens_per_second', 'predicted_n': 'output_tokens', 'prompt_ms': 'prompt_ms'}),
                            (chunk.get('usage'), {'prompt_tokens': 'input_tokens', 'completion_tokens': 'output_tokens'}),
                        )
                        for src, mapping in sources:
                            if isinstance(src, dict):
                                for k, v in mapping.items():
                                    val = src.get(k)
                                    if type(val) in (int, float) and val >= 0:
                                        metrics[v] = val

                        self.controller.touch_chat_session(deployment)
                        with self.controller.lock:
                            self.controller.text_draft = ''.join(parts)
                        yield f"{line_str}\n\n"

                text = ''.join(parts).strip()
                metrics['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                with self.controller.lock:
                    job.update(status='done', stage='done', message='回复已保存到本机',
                               remote_result={'text': text, 'metrics': metrics})
                    self.controller.save_jobs()
                    self.controller.text_draft = ''
                yield "data: [DONE]\n\n"
            except GeneratorExit:
                text = ''.join(parts).strip()
                with self.controller.lock:
                    job.update(status='cancelled', stage='cancelled', message='回复已停止',
                               remote_result={'text': text, 'metrics': metrics} if text else None)
                    self.controller.save_jobs()
                    self.controller.text_draft = ''
            except Exception as exc:
                text = ''.join(parts).strip()
                err_msg = str(exc).strip() or '模型回复失败，请重试'
                with self.controller.lock:
                    job.update(status='error', stage='error', message=err_msg,
                               remote_result={'text': text, 'metrics': metrics} if text else None)
                    self.controller.save_jobs()
                    self.controller.text_draft = ''
                err_payload = json.dumps({'error': {'message': err_msg}}, ensure_ascii=False)
                yield f'data: {err_payload}\n\n'

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    def remove_chat(self, chat_id: ChatId):
        with self.controller.lock:
            jobs = {job['id']: job for job in self.chat_jobs(chat_id)}
            if not jobs:
                return {'ok': True}
            if any(self.chat_job_in_progress(job) for job in jobs.values()):
                raise HTTPException(409, '请先停止这段聊天中的回复，再移除聊天')
            for job_id in jobs:
                del self.controller.jobs[job_id]
            try:
                self.controller.save_jobs()
            except Exception:
                self.controller.jobs.update(jobs)
                raise
            return {'ok': True}

    def close(self):
        with self.controller.lock:
            if not self.closing:
                self.closing = True
                self.close_error = None
                threading.Thread(target=self.finish_close, daemon=True).start()
            return {'ok': True}

    def finish_close(self):
        controller = self.controller
        try:
            with controller.lock:
                deployment_id = self.deployment().get('id')
                for job in self.jobs():
                    if job.get('chat_session') == 'ready':
                        # Ending the chat task frees the queue; it releases the GPU
                        # itself unless other tasks are waiting to take it over.
                        controller.end_chat_session(job['id'], 'close')
                    elif job['status'] == 'queued' or (job['id'] == controller.active and job['status'] in PENDING):
                        controller.cancel(job['id'])
            deadline = time.monotonic() + self.CLOSE_TIMEOUT
            while time.monotonic() < deadline:
                with controller.lock:
                    active = controller.jobs.get(controller.active)
                    if not active or active['config'].get('app_id') != APP_ID:
                        break
                time.sleep(0.25)
            else:
                raise RuntimeError('停止尚未确认，请稍后重试关闭')
            for job in self.jobs_for_cleanup():
                controller.cancel(job['id'])
                if not job.get('safe_release'):
                    raise RuntimeError(job['message'])
            with controller.lock:
                if ((controller.session or {}).get('app_id') == APP_ID and not controller.active
                        and not waiting_jobs(controller.jobs)):
                    controller.check_and_release()
                if deployment_id:
                    controller.store.set_state(APP_ID, deployment_id)
                    self.closed_id = deployment_id
        except HTTPException as exc:
            self.close_error = str(exc.detail)
        except (OSError, RuntimeError) as exc:
            self.close_error = str(exc)
        finally:
            with controller.lock:
                self.closing = False

    def jobs_for_cleanup(self):
        with self.controller.lock:
            return [job for job in self.jobs() if job['status'] in {'error', 'interrupted', 'cancelling'}
                    and not job.get('safe_release')]

"""Local reusable character profiles and deterministic mention expansion."""
import copy
import io
import json
import re
import threading
import uuid
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

IMAGE_ID = r'^[0-9a-f]{32}\.(png|jpg|webp)$'
AUDIO_ID = r'^[0-9a-f]{32}\.(wav|mp3|m4a|ogg|flac|aac)$'
MENTION = re.compile(r'(?<![A-Za-z0-9_@])@([\w-]+)')
PICTURE_MENTION = re.compile(r'(?<![A-Za-z0-9_@])@图片([0-9]+)')


class AudioAsset(BaseModel):
    audio_id: str = Field(pattern=AUDIO_ID)
    name: str = Field(default='声音样本', max_length=120)
    duration: float = Field(ge=2, le=15, allow_inf_nan=False)


class CharacterProfile(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r'^[0-9a-f]{32}$')
    name: str = Field(pattern=r'^[A-Za-z0-9_\u4e00-\u9fff-]{1,30}$')
    description: str = Field(default='', max_length=500)
    voice: str = Field(default='', max_length=300)
    image_ids: list[str] = Field(min_length=1, max_length=9)
    audios: list[AudioAsset] = Field(default_factory=list, max_length=3)
    deleted: bool = False

    @model_validator(mode='after')
    def validate_assets(self):
        if re.fullmatch(r'图片\d+', self.name):
            raise ValueError('角色名称不能使用图片编号，请使用角色名')
        if any(not re.fullmatch(IMAGE_ID, name) for name in self.image_ids):
            raise ValueError('角色参考图编号不合法')
        if len(set(self.image_ids)) != len(self.image_ids):
            raise ValueError('角色参考图不能重复')
        if len({a.audio_id for a in self.audios}) != len(self.audios):
            raise ValueError('声音样本不能重复')
        if sum(a.duration for a in self.audios) > 15.0001:
            raise ValueError('一个角色的声音样本总长不能超过 15 秒')
        return self


class VoiceReference(AudioAsset):
    character_id: str = Field(pattern=r'^[0-9a-f]{32}$')


def mention_names(prompt):
    return list(dict.fromkeys(m.group(1) for m in MENTION.finditer(PICTURE_MENTION.sub('', prompt))))


def validate_audio_asset(assets, asset):
    try:
        path = assets.resolve(asset.audio_id)
        metadata = json.loads(path.with_suffix(path.suffix + '.json').read_text(encoding='utf-8'))
        stat = path.stat()
        if metadata['size'] != stat.st_size or metadata['mtime_ns'] != stat.st_mtime_ns:
            raise ValueError('声音文件已变更')
        return AudioAsset(audio_id=asset.audio_id, name=asset.name, duration=metadata['duration'])
    except (OSError, KeyError, ValueError) as exc:
        raise ValueError('声音样本已丢失或变更，请重新上传：' + asset.name) from exc


class CharacterLibrary:
    def __init__(self, directory, assets):
        self.directory = Path(directory)
        self.path = self.directory / 'characters.json'
        self.lock = threading.RLock()
        self.assets = assets

    def list(self, include_deleted=False):
        with self.lock:
            if not self.path.exists():
                return []
            profiles = [CharacterProfile(**item) for item in json.loads(self.path.read_text(encoding='utf-8'))]
            return profiles if include_deleted else [p for p in profiles if not p.deleted]

    def get(self, profile_id, include_deleted=False):
        with self.lock:
            profile = next((p for p in self.list(include_deleted) if p.id == profile_id), None)
            if profile is None:
                raise KeyError('角色不存在')
            return profile

    def delete(self, profile_id):
        with self.lock:
            profiles = self.list(include_deleted=True)
            profile = next((p for p in profiles if p.id == profile_id and not p.deleted), None)
            if profile is None:
                raise KeyError('角色不存在')
            profile.deleted = True
            self._write(profiles)
            return profile

    def restore(self, profile_id):
        with self.lock:
            profile = self.get(profile_id, include_deleted=True)
            profile.deleted = False
            return self.save(profile)

    def _write(self, profiles):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps([p.model_dump() for p in profiles], ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.path)

    def save(self, profile):
        with self.lock:
            profiles = self.list(include_deleted=True)
            if not profile.deleted and any(not p.deleted and p.id != profile.id and p.name.casefold() == profile.name.casefold() for p in profiles):
                raise ValueError('角色名称已存在，请使用不同名称')
            for image_id in profile.image_ids:
                if not self.assets.exists(image_id):
                    raise ValueError('角色参考图已丢失，请重新上传')
            profile.audios = [validate_audio_asset(self.assets, a) for a in profile.audios]
            profile = CharacterProfile(**profile.model_dump())
            profiles = [p for p in profiles if p.id != profile.id]
            if not profile.deleted and sum(not p.deleted for p in profiles) >= 100:
                raise ValueError('角色库最多保存 100 个角色')
            profiles.append(profile)
            self._write(profiles)
            return profile


def expand_characters(payload, profiles):
    """Freeze only mentioned profiles into the job; retries reuse this snapshot."""
    if not isinstance(payload, dict):
        return payload
    # Check explicit image mentions before library images can occupy unused slots.
    slots = {r.get('slot') for r in payload.get('references', []) if isinstance(r, dict)}
    missing_pictures = sorted({int(m.group(1)) for m in PICTURE_MENTION.finditer(str(payload.get('prompt', '')))} - slots)
    if missing_pictures:
        raise ValueError('引用了未上传的图片：' + '、'.join('@图片' + str(slot) for slot in missing_pictures))
    if payload.get('characters'):
        return payload
    names = mention_names(str(payload.get('prompt', '')))
    if not names:
        return payload
    by_name = {p.name: p for p in profiles}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise ValueError('角色不存在：' + '、'.join('@' + name for name in missing) + '；请从 @ 菜单选择角色，并在名称后留空格')
    data = copy.deepcopy(payload)
    refs = data.setdefault('references', [])
    used_slots = {r['slot'] for r in refs}
    characters, audios = [], []
    for name in names:
        profile = by_name[name]
        characters.append(profile.model_dump())
        for image_id in profile.image_ids:
            if any(r['image_id'] == image_id for r in refs):
                continue
            slot = next((i for i in range(1, 10) if i not in used_slots), None)
            if slot is None:
                raise ValueError('角色图与首尾帧、其他参考图合计最多 9 张，请减少素材')
            used_slots.add(slot)
            refs.append(dict(slot=slot, image_id=image_id, frame='reference', role='character',
                             description=profile.name + '：' + profile.description, character_id=profile.id))
        audios.extend(dict(a.model_dump(), character_id=profile.id) for a in profile.audios)
    if len(audios) > 3 or sum(a['duration'] for a in audios) > 15.0001:
        raise ValueError('本次引用角色的声音样本合计最多 3 段、15 秒，请减少声音样本或引用角色')
    data.update(characters=characters, audio_references=audios, generation_type='image')
    return data


def inspect_audio(data):
    """Decode actual frames, rather than trusting user-supplied duration metadata."""
    import av
    try:
        with av.open(io.BytesIO(data)) as container:
            if len(container.streams.audio) != 1 or container.streams.video:
                raise ValueError('请上传只有一条音轨的音频文件')
            duration = 0.0
            for frame in container.decode(audio=0):
                if len(frame.layout.channels) > 2 or not 8000 <= frame.sample_rate <= 192000:
                    raise ValueError('请上传单声道或立体声的常规采样率音频')
                if frame.format.name in ('u8', 'u8p'):
                    raise ValueError('请将音频转换为 16 位 WAV、MP3 或其他常规格式')
                duration += frame.samples / frame.sample_rate
                if duration > 15.0001:
                    raise ValueError('每段声音样本需为 2–15 秒，请先裁剪')
            duration = round(duration, 9)
            if not 2 <= duration <= 15:
                raise ValueError('每段声音样本需为 2–15 秒')
            return duration
    except av.error.FFmpegError as exc:
        raise ValueError('音频无法解码，请使用有效的 WAV、MP3、M4A、OGG、FLAC 或 AAC 文件') from exc

"""Two-pass Qwen preparation of MiniMax H3 prompts.

Pass 1 understands the user's script in its own language and returns a
structure. Pass 2 translates the descriptive fields into English without ever
seeing the spoken words. This module renders the official H3 sections.

The user's script is the instruction: follow it, do not embellish it. Problems
are reported as warnings so the user can adjust the script; only output that
cannot be rendered at all raises an error. There are no automatic retries.

Format source: MiniMaxAI/MiniMax-H3's upstream docs at GUIDE_REVISION:
VIDEO_PROMPT_WRITING_GUIDE_base_en.md and VIDEO_PROMPT_WRITING_GUIDE_ref_en.md.
"""
import hashlib
import json
import math
import re

VERSION = 'qwen-h3-two-pass-5'
GUIDE_REVISION = '42ed227ee7df40d41602854ae760620d6eb651fe'
PRESENCE = ('on_screen', 'off_screen', 'voiceover', 'narrator')
MARKERS = ('fully_preserved', 'partially_preserved', 'attribute_transfer', 'weak_reference')
PLACEHOLDER = re.compile(r'\{\{\s*([LT]\d+)\s*\}\}')
# Quotation marks and brackets used across writing systems (CJK, Latin, European).
QUOTES = '“”"\'‘’「」『』«»„‟〝〞＂'
BRACKETED = re.compile(r'\s*(?:\(.*\)|（.*）|\[.*\]|【.*】|〔.*〕|［.*］)\s*', re.S)
SPEECH_LEAD_IN = re.compile(
    r'(?:,\s*(?:and\s+)?(?:playing|saying|responding|replying|speaking|shouting|whispering|asking|calling|'
    r'exclaiming|muttering|roaring|yelling|barking|snapping|crying|screaming|growling|uttering|adding|continuing)\b[^.;:!?]*'
    r'|\s+and\s+(?:says|responds|replies|shouts|whispers|asks|speaks|exclaims|roars|yells|plays|barks|snaps|cries|screams|growls|utters|adds|continues)\b[^.;:!?]*):\s*$',
    re.I,
)
CN_SPEECH_LEAD_IN = re.compile(
    r'(?:[，,；;]\s*[^，。；！？,.;!?\n<>]{0,24}?(?:声音|女声|男声|童声|道|说|问|答|喊|叫|吼|斥|喝)'
    r'|(?:<Subject \d+>\s*)?[^，。；！？,.;!?\n<>]{0,10}?(?:道|说|问|答|喊|叫|吼))\s*[：:]\s*$'
)

UNDERSTAND_PROMPT = """You read a user's video script for the MiniMax H3 video model and return how you understand it, as one JSON object.
The script is the user's instruction. Follow it as written: do not add events, characters, dialogue, shots, music or an ending it does not ask for, and do not drop or soften anything it asks for. Keep every field in the script's own language; do not translate.

Input: original_prompt, task (T2VA, I2VA, FL2VA, L2VA or Ref2VA), duration_seconds, pictures (label, slot, frame, role, description) and characters (subject, id, name, description, voice, pictures). In the script, @name refers to a character and @图片N to the picture with slot N; replace @name with <Subject N> and @图片N with <Picture N>, while keeping any parenthetical appearance, expression or clothing notes written next to @name in the script.

Return exactly this shape:
{"style": "", "shots": [{"start": 0, "timing": "script", "description": ""}],
 "speakers": [{"id": "P1", "name": "", "kind": "person", "character_id": null, "voice": ""}],
 "lines": [{"id": "L1", "kind": "speech", "speakers": ["P1"], "presence": "on_screen", "language": "", "source": "verbatim", "text": "", "delivery": ""}],
 "visible_text": [{"id": "T1", "text": ""}],
 "sound": "", "music": "", "notes": []}

style: the visual style, aspect ratio and lighting contrast the script states; empty if it states none.
shots: playback order. Start a new shot only where the script states or clearly implies a camera cut; continuous camera movement is not a cut. The first shot starts at 0. For a later shot, start is its cut time in seconds: timing "script" when the script gives the time, otherwise "estimated" with a time you choose within duration_seconds.
description: everything visible or audible in that shot as the script describes it: shot framing/angle (including camera directions inside bracketed shot headers such as 俯拍特写, 侧面近景, 反打正面近景, 过肩特写), subjects and their described look, actions, camera movement, sounds. Put {{L1}} where line L1 is heard and {{T1}} where text T1 is shown on screen. Only use {{Ln}} placeholders for kind "speech" lines; keep non-verbal sounds and actions directly in description. Each {{Ln}} replaces the entire speaking clause (the speaker name, voice description, speaking verb such as "外放出…的声音：" or "语速极快地回复道：", colon and quoted words); keep physical actions before or after speaking (such as a phone vibrating/popping up a message, or pressing/releasing a button), and do not leave a speaking verb or colon right before {{Ln}}.
speakers: every distinct person or character voice that actually speaks or sings, including people heard from off screen, voices from phones or loudspeakers, and narrators. A person who is only addressed or mentioned is not a speaker. kind: "person" for a human, character, narrator or vocal speaker; "sound" if the source is a non-talking machine, alarm, device, object or environment that only makes non-verbal noise. character_id: the supplied character's id, otherwise null. voice: how the script or the character's voice field describes this voice; empty if neither does.
lines: every spoken or sung line, in playback order, plus every quoted span you considered and rejected as speech (so the decision is visible). kind: "speech" only for words a person or character actually utters (including a voice played from a phone or loudspeaker); "sound" for a non-verbal noise written as text, in any language or script (onomatopoeia, beeps, ringtones, impacts, mechanical noise); "stage_direction" for an action, emotion or physical description, often written inside brackets. Quotation marks in any language are not evidence of speech by themselves: writers quote sounds, key names, UI labels and screen text too. Quoted key names, UI labels and on-screen text belong in visible_text, not lines. Decide by meaning, not by punctuation or language. Only kind "speech" is voiced by the video model. speakers: one id, or several ids when they speak together; for kind "sound" or "stage_direction" use an empty list. presence: on_screen (seen speaking), off_screen (heard from outside the frame, including through a phone, radio or loudspeaker), voiceover (inner voice or narration of a character who is on screen with closed lips), narrator (a narrator who is not in the scene). language: English name of the spoken language.
source "verbatim": the script gives the exact words; copy them character for character, without surrounding quotation marks, speaker labels or <d> tags. source "derived": the script clearly wants the words to be heard but does not give them exactly (for example reported speech); write them as close to the script's wording as possible. If the script only says that someone talks, without asking for audible words, keep that as an action in the description instead of a line.
delivery: how the script says the line is delivered; empty if it does not say.
visible_text: any literal text or character string shown or typed on a screen, UI, sign or object in the scene (for example "zzzzzzzzzzzz……"), copied character for character.
sound: ambience and physical or non-verbal sounds the script mentions, including quoted onomatopoeia; no speech.
music: background score only the audience hears, only if the script asks for it; otherwise empty. Music the characters can hear belongs in a shot description.
notes: short remarks on anything ambiguous.
Return JSON only, without markdown fences or explanations."""

TRANSLATE_PROMPT = """You turn an understood video script into the English fields of a MiniMax H3 prompt and return one JSON object.
Translate faithfully: keep every event, action, camera angle/move, character detail, sound and relationship from the script, and add no events, characters, dialogue, shots, music or ending. You may describe a subject's intrinsic appearance and clothing from the attached pictures, because the pictures are part of the user's instruction.

Input: task, duration_seconds, style, shots (descriptions with {{Ln}} and {{Tn}} placeholders), speakers, lines (ids, speakers, presence and delivery; the spoken words are hidden), visible_text ids, sound, music, pictures (with label, frame, role, subject and description), and for reference tasks subjects and frame_pictures. The images themselves are attached in pictures order.

Return exactly this shape:
{"summary": "", "subjects": {"<Subject 1>": {"definition": "", "marker": "fully_preserved", "retention": ""}},
 "pictures": {}, "style": "", "shots": [""], "speakers": {"P1": {"intro": "", "short": ""}},
 "deliveries": {"L1": ""}, "soundscape": "", "music": "N/A"}

Picture types (check each entry's frame field in pictures):
- frame "reference": an asset reference that supplies appearance/clothing/style traits for its <Subject N>. It is NEVER a video frame or scene in the target video: never write "the shot begins from <Picture N>" for it, never mention <Picture N> in shots, summary or speakers (use <Subject N> only in the shots where that subject actually appears), and cite <Picture N> only inside subjects[label].definition. If the reference image is a character turnaround/sheet, multi-view collage or cutout, extract only the character's intrinsic look (face, hair, build, outfit) and never mention "character sheet", "multiple views", "turnaround" or reference-sheet layout anywhere.
- frame "first" or "last": a concrete video keyframe anchor. Write "the shot begins from <Picture N>" in Shot 1 when frame is "first", and "the shot ends on <Picture N>" in the final shot when frame is "last".

style: comma-separated English descriptors without a leading article, such as "live-action, cinematic, soft daylight". If the script gives no style, derive it from the attached pictures when there are any; otherwise leave it empty.
shots: one English text per input shot, same order and count. Keep every {{Ln}}, {{Tn}}, <Subject N> and keyframe <Picture N> in its shot. Each {{Ln}} is expanded by the renderer into a complete standalone sentence "<speaker> (Sx), <voice/audio>, <delivery>: <d>words</d>", so place {{Ln}} as its own standalone sentence or clause (for example "... lights up with a WeChat voice message. {{L1}}" or "... presses the voice button on the phone. {{L2}} He then releases the button.") and NEVER write a speaking/playing participle, speaker description or colon ":" right before {{Ln}}. Begin every shot after the first with a transition such as "the camera cuts to ...". Translate shot framing accurately (overhead close-up, side close-up, reverse-angle front close-up, over-the-shoulder close-up) and write camera motion using push in, pull out, pan, truck, tilt, pedestal, arc shot, tracking shot, static shot, zoom, POV or shake.
speakers: when a speaker has a <Subject N> (subject is not null), intro must contain only voice traits if any (never visual appearance or <Picture N>), and short is "<Subject N>". When subject is null, intro is the English identity used at the speaker's first line (for example "the mother with a gentle, warm voice") and short is a brief later reference ("the mother").
deliveries: an English verb phrase per line, such as "says", "responds quickly and dismissively" or "shouts urgently"; use "says" when no delivery is given.
soundscape: 1-3 English sentences summarizing only the environment ambience and physical/mechanical action sounds across the video. Never mention spoken voices, voice messages or dialogue playback in soundscape. "N/A" only if the script asks for complete silence.
music: 1-3 English sentences on instruments, tempo and dynamics when input music is given; otherwise "N/A".
Reference tasks only: subjects gives each listed <Subject N> a definition that completes "<Subject N> is ..." from its reference pictures and script description and cites its <Picture N> labels, a marker (fully_preserved unless the script changes the subject; then partially_preserved, attribute_transfer or weak_reference) and a short English retention note. pictures describes only frame_pictures (frame "first" or "last"); leave pictures empty {} when frame_pictures is empty. summary is one short English paragraph about the target video story and how <Subject N> / <Audio N> / keyframe <Picture N> are used.
Every output text field (style, shots, speakers, deliveries, soundscape, music, subjects, pictures, summary) MUST be written in English; never copy Chinese or non-English source sentences into style, shots, deliveries or soundscape, and translate any shot framing header into English camera framing instead of copying Chinese brackets.
Return JSON only, without markdown fences or explanations."""

PRESENCE_CN = dict(on_screen='画面内', off_screen='画外', voiceover='画外音/内心独白', narrator='旁白')


def task_of(mode, pictures):
    if mode == 'Ref2VA':
        return 'Ref2VA'
    frames = {p.get('frame') for p in pictures}
    if 'first' in frames and 'last' in frames:
        return 'FL2VA'
    return 'L2VA' if 'last' in frames else 'I2VA' if 'first' in frames else 'T2VA'


def prompt_request(c):
    # Match remote_h3.ordered_images without importing the remote execution module.
    ref_mode = bool(c.get('characters') or c.get('audio_references')) or any(
        r.get('frame', 'reference') == 'reference' for r in c.get('references', []))
    refs = sorted(c.get('references', []), key=(lambda r: r['slot']) if ref_mode
                  else (lambda r: 0 if r.get('frame') == 'first' else 1))
    pictures = [dict(label=f'Picture {i+1}', **r) for i, r in enumerate(refs)]
    mode = 'Ref2VA' if ref_mode else 'Base'
    return dict(original_prompt=c['prompt'], mode=mode, task=task_of(mode, pictures),
                duration_seconds=(math.ceil((c['duration'] * 24 - 5) / 17) * 17 + 5) / 24,
                pictures=pictures, characters=c.get('characters', []), audios=c.get('audio_references', []))


def reference_subjects(request):
    """Characters first (stable <Subject N>), then non-frame pictures not owned by a character."""
    if request['mode'] != 'Ref2VA':
        return []
    chars = request['characters']
    subjects = [dict(label=f'<Subject {i+1}>', kind='character', name=ch['name'], description=ch.get('description', ''),
                     pictures=['<' + p['label'] + '>' for p in request['pictures'] if p['image_id'] in ch['image_ids']],
                     character_id=ch['id'])
                for i, ch in enumerate(chars)]
    owned = {image for ch in chars for image in ch['image_ids']}
    for p in request['pictures']:
        if p.get('frame', 'reference') == 'reference' and p['image_id'] not in owned:
            subjects.append(dict(label=f'<Subject {len(subjects)+1}>', kind=p.get('role', 'other'), name='',
                                 description=p.get('description', ''), pictures=['<' + p['label'] + '>'],
                                 slot=p['slot']))
    return subjects


def frame_pictures(request):
    return [p for p in request['pictures'] if p.get('frame') in ('first', 'last')]


def understanding_context(request):
    """What pass 1 sees: the script and asset roles, never audio files or sample words."""
    return dict(original_prompt=request['original_prompt'], task=request['task'],
                duration_seconds=request['duration_seconds'],
                pictures=[{'label': '<' + p['label'] + '>', **{k: p[k] for k in ('slot', 'frame', 'role', 'description') if k in p}}
                          for p in request['pictures']],
                characters=[{'subject': f'<Subject {i+1}>', **{k: ch[k] for k in ('id', 'name', 'description', 'voice') if k in ch},
                             'pictures': ['<' + p['label'] + '>' for p in request['pictures'] if p['image_id'] in ch['image_ids']]}
                            for i, ch in enumerate(request['characters'])])


def request_fingerprint(request, encoder_revision, vendor_revision):
    payload = dict(request=request, version=VERSION, prompts=[UNDERSTAND_PROMPT, TRANSLATE_PROMPT],
                   guide=GUIDE_REVISION, encoder_revision=encoder_revision, vendor_revision=vendor_revision)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def repair_json_text(candidate):
    """Progressively apply heuristics to repair common LLM JSON syntax issues."""
    try:
        return json.loads(candidate, strict=False)
    except Exception:
        pass

    repaired = candidate
    # Heuristic A: Missing closing ASCII quote after Chinese/fullwidth right quotes before delimiter
    repaired = re.sub(r'([”』」）】]\s*),\s*"', r'\1", "', repaired)
    repaired = re.sub(r'([”』」）】]\s*)\n\s*"', r'\1",\n"', repaired)
    repaired = re.sub(r'([”』」）】]\s*)([}\]])', r'\1"\2', repaired)

    try:
        return json.loads(repaired, strict=False)
    except Exception:
        pass

    # Heuristic B: Trailing commas: ,} -> } and ,] -> ]
    repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
    try:
        return json.loads(repaired, strict=False)
    except Exception:
        pass

    # Heuristic C: Missing commas between lines of strings or objects
    repaired = re.sub(r'"\s*\n\s*"', '",\n"', repaired)
    repaired = re.sub(r'}\s*\n\s*{', '},\n{', repaired)
    repaired = re.sub(r'}\s*\n\s*"', '},\n"', repaired)
    repaired = re.sub(r']\s*\n\s*"', '],\n"', repaired)
    try:
        return json.loads(repaired, strict=False)
    except Exception:
        pass

    # Heuristic D: Unclosed brackets or braces due to truncation
    def close_unbalanced(s):
        stack = []
        in_string = False
        escape = False
        for ch in s:
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if not in_string:
                if ch in '{[':
                    stack.append(ch)
                elif ch == '}' and stack and stack[-1] == '{':
                    stack.pop()
                elif ch == ']' and stack and stack[-1] == '[':
                    stack.pop()
        if in_string:
            s += '"'
        while stack:
            opener = stack.pop()
            s += '}' if opener == '{' else ']'
        return s

    closed = close_unbalanced(repaired)
    try:
        return json.loads(closed, strict=False)
    except Exception:
        pass

    return None


def decode_plan(text):
    value = text.strip()
    if value.startswith('```'):
        value = re.sub(r'^```(?:json)?\s*', '', value, flags=re.I)
        value = re.sub(r'\s*```$', '', value)
    start = value.find('{')
    if start < 0:
        raise ValueError('Qwen 输出中没有 JSON 对象')
    end = value.rfind('}')
    candidate = value[start:end + 1] if end >= start else value[start:]
    try:
        parsed = json.loads(candidate)
    except Exception as first_exc:
        repaired = repair_json_text(candidate)
        if repaired is not None:
            parsed = repaired
        else:
            raise first_exc
    if not isinstance(parsed, dict):
        raise ValueError('Qwen 输出必须是 JSON 对象')
    return parsed


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _compact(text):
    return re.sub(r'\s+', '', text)


def _speech_seconds(text):
    cjk = len(re.findall(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]', text))
    words = len(re.findall(r'[A-Za-z0-9]+', text))
    return cjk / 4.5 + words / 2.5


def prepare_understanding(raw, request):
    """Normalize pass 1. Structural problems that cannot be rendered raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError('理解结果必须是 JSON 对象')
    warnings = ['Qwen 备注：' + _text(n) for n in raw.get('notes') or [] if _text(n)]
    duration = request['duration_seconds']
    original = request['original_prompt']
    compact_original = _compact(original)
    if not isinstance(raw.get('shots'), list) or not raw['shots']:
        raise ValueError('理解结果缺少镜头')
    shots = []
    for index, shot in enumerate(raw['shots'], 1):
        if isinstance(shot, str):
            shot = dict(start=0.0 if index == 1 else None, description=shot, timing='estimated')
        if not isinstance(shot, dict) or not isinstance(shot.get('description'), str):
            raise ValueError(f'镜头 {index} 缺少描述')
        shots.append(dict(start=_number(shot.get('start')), description=shot['description'],
                          timing='script' if shot.get('timing') == 'script' else 'estimated'))
    shots[0].update(start=0.0, timing='script')
    for index in range(1, len(shots)):
        previous, start = shots[index - 1]['start'], shots[index]['start']
        if start is None or start <= previous or start >= duration:
            fixed = round(previous + (duration - previous) / (len(shots) - index + 1), 3)
            warnings.append(f'镜头 {index+1} 的切换时间无效（{shots[index]["start"]}），已改为 {fixed:.2f} 秒')
            shots[index].update(start=fixed, timing='estimated')

    characters = {ch['id']: ch for ch in request['characters']}
    by_name = {ch['name']: ch['id'] for ch in request['characters']}
    speakers, speaker_by_name = {}, {}
    for item in raw.get('speakers') or []:
        if not isinstance(item, dict) or not _text(item.get('id') or item.get('name')):
            raise ValueError('说话人缺少 id')
        pid = _text(item.get('id')) or f'P{len(speakers)+1}'
        name = _text(item.get('name')) or pid
        cid = item.get('character_id')
        if cid is not None and cid not in characters:
            cid = by_name.get(str(cid)) or by_name.get(name)
            if cid is None:
                warnings.append(f'说话人 {pid} 引用了不存在的角色 {item.get("character_id")}，按普通说话人处理')
        elif cid is None and name in by_name:
            cid = by_name[name]
        spk_kind = _text(item.get('kind')).lower() or 'person'
        speakers[pid] = dict(id=pid, name=name, kind=spk_kind, character_id=cid, voice=_text(item.get('voice')))
        speaker_by_name[name] = pid

    def resolve_speaker(token):
        token = _text(token)
        if token in speakers:
            return token
        if token in speaker_by_name:
            return speaker_by_name[token]
        if token:
            pid = f'P{len(speakers)+1}'
            speakers[pid] = dict(id=pid, name=token, kind='person', character_id=by_name.get(token), voice='')
            speaker_by_name[token] = pid
            warnings.append(f'说话人 {token} 未在 speakers 列表中单独声明，已自动登记为 {pid}')
            return pid
        return None

    lines = {}
    for index, item in enumerate(raw.get('lines') or [], 1):
        if not isinstance(item, dict):
            raise ValueError('台词条目必须是对象')
        lid = _text(item.get('id')) or f'L{index}'
        if not re.fullmatch(r'L\d+', lid):
            raise ValueError('台词编号必须是 L1、L2 这样的格式')
        if lid in lines:
            raise ValueError(f'台词编号重复：{lid}')
        text = _text(item.get('text')).strip(QUOTES)
        if not text:
            raise ValueError(f'台词 {lid} 内容为空')
        raw_ids = item.get('speakers') if 'speakers' in item else item.get('speaker')
        raw_ids = [raw_ids] if isinstance(raw_ids, str) else raw_ids
        kind = _text(item.get('kind')).lower() or 'speech'
        if kind == 'speech' and BRACKETED.fullmatch(text):
            # Structural, script-independent guard: a "line" that is only a
            # bracketed note has no words to pronounce.
            kind = 'stage_direction'
        if kind == 'speech' and isinstance(raw_ids, list) and raw_ids:
            resolved_ids = [resolve_speaker(s) for s in raw_ids]
            if all(s in speakers and speakers[s].get('kind') == 'sound' and not speakers[s].get('character_id')
                   for s in resolved_ids if s is not None):
                kind = 'sound'
        if kind != 'speech':
            label = '音效' if kind == 'sound' else '动作说明'
            warnings.append(f'{lid}（{text}）被识别为{label}，不作为台词发音')
            for s in shots:
                s['description'] = re.sub(r'\s*\{\{\s*' + re.escape(lid) + r'\s*\}\}', '', s['description'])
            if kind == 'sound' and _compact(text) not in _compact(_text(raw.get('sound'))):
                raw['sound'] = '；'.join(filter(None, [_text(raw.get('sound')), text]))
            continue
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError(f'台词 {lid} 没有说话人')
        ids = [resolve_speaker(s) for s in raw_ids]
        if any(s is None for s in ids):
            raise ValueError(f'台词 {lid} 的说话人无效')
        presence = item.get('presence')
        if presence not in PRESENCE:
            warnings.append(f'台词 {lid} 的出声方式 {presence!r} 无法识别，按画面内处理')
            presence = 'on_screen'
        language = _text(item.get('language'))
        if not language:
            language = 'Chinese' if re.search(r'[\u3400-\u9fff]', text) else 'English'
            warnings.append(f'台词 {lid} 未标语言，按 {language} 处理')
        source = 'derived' if item.get('source') == 'derived' else 'verbatim'
        names = '、'.join(speakers[s]['name'] for s in ids)
        if source == 'derived':
            warnings.append(f'{lid}（{names}）为转述生成的台词，原文没有逐字给出：{text}')
        elif _compact(text) not in compact_original:
            warnings.append(f'{lid}（{names}）标为原文，但与原文不一致：{text}')
        lines[lid] = dict(id=lid, speakers=ids, presence=presence, language=language, source=source,
                          text=text, delivery=_text(item.get('delivery')))

    used_sids = {sid for line in lines.values() for sid in line['speakers']}
    speakers = {sid: spk for sid, spk in speakers.items() if sid in used_sids or spk.get('character_id')}

    texts = {}
    for index, item in enumerate(raw.get('visible_text') or [], 1):
        if not isinstance(item, dict) or not _text(item.get('text')):
            raise ValueError('画面文字需要 T1、T2 这样的编号和内容')
        tid = _text(item.get('id')) or f'T{index}'
        if not re.fullmatch(r'T\d+', tid):
            raise ValueError('画面文字需要 T1、T2 这样的编号和内容')
        text = _text(item['text']).strip(QUOTES)
        if _compact(text) not in compact_original:
            warnings.append(f'画面文字 {tid} 与原文不一致：{text}')
        texts[tid] = text

    known = set(lines) | set(texts)
    seen = []
    for shot in shots:
        def keep_first(match):
            key = match[1]
            if key not in known:
                raise ValueError(f'镜头描述引用了不存在的编号 {key}')
            if key in seen:
                warnings.append(f'{key} 在镜头中出现多次，只保留第一次')
                return ''
            seen.append(key)
            return '{{' + key + '}}'
        shot['description'] = PLACEHOLDER.sub(keep_first, shot['description'])

    q_cls = '[' + re.escape(QUOTES) + ']'
    seen_set = set(seen)
    ordered_keys = sorted(known, key=lambda k: (0 if k.startswith('L') else 1, int(k[1:])))
    # First pass: place all unplaced keys in-place before stripping any duplicate quotes
    for key in ordered_keys:
        if key in seen_set:
            continue
        target = lines[key]['text'] if key in lines else texts[key]
        if not target:
            continue
        quoted_pat = re.compile(q_cls + r'\s*[—…·.\s]*' + re.escape(target) + r'[—…·.!?;！？。；\s]*' + q_cls)
        placed = False
        for idx, shot in enumerate(shots):
            desc = shot['description']
            if quoted_pat.search(desc):
                shot['description'] = re.sub(r'[ \t]{2,}', ' ', quoted_pat.sub(' {{' + key + '}} ', desc, count=1)).strip()
                seen_set.add(key)
                warnings.append(f'{key} 在镜头 {idx+1} 中未写占位符，已自动将原文引号内容原位替换为 {{{{{key}}}}}')
                placed = True
                break
        if not placed and len(target) >= 4:
            for idx, shot in enumerate(shots):
                desc = shot['description']
                if target in desc:
                    shot['description'] = desc.replace(target, '{{' + key + '}}', 1)
                    seen_set.add(key)
                    warnings.append(f'{key} 在镜头 {idx+1} 中未写占位符，已自动将原文原位替换为 {{{{{key}}}}}')
                    placed = True
                    break

    # Second pass: strip duplicate literal quotes that appear alongside an already-placed {{key}}
    for key in ordered_keys:
        if key not in seen_set:
            continue
        target = lines[key]['text'] if key in lines else texts[key]
        if not target:
            continue
        quoted_pat = re.compile(q_cls + r'\s*' + re.escape(target) + r'\s*' + q_cls)
        for shot in shots:
            if '{{' + key + '}}' in shot['description'] and quoted_pat.search(shot['description']):
                shot['description'] = quoted_pat.sub('', shot['description'])

    seen = []
    for shot in shots:
        for key in PLACEHOLDER.findall(shot['description']):
            if key not in seen:
                seen.append(key)

    unplaced = sorted(known - set(seen), key=lambda k: (k[0], int(k[1:])))
    unplaced_lines = [k for k in unplaced if k in lines]
    if len(unplaced_lines) >= 2 and len(shots) > 1:
        raise ValueError(f'多句台词（{", ".join(unplaced_lines)}）未能定位到具体镜头，无法确定发声时间点')
    for key in unplaced:
        warnings.append(f'{key} 没有放进任何镜头，已放到最后一个镜头末尾')
        shots[-1]['description'] += ' {{' + key + '}}'
        seen.append(key)
    order = [key for key in seen if key in lines]

    spoken = sum(_speech_seconds(lines[key]['text']) for key in order)
    if spoken > duration:
        warnings.append(f'台词估计需要约 {spoken:.1f} 秒，超过视频时长 {duration:.2f} 秒，可能说不完')
    return dict(style=_text(raw.get('style')), shots=shots, speakers=speakers, lines=lines, order=order,
                visible_text=texts, sound=_text(raw.get('sound')), music=_text(raw.get('music')), warnings=warnings)


def translation_context(understanding, request):
    """What pass 2 sees. Spoken words and visible text are hidden behind placeholders."""
    used = {sid for key in understanding['order'] for sid in understanding['lines'][key]['speakers']}
    ref_subjects = reference_subjects(request)
    subjects = {s['character_id']: s['label'] for s in ref_subjects if s.get('character_id')}
    owner_by_pic = {pic: s['label'] for s in ref_subjects for pic in s['pictures']}
    pictures = [dict(label='<' + p['label'] + '>', frame=p.get('frame', 'reference'),
                     role=p.get('role', 'other'), subject=owner_by_pic.get('<' + p['label'] + '>'),
                     description=p.get('description', ''))
                for p in request['pictures']]
    context = dict(task=request['task'], duration_seconds=request['duration_seconds'], style=understanding['style'],
                   shots=[dict(shot=i + 1, start=s['start'],
                               description=re.sub(r'^\s*【([^】]+)】\s*', r'（\1）', s['description']))
                          for i, s in enumerate(understanding['shots'])],
                   speakers=[dict(id=s['id'], name=s['name'], voice=s['voice'], subject=subjects.get(s['character_id']))
                             for s in understanding['speakers'].values() if s['id'] in used],
                   lines=[{k: understanding['lines'][key][k] for k in ('id', 'speakers', 'presence', 'delivery')}
                          for key in understanding['order']],
                   visible_text=sorted(understanding['visible_text']), sound=understanding['sound'],
                   music=understanding['music'], pictures=pictures)
    if request['mode'] == 'Ref2VA':
        context['subjects'] = [{k: s[k] for k in ('label', 'kind', 'name', 'description', 'pictures')}
                               for s in ref_subjects]
        context['frame_pictures'] = [dict(label='<' + p['label'] + '>', frame=p['frame']) for p in frame_pictures(request)]
    return context


def _mentions(value, request):
    for index, ch in enumerate(request['characters'], 1):
        value = re.sub(r'(?<![A-Za-z0-9_@])@' + re.escape(ch['name']) + r'(?![\w-])', f'<Subject {index}>', value)
    for p in request['pictures']:
        value = re.sub(r'@图片\s*' + str(p['slot']) + r'(?!\d)', '<' + p['label'] + '>', value)
    return value


def _spoken(text, request):
    # Authoring @ is not something a character should pronounce. No other edits.
    for ch in request['characters']:
        text = re.sub(r'(?<![A-Za-z0-9_@])@' + re.escape(ch['name']) + r'(?![\w-])', ch['name'], text)
    return text


def _sentence(text):
    text = text.strip()
    return text if not text or text[-1] in '.!?' else text + '.'


def _timestamp(seconds):
    return f'{int(seconds // 60):02d}:{seconds % 60:06.3f}'


def _capitalize(text):
    return text[:1].upper() + text[1:]


def _lower_article(text):
    word = text.split(' ', 1)[0]
    return text[:1].lower() + text[1:] if word in ('A', 'An', 'The') else text


def _style_sentence(style):
    if not style:
        return ''
    if re.match(r'^the target video\b', style, re.I):
        return _sentence(_capitalize(style))
    if re.search(r'\bstyle\b', style, re.I):
        return _sentence(f'The target video features {_lower_article(style)}')
    return f'The target video is in a {_lower_article(style)} style.'


def _english_delivery(verb):
    verb = _text(verb).rstrip(':,：，')
    if not verb:
        return 'says'
    if not re.search(r'[\u3400-\u9fff]', verb) or re.search(r'[A-Za-z]{2,}', verb):
        return verb
    if '问' in verb:
        return 'asks'
    if any(w in verb for w in ('回', '答')):
        return 'replies'
    if any(w in verb for w in ('喊', '叫', '吼')):
        return 'shouts'
    if any(w in verb for w in ('低语', '耳语')):
        return 'whispers'
    return 'says'


def render(understanding, translation, request, strict_english=False):
    """Pass 2 output plus pass 1 structure -> official H3 prompt text and audio bindings."""
    if not isinstance(translation, dict):
        raise ValueError('翻译结果必须是 JSON 对象')
    u, warnings = understanding, list(understanding['warnings'])
    raw_shots = translation.get('shots')
    if not isinstance(raw_shots, list) or len(raw_shots) != len(u['shots']):
        raise ValueError(f'翻译结果镜头数量应为 {len(u["shots"])}')
    shots = [s.get('description') or s.get('text') if isinstance(s, dict) else s for s in raw_shots]
    if not all(isinstance(s, str) for s in shots):
        raise ValueError('翻译结果每个镜头必须是字符串')
    known = set(u['lines']) | set(u['visible_text'])
    seen = set()
    for index, text in enumerate(shots):
        text = re.sub(r'^\s*【[^】]+】\s*', '', text)
        if '<d>' in text:
            warnings.append(f'翻译结果在镜头 {index+1} 中自行写入了 <d> 台词标记，已原样保留')
        def keep_first(match):
            if match[1] not in known:
                warnings.append(f'翻译结果引用了不存在的编号 {match[1]}，已忽略')
                return ''
            if match[1] in seen:
                return ''
            seen.add(match[1])
            return '{{' + match[1] + '}}'
        shots[index] = PLACEHOLDER.sub(keep_first, text)
    for index, shot in enumerate(u['shots']):
        for key in PLACEHOLDER.findall(shot['description']):
            if key not in seen:
                warnings.append(f'翻译结果遗漏了 {key}，已补在镜头 {index+1} 末尾')
                shots[index] = shots[index].rstrip() + ' {{' + key + '}}'
                seen.add(key)

    deliveries_check = translation.get('deliveries') if isinstance(translation.get('deliveries'), dict) else {}
    untranslated_reasons = []
    for key in u['order']:
        verb_check = _text(deliveries_check.get(key))
        if re.search(r'[\u3400-\u9fff]', verb_check) and not re.search(r'[A-Za-z]{2,}', verb_check):
            untranslated_reasons.append(f'翻译结果中 {key} 的语气描述未翻译为英文：{verb_check}')
    for index, text in enumerate(shots):
        stripped = _mentions(PLACEHOLDER.sub('', re.sub(r'<d>.*?</d>', '', text, flags=re.S)), request)
        cjk_count = len(re.findall(r'[\u3400-\u9fff]', stripped))
        en_words = len(re.findall(r'[A-Za-z]{2,}', stripped))
        if cjk_count > 6 and cjk_count > en_words:
            untranslated_reasons.append(f'翻译结果镜头 {index+1} 未翻译为英文')
    for field_name in ('style', 'soundscape'):
        field_val = _mentions(_text(translation.get(field_name)), request)
        cjk_count = len(re.findall(r'[\u3400-\u9fff]', field_val))
        en_words = len(re.findall(r'[A-Za-z]{2,}', field_val))
        if cjk_count > 4 and cjk_count > en_words:
            untranslated_reasons.append(f'翻译结果 {field_name} 未翻译为英文')
    if untranslated_reasons:
        if strict_english:
            raise ValueError(untranslated_reasons[0])
        warnings.append('翻译结果部分描述保留了中文，已自动规范化台词发声引导')

    subjects = reference_subjects(request)
    subject_by_character = {s['character_id']: s['label'] for s in subjects if s.get('character_id')}

    # Global speaker IDs follow the order of actual vocal events.
    sid_of, speakers_out = {}, []
    for key in u['order']:
        for pid in u['lines'][key]['speakers']:
            sp = u['speakers'][pid]
            identity = ('character', sp['character_id']) if sp['character_id'] else ('speaker', pid)
            if identity not in sid_of:
                sid_of[identity] = 'S' + str(len(speakers_out) + 1)
                speakers_out.append(dict(id=sid_of[identity], name=sp['name'], character_id=sp['character_id']))
    def sid(pid):
        sp = u['speakers'][pid]
        return sid_of[('character', sp['character_id']) if sp['character_id'] else ('speaker', pid)]

    definitions, retained, indices, bindings, unused = [], [], [], [], []
    for index, audio in enumerate(request['audios']):
        cid = audio['character_id']
        speaker = sid_of.get(('character', cid))
        if not speaker or cid not in subject_by_character:
            unused.append(dict(audio_id=audio['audio_id'], character_id=cid, reason='该角色没有实际台词，本次不传入音色样本'))
            continue
        label = f'<Audio {len(indices)+1}>'
        indices.append(index)
        definitions.append(f'{label} is the voice-timbre reference for {subject_by_character[cid]} ({speaker}).')
        retained.append(f'{label}: reference - the voice of {subject_by_character[cid]} follows the timbre of {label} '
                        'without copying its original words or signal.')
        bindings.append(dict(audio=label.strip('<>'), character_id=cid, speaker=speaker))

    raw_speakers = translation.get('speakers')
    names = {_text(item.get('id')): item for item in raw_speakers if isinstance(item, dict)} \
        if isinstance(raw_speakers, list) else (raw_speakers if isinstance(raw_speakers, dict) else {})
    deliveries = translation.get('deliveries') if isinstance(translation.get('deliveries'), dict) else {}
    introduced = set()
    def reference(pid, shot_prefix=''):
        sp = u['speakers'][pid]
        label = subject_by_character.get(sp['character_id'])
        entry = names.get(pid) if isinstance(names.get(pid), dict) else {}
        intro, short = _text(entry.get('intro')), _text(entry.get('short'))
        first = pid not in introduced
        introduced.add(pid)
        if label:
            keep_intro = (first and label not in shot_prefix and '<Picture' not in intro
                          and 'character sheet' not in intro.lower())
            return label, intro if keep_intro else '', label
        if not intro and not short:
            warnings.append(f'翻译结果缺少说话人 {pid}（{sp["name"]}）的英文身份描述')
            intro = short = 'the speaker'
        phrase = (intro or short) if first else (short or intro)
        return phrase, '', short or intro

    dialogue = []
    def clause(key, shot_prefix=''):
        line = u['lines'][key]
        parts = [reference(pid, shot_prefix) for pid in line['speakers']]
        ids = '(' + ','.join(sid(pid) for pid in line['speakers']) + ')'
        head = ' and '.join(p[0] for p in parts) + ' ' + ids
        extras = []
        if line['presence'] == 'off_screen':
            extras.append('off-screen')
        if len(parts) == 1 and parts[0][1]:
            extras.append(parts[0][1])
        audio = [f'<{b["audio"]}>' for b in bindings if b['speaker'] in [sid(pid) for pid in line['speakers']]]
        if audio:
            extras.append('using the voice timbre referenced from ' + ', '.join(audio))
        if extras:
            head += ', ' + ', '.join(extras) + ','
        text = _spoken(line['text'], request)
        if line['presence'] in ('voiceover', 'narrator'):
            result = f'{head} says in an off-screen voiceover: <d>[{line["language"]}] {text}</d>'
            if line['presence'] == 'voiceover':
                owner = parts[0][2] + "'s lips remain" if len(parts) == 1 else 'their lips remain'
                result += f' while {owner} completely closed.'
        else:
            verb = _english_delivery(deliveries.get(key) or line.get('delivery'))
            result = f'{head} {verb}: <d>[{line["language"]}] {text}</d>'
        dialogue.append(dict(speaker=ids.strip('()'), language=line['language'], text=text,
                             source=line['source'], presence=line['presence'], line_id=key))
        return result

    def fill(value):
        value = _mentions(value, request)
        out, last = [], 0
        for match in PLACEHOLDER.finditer(value):
            before = value[last:match.start()]
            key = match[1]
            if key in u['visible_text']:
                out.append(before)
                replacement = json.dumps(u['visible_text'][key], ensure_ascii=False)
                last = match.end()
            else:
                cleaned = SPEECH_LEAD_IN.sub('.', before)
                if cleaned == before:
                    cleaned = CN_SPEECH_LEAD_IN.sub('.', before)
                if cleaned == before and before.rstrip().endswith((':', '：')):
                    cleaned = re.sub(r'[,;，；]?\s*[:：]\s*$', '.', before)
                if cleaned != before:
                    cleaned = cleaned.rstrip() + ' '
                out.append(cleaned)
                prefix = ''.join(out).rstrip()
                replacement = clause(key, prefix)
                if not prefix or prefix[-1] in '.!?。！？；;' or prefix.endswith('</d>'):
                    replacement = _capitalize(replacement)
                end = match.end()
                if value[end:end + 1] in ('.', '。', '；', ';'):
                    end += 1
                last = end
            out.append(replacement)
        out.append(value[last:])
        return re.sub(r'[ \t]{2,}', ' ', ''.join(out)).strip()

    style = re.sub(r'^(an?|the)\s+', '', _text(translation.get('style')), flags=re.I).rstrip('.')
    rendered = []
    for index, text in enumerate(shots):
        body = fill(text)
        if index == 0:
            if style and request['mode'] != 'Ref2VA':
                first_word = body.split(' ', 1)[0]
                body = (_capitalize(style) + ', ' + _lower_article(body)) if first_word in ('A', 'An', 'The') \
                    else _sentence(_capitalize(style)) + ' ' + body
            rendered.append('[Shot 1] ' + body)
        else:
            if not re.match(r'the (camera|shot)\b', body, re.I):
                body = 'the shot cuts to ' + _lower_article(body)
            body = 't' + body[1:] if body.startswith('The ') else body
            rendered.append(f'[Shot {index+1}] At {_timestamp(u["shots"][index]["start"])}, {body}')
    final_shot = len(rendered)

    soundscape = _text(translation.get('soundscape'))
    if not soundscape:
        warnings.append('翻译结果缺少 overall_soundscape，已填 N/A')
        soundscape = 'N/A'
    music = _text(translation.get('music')) or 'N/A'
    if not u['music'] and music != 'N/A':
        warnings.append('原文没有要求配乐，已忽略翻译结果中的配乐描述')
        music = 'N/A'
    soundscape, music = _mentions(soundscape, request), _mentions(music, request)

    if request['mode'] == 'Ref2VA':
        ref_only_pics = ['<' + p['label'] + '>' for p in request['pictures'] if p.get('frame', 'reference') == 'reference']
        for index, text in enumerate(rendered):
            for pic in ref_only_pics:
                if pic in text and pic not in u['shots'][index]['description']:
                    warnings.append(f'镜头 {index+1} 直接引用了非关键帧参考图 {pic}（正片镜头应使用对应的 <Subject N>）')
        entries = translation.get('subjects') if isinstance(translation.get('subjects'), dict) else {}
        frames = translation.get('pictures') if isinstance(translation.get('pictures'), dict) else {}
        subject_lines, retention_lines = [], []
        for subject in subjects:
            entry = entries.get(subject['label']) or entries.get(subject['label'].strip('<>'))
            entry = entry if isinstance(entry, dict) else {}
            definition = _mentions(_text(entry.get('definition')), request)
            definition = re.sub(r'^' + re.escape(subject['label']) + r'\s+is\s+', '', definition)
            if not definition:
                warnings.append(f'翻译结果缺少 {subject["label"]} 的定义')
                definition = f'the {subject["kind"]} shown in ' + ', '.join(subject['pictures'])
            subject_lines.append(_sentence(f'{subject["label"]} is {definition}'))
            marker = entry.get('marker') if entry.get('marker') in MARKERS else 'fully_preserved'
            note = _text(entry.get('retention')) or 'its referenced features are retained'
            appears = [f'[Shot {i+1}]' for i, text in enumerate(rendered) if subject['label'] in text]
            where = f' (appears in {", ".join(appears)})' if appears else ''
            retention_lines.append(f'{subject["label"]}{where}: {marker} - {_sentence(note)}')
        for picture in frame_pictures(request):
            label = '<' + picture['label'] + '>'
            shown = _text(frames.get(label) or frames.get(picture['label']))
            role = 'the first frame of [Shot 1]' if picture['frame'] == 'first' else f'the last frame of [Shot {final_shot}]'
            subject_lines.append(_sentence(f'{label} is {role}' + (f', showing {shown}' if shown else '')))
            anchor = '[Shot 1] first frame' if picture['frame'] == 'first' else f'[Shot {final_shot}] last frame'
            retention_lines.append(f'{label} ({anchor}): fully_preserved - the frame is matched at that point of the video.')
        tasks = [name for name, active in (('keyframe completion', frame_pictures(request)),
                                           ('reference generation', subjects), ('audio reference', bindings)) if active]
        summary = _mentions(_text(translation.get('summary')), request)
        if not summary:
            warnings.append('翻译结果缺少 summary')
            summary = 'The target video follows the detailed description below.'
        detail = ([_style_sentence(style)] if style else []) + rendered
        sections = [('subject_definitions', '\n'.join(subject_lines + definitions)),
                    ('summary', f'[{" + ".join(tasks)}] {summary}'),
                    ('retention_analysis', '\n'.join(retention_lines + retained)),
                    ('detailed_description', '\n'.join(detail)),
                    ('overall_soundscape', soundscape), ('non_diegetic_music', music)]
        prompt = '\n\n'.join(f'{name}:\n{value}' for name, value in sections)
    else:
        duration = f'{request["duration_seconds"]:.2f}'
        instruction = {
            'I2VA': 'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.',
            'FL2VA': ('How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the '
                      f'0.00-second mark of the target video; Picture 2 (from Shot {final_shot}) aligns with the '
                      f'{duration}-second mark of the target video.'),
            'L2VA': ('How the reference pictures align with the target video — <Picture 1> '
                     f'(from [Shot {final_shot}]) aligns with the {duration}-second mark of the target video.'),
        }.get(request['task'])
        prompt = '\n\n'.join(filter(None, [instruction, 'integrated_multimodal_description: ' + ' '.join(rendered),
                                           'overall_soundscape: ' + soundscape, 'non_diegetic_music: ' + music]))
    if PLACEHOLDER.search(prompt):
        raise ValueError('最终提示词仍有未替换的占位符')

    summary_lines = []
    for index, shot in enumerate(u['shots']):
        timing = '原文时间' if shot['timing'] == 'script' else '估计时间'
        summary_lines.append(f'镜头 {index+1}：{shot["start"]:.2f} 秒起（{timing}）')
    for item in dialogue:
        speaker_names = '、'.join(s['name'] for s in speakers_out if s['id'] in item['speaker'].split(','))
        source = '原文' if item['source'] == 'verbatim' else '转述'
        summary_lines.append(f'({item["speaker"]}) {speaker_names}（{PRESENCE_CN[item["presence"]]}，{source}）：{item["text"]}')
    return dict(version=VERSION, guide_revision=GUIDE_REVISION, task=request['task'], prompt=prompt,
                understanding={k: u[k] for k in ('style', 'shots', 'speakers', 'lines', 'order', 'visible_text', 'sound', 'music')},
                translation=translation, speakers=speakers_out, dialogue=dialogue,
                input_audio_indices=indices, audio_bindings=bindings, unused_audio_bindings=unused,
                warnings=warnings, understanding_summary=summary_lines,
                preservation=dict(policy='follow-source-instruction',
                                  original_sha256=hashlib.sha256(request['original_prompt'].encode()).hexdigest()))


def load_plan(plan):
    """Read our compiled cache; the version only selects a compatible format."""
    if not isinstance(plan, dict) or plan.get('version') != VERSION:
        raise ValueError('整理缓存版本已过期，需要重新整理')
    return plan

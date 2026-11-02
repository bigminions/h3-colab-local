"""Offline regression and diverse-format tests for the two-pass H3 prompt pipeline."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

import app
import h3_prompt as p
import h3_prompt_worker as worker
import remote_h3 as h3


SCRIPT = """硬朗写实青年漫风，成熟二维动漫比例，清晰黑色墨线轮廓。
背后过肩中近景缓慢向前推近。
[0s-4s] @吴铭 背对镜头敲键盘，“叮叮当当”的提示音响个不停，群语音外放出产品经理的女声：“明早老板要看演示，这个小需求很简单，今晚顺手加一下呗！”
[4s-8s] 镜头缓慢向显示器推近，@吴铭 耳麦里传出运维的狂喊声：“老吴！线上订单库连接池爆了，CPU飙到99%了，赶紧救火！”"""


def configuration(text=SCRIPT):
    return dict(prompt=text, duration=10, mode='pdd', people=False, veda=False,
                prompt_processing='faithful', resolution='480p', seed=1, job_id='test',
                references=[dict(slot=3, image_id='scene.png', role='other', frame='first', description=''),
                            dict(slot=5, image_id='man.png', role='character', frame='reference', description='')],
                characters=[dict(id='ming', name='吴铭', image_ids=['man.png'], description='黑色连帽卫衣', voice='男声')],
                audio_references=[dict(character_id='ming', audio_id='voice.wav', duration=7.7)])


def empty_understanding(description='[Shot 1]', style='二维动漫'):
    return dict(style=style, shots=[dict(start=0, timing='script', description=description)],
                speakers=[], lines=[], visible_text=[], sound='安静环境声', music='', notes=[])


def empty_translation(shot='A quiet indoor scene.', style='2D-animated, crisp black ink outlines'):
    return dict(style=style, shots=[shot], speakers={}, deliveries={},
                soundscape='Quiet indoor room tone continues throughout the scene.', music='N/A',
                subjects={'<Subject 1>': dict(definition='Wu Ming in <Picture 2>, wearing a black hoodie',
                                              marker='fully_preserved', retention='his identity and black hoodie are retained')},
                pictures={'<Picture 1>': 'an office desk with three monitors'},
                summary='An office crisis unfolds around <Subject 1>.')


def two_pass_office():
    u = dict(
        style='硬朗写实青年漫风，成熟二维动漫比例，清晰黑色墨线轮廓',
        shots=[dict(start=0, timing='script',
                    description='背后过肩中近景缓慢向前推近，@吴铭 背对镜头敲键盘，叮叮当当的提示音响个不停，{{L1}} 随后镜头继续向显示器推近，{{L2}}')],
        speakers=[dict(id='P1', name='产品经理', character_id=None, voice='群语音外放的女声'),
                  dict(id='P2', name='运维', character_id=None, voice='耳麦里的狂喊声')],
        lines=[dict(id='L1', speakers=['P1'], presence='off_screen', language='Chinese', source='verbatim',
                    text='明早老板要看演示，这个小需求很简单，今晚顺手加一下呗！', delivery='群语音外放'),
               dict(id='L2', speakers=['P2'], presence='off_screen', language='Chinese', source='verbatim',
                    text='老吴！线上订单库连接池爆了，CPU飙到99%了，赶紧救火！', delivery='狂喊')],
        visible_text=[], sound='机械键盘敲击声与叮叮当当的提示音', music='', notes=[])
    t = empty_translation(
        shot=('The shot begins from <Picture 1> as the camera pushes in with small amplitude at slow speed over '
              "<Subject 1>'s shoulder while he types with his mouth closed. {{L1}} At 00:04.000, the camera "
              'continues pushing in toward the monitors. {{L2}}'),
        style='hard-edged realistic young-adult manga, mature 2D proportions, crisp black ink outlines')
    t['speakers'] = dict(
        P1=dict(intro='the female product manager with a sweet, insistent voice over the group-call speaker', short='the product manager'),
        P2=dict(intro='the male operations engineer over the headset', short='the operations engineer'))
    t['deliveries'] = dict(L1='says cheerfully', L2='roars urgently')
    t['soundscape'] = 'Rapid mechanical keyboard clicks and notification chimes continue throughout the office.'
    return u, t


class FidelityTests(unittest.TestCase):
    def compile(self, c, u_raw, t_raw):
        request = p.prompt_request(c)
        u = p.prepare_understanding(u_raw, request)
        return p.render(u, t_raw, request), request, u

    def test_authoring_mentions_map_before_original_text_is_inserted(self):
        c = configuration('屏幕显示“@吴铭 与 @图片5”。')
        u = empty_understanding('@吴铭 站在 @图片5 旁，屏幕显示 {{T1}}')
        u['visible_text'] = [dict(id='T1', text='@吴铭 与 @图片5')]
        t = empty_translation('@吴铭 stands beside @图片5 while the monitor reads {{T1}}.')
        plan, _, _ = self.compile(c, u, t)
        self.assertIn('<Subject 1> stands beside <Picture 2>', plan['prompt'])
        self.assertIn('"@吴铭 与 @图片5"', plan['prompt'])

    def test_two_pass_hides_dialogue_from_translation_and_preserves_exact_words(self):
        c = configuration()
        u_raw, t_raw = two_pass_office()
        request = p.prompt_request(c)
        u = p.prepare_understanding(u_raw, request)
        pass2_input = json.dumps(p.translation_context(u, request), ensure_ascii=False)
        self.assertNotIn('明早老板要看演示', pass2_input)
        self.assertNotIn('线上订单库连接池爆了', pass2_input)
        plan = p.render(u, t_raw, request)
        text = plan['prompt']
        self.assertIn('[keyframe completion + reference generation]', text)
        self.assertIn('The target video is in a hard-edged realistic young-adult manga', text)
        self.assertIn('The female product manager with a sweet, insistent voice over the group-call speaker (S1), off-screen, says cheerfully: <d>[Chinese] 明早老板要看演示，这个小需求很简单，今晚顺手加一下呗！</d>', text)
        self.assertIn('The male operations engineer over the headset (S2), off-screen, roars urgently: <d>[Chinese] 老吴！线上订单库连接池爆了，CPU飙到99%了，赶紧救火！</d>', text)
        self.assertNotIn('[Shot 2]', text)
        self.assertEqual(plan['input_audio_indices'], [])
        self.assertEqual(len(plan['unused_audio_bindings']), 1)
        graph = h3.build_graph(c, plan)
        self.assertEqual(graph['5']['inputs']['prompt'], text)
        self.assertFalse(any(n['class_type'] == 'LoadAudio' for n in graph.values()))

    def test_unquoted_colon_dialogue_and_existing_d_tags(self):
        for raw_script in ['**运维（耳麦狂喊）：** 快来救火！', '运维 <d>[Chinese] 快来救火！</d>']:
            c = configuration(raw_script)
            u = empty_understanding('{{L1}}')
            u['speakers'] = [dict(id='P1', name='运维', character_id=None, voice='男声')]
            u['lines'] = [dict(id='L1', speakers=['P1'], presence='off_screen', language='Chinese',
                               source='verbatim', text='快来救火！', delivery='shouts')]
            t = empty_translation('{{L1}}')
            t['speakers'] = dict(P1=dict(intro='the male operations engineer', short='the engineer'))
            t['deliveries'] = dict(L1='shouts urgently')
            plan, _, _ = self.compile(c, u, t)
            self.assertEqual(plan['prompt'].count('<d>'), 1)
            self.assertIn('<d>[Chinese] 快来救火！</d>', plan['prompt'])
            self.assertEqual(plan['warnings'], [])

    def test_indirect_speech_and_verbatim_mismatch_warn_without_blocking(self):
        c = configuration('他抱怨说今晚又要加班，同事回答“先喝杯咖啡”。')
        u = empty_understanding('{{L1}} {{L2}}')
        u['speakers'] = [dict(id='P1', name='吴铭', character_id='ming', voice='男声'),
                         dict(id='P2', name='同事', character_id=None, voice='女声')]
        u['lines'] = [dict(id='L1', speakers=['P1'], presence='on_screen', language='Chinese',
                           source='derived', text='今晚又要加班了。', delivery='complains'),
                      dict(id='L2', speakers=['P2'], presence='on_screen', language='Chinese',
                           source='verbatim', text='先去喝杯热咖啡吧', delivery='replies')]
        t = empty_translation('{{L1}} {{L2}}')
        t['speakers'] = dict(P1=dict(intro='a tired young man', short='he'),
                             P2=dict(intro='a female colleague', short='she'))
        t['deliveries'] = dict(L1='complains wearily', L2='replies')
        plan, _, _ = self.compile(c, u, t)
        self.assertIn('<d>[Chinese] 今晚又要加班了。</d>', plan['prompt'])
        self.assertIn('<d>[Chinese] 先去喝杯热咖啡吧</d>', plan['prompt'])
        self.assertTrue(any('转述生成的台词' in w for w in plan['warnings']))
        self.assertTrue(any('标为原文，但与原文不一致' in w for w in plan['warnings']))
        self.assertTrue(any('转述）：今晚又要加班了。' in s for s in plan['understanding_summary']))

    def test_voiceover_narrator_and_group_speech_follow_official_guide(self):
        c = dict(prompt='旁白：“那年夏天。”吴铭心想：“不能放弃。”孩子们齐声喊：“等等我们！”',
                 duration=10, mode='pdd', people=False, veda=False, prompt_processing='faithful',
                 resolution='480p', seed=1, job_id='test', references=[], characters=[], audio_references=[])
        u = empty_understanding('{{L1}} {{L2}} {{L3}}')
        u['speakers'] = [dict(id='P1', name='旁白', character_id=None, voice='沉稳男声'),
                         dict(id='P2', name='吴铭', character_id=None, voice='青年男声'),
                         dict(id='P3', name='男孩', character_id=None, voice='童声'),
                         dict(id='P4', name='女孩', character_id=None, voice='童声')]
        u['lines'] = [dict(id='L1', speakers=['P1'], presence='narrator', language='Chinese',
                           source='verbatim', text='那年夏天。', delivery=''),
                      dict(id='L2', speakers=['P2'], presence='voiceover', language='Chinese',
                           source='verbatim', text='不能放弃。', delivery=''),
                      dict(id='L3', speakers=['P3', 'P4'], presence='on_screen', language='Chinese',
                           source='verbatim', text='等等我们！', delivery='shout together')]
        t = empty_translation('{{L1}} {{L2}} {{L3}}', style='live-action, cinematic')
        t['speakers'] = dict(P1=dict(intro='the calm male narrator', short='the narrator'),
                             P2=dict(intro='the young man', short='the young man'),
                             P3=dict(intro='the boy', short='the boy'),
                             P4=dict(intro='the girl', short='the girl'))
        t['deliveries'] = dict(L3='shout together')
        plan, _, _ = self.compile(c, u, t)
        text = plan['prompt']
        self.assertIn('Live-action, cinematic, the calm male narrator (S1) says in an off-screen voiceover: <d>[Chinese] 那年夏天。</d>', text)
        self.assertIn("The young man (S2) says in an off-screen voiceover: <d>[Chinese] 不能放弃。</d> while the young man's lips remain completely closed.", text)
        self.assertIn('The boy and the girl (S3,S4) shout together: <d>[Chinese] 等等我们！</d>', text)

    def test_pure_landscape_and_base_keyframe_alignments(self):
        # Pure T2VA without references or dialogue
        t2va = dict(prompt='清晨海面波光粼粼。', duration=5, references=[])
        u = empty_understanding('清晨海面波光粼粼。', style='写实摄影')
        t = empty_translation('A wide shot shows sunlight glinting across calm ocean waves at dawn.', style='live-action, cinematic')
        plan, _, _ = self.compile(t2va, u, t)
        self.assertTrue(plan['prompt'].startswith('integrated_multimodal_description: [Shot 1] Live-action, cinematic, a wide shot'))
        self.assertNotIn('<d>', plan['prompt'])
        self.assertEqual(plan['dialogue'], [])

        # I2VA first frame
        i2va = dict(prompt='女人抬头看向窗外。', duration=5,
                    references=[dict(slot=1, image_id='first.png', frame='first', role='other', description='')])
        plan, _, _ = self.compile(i2va, u, empty_translation('The woman in <Picture 1> looks up toward the window.', style='live-action'))
        self.assertTrue(plan['prompt'].startswith(
            'For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\nintegrated_multimodal_description:'))

        # FL2VA first + last frame across 2 shots
        fl2va = dict(prompt='从第一帧过渡到最后一帧。', duration=10,
                     references=[dict(slot=1, image_id='first.png', frame='first', role='other', description=''),
                                 dict(slot=2, image_id='last.png', frame='last', role='other', description='')])
        u_two = empty_understanding('第一镜')
        u_two['shots'].append(dict(start=4.5, timing='script', description='第二镜'))
        t_two = empty_translation('Shot one starts from Picture 1.')
        t_two['shots'].append('the camera cuts to Picture 2.')
        plan, _, _ = self.compile(fl2va, u_two, t_two)
        self.assertTrue(plan['prompt'].startswith(
            'How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; '
            'Picture 2 (from Shot 2) aligns with the 10.12-second mark of the target video.\n\n'))
        self.assertIn('[Shot 2] At 00:04.500, the camera cuts to Picture 2.', plan['prompt'])

        # L2VA last frame only
        l2va = dict(prompt='纸飞机落在桌上。', duration=10,
                    references=[dict(slot=4, image_id='last.png', frame='last', role='other', description='')])
        plan, _, _ = self.compile(l2va, u, empty_translation('A paper airplane lands on the table as in <Picture 1>.'))
        self.assertIn('<Picture 1> (from [Shot 1]) aligns with the 10.12-second mark of the target video.', plan['prompt'])

    def test_silent_reference_is_skipped_and_active_audio_is_renumbered_locally(self):
        c = configuration('吴铭说：“好的。”')
        c['characters'].insert(0, dict(id='silent', name='路人', image_ids=['scene.png'], description='', voice=''))
        c['audio_references'].insert(0, dict(character_id='silent', audio_id='silent.wav', duration=2))
        u = empty_understanding('{{L1}}')
        u['speakers'] = [dict(id='P1', name='吴铭', character_id='ming', voice='男声')]
        u['lines'] = [dict(id='L1', speakers=['P1'], presence='on_screen', language='Mandarin',
                           source='verbatim', text='好的。', delivery='says')]
        t = empty_translation('{{L1}}')
        t['speakers'] = dict(P1=dict(intro='a tired man in a black hoodie', short='he'))
        t['deliveries'] = dict(L1='says')
        plan, _, _ = self.compile(c, u, t)
        g = h3.build_graph(c, plan)
        self.assertEqual(plan['input_audio_indices'], [1])
        self.assertEqual(g['70']['inputs']['audio'], 'voice.wav')
        self.assertIn('<Audio 1> is the voice-timbre reference for <Subject 2> (S1).', plan['prompt'])
        self.assertIn('<Subject 2> (S1), a tired man in a black hoodie, using the voice timbre referenced from <Audio 1>, says: <d>[Mandarin] 好的。</d>', plan['prompt'])
        self.assertNotIn('<Audio 2>', plan['prompt'])

    def test_unrequested_music_is_ignored_and_overlong_dialogue_warns(self):
        c = configuration('吴铭说：“这是一段非常非常长、在五秒视频里根本不可能完整说完的测试台词内容，用来检查时长估算警告。”')
        c['duration'] = 5
        u = empty_understanding('{{L1}}')
        u['speakers'] = [dict(id='P1', name='吴铭', character_id='ming', voice='男声')]
        u['lines'] = [dict(id='L1', speakers=['P1'], presence='on_screen', language='Chinese',
                           source='verbatim', text='这是一段非常非常长、在五秒视频里根本不可能完整说完的测试台词内容，用来检查时长估算警告。', delivery='')]
        t = empty_translation('{{L1}}')
        t['music'] = 'A dramatic orchestral score swells.'
        plan, _, _ = self.compile(c, u, t)
        self.assertTrue(plan['prompt'].endswith('non_diegetic_music:\nN/A'))
        self.assertTrue(any('原文没有要求配乐' in w for w in plan['warnings']))
        self.assertTrue(any('超过视频时长' in w for w in plan['warnings']))

    def test_worker_runs_two_passes_and_records_diagnostics(self):
        request = p.prompt_request(configuration())
        u_raw, t_raw = two_pass_office()
        u_json = json.dumps(u_raw, ensure_ascii=False)
        t_json = json.dumps(t_raw, ensure_ascii=False)
        generator = MagicMock()
        generator.generate_text.side_effect = [
            (u_json, u_json, '', '', 'pass1 report'),
            (t_json, t_json, '', '', 'pass2 report'),
        ]
        with tempfile.TemporaryDirectory(dir=app.PRIVATE) as tmp:
            path = Path(tmp) / 'diagnostics.json'
            result = worker.rewrite(request, None, generator, 'tail', image='fake_tensor', diagnostics_path=path)
            records = json.loads(path.read_text('utf-8'))
        self.assertEqual(generator.generate_text.call_count, 2)
        first_call, second_call = generator.generate_text.call_args_list
        self.assertIsNone(first_call.kwargs['image'])
        self.assertEqual(second_call.kwargs['image'], 'fake_tensor')
        self.assertEqual([r['phase'] for r in records], ['understanding', 'translation'])
        self.assertEqual(result['generation_report'], ['pass1 report', 'pass2 report'])
        self.assertEqual(len(result['attempt_seconds']), 2)

    def test_parse_and_unrenderable_outputs_fail_after_bounded_retries(self):
        request = p.prompt_request(configuration())
        u_raw, _ = two_pass_office()
        u_json = json.dumps(u_raw, ensure_ascii=False)
        # Pass 1 failure retries once before failing (2 calls)
        for bad_first in ['not JSON', json.dumps(dict(shots=[]))]:
            generator = MagicMock(return_value=(bad_first, bad_first, '', '', 'mock'))
            with self.assertRaisesRegex(ValueError, '第 1 步'):
                worker.rewrite(request, None, generator, 'tail')
            self.assertEqual(generator.generate_text.call_count, 2)
        # Pass 2 failure retries once before failing (1 pass1 call + 2 pass2 calls = 3 calls)
        generator = MagicMock()
        generator.generate_text.side_effect = [
            (u_json, u_json, '', '', 'pass1'),
            (json.dumps(dict(shots=['one', 'two'])), '{}', '', '', 'pass2 attempt 1'),
            (json.dumps(dict(shots=['one', 'two'])), '{}', '', '', 'pass2 attempt 2'),
        ]
        with self.assertRaisesRegex(ValueError, '第 2 步'):
            worker.rewrite(request, None, generator, 'tail')
        self.assertEqual(generator.generate_text.call_count, 3)

    def test_worker_recovers_on_second_attempt_and_auto_repairs_json(self):
        request = p.prompt_request(configuration())
        u_raw, t_raw = two_pass_office()
        u_json = json.dumps(u_raw, ensure_ascii=False)
        t_json = json.dumps(t_raw, ensure_ascii=False)
        # Test recovery when attempt 1 fails but attempt 2 succeeds
        generator = MagicMock()
        generator.generate_text.side_effect = [
            (u_json, u_json, '', '', 'pass1'),
            ('completely broken json', 'completely broken json', '', '', 'pass2 attempt 1'),
            (t_json, t_json, '', '', 'pass2 attempt 2'),
        ]
        result = worker.rewrite(request, None, generator, 'tail')
        self.assertEqual(generator.generate_text.call_count, 3)
        self.assertIn('prompt', result)

        # Test automatic JSON repair on broken Chinese quotes/trailing commas without retry
        broken_json = '{"style": "art", "shots": ["shot1出大事了！”, "shot2"], "speakers": {}, "deliveries": {}, "soundscape": "", "music": "N/A", "subjects": {}, "pictures": {}, "summary": "sum", }'
        parsed = p.decode_plan(broken_json)
        self.assertEqual(len(parsed['shots']), 2)
        self.assertEqual(parsed['shots'][0], 'shot1出大事了！”')

    def test_old_cache_versions_are_rejected(self):
        plan = dict(version=p.VERSION, prompt='compiled', input_audio_indices=[])
        self.assertIs(p.load_plan(plan), plan)
        for old_version in ('qwen-h3-annotations-7', 'qwen-h3-english-8', 'qwen-h3-two-pass-3'):
            with self.assertRaisesRegex(ValueError, '缓存版本已过期'):
                p.load_plan(dict(plan, version=old_version))

    def test_inplace_placeholder_replacement_and_untranslated_pass2_rejection(self):
        c = configuration(
            '[0s-4s] @吴铭 破音大喊：“牛马哥！出大事了！”屏幕显示：“【距离上线还有 15 天】”\n'
            '[4s-8s] @吴铭 惊讶地说：“啊！！！”'
        )
        request = p.prompt_request(c)
        # Pass 1 forgot to put {{L1}}, {{L2}}, {{T1}} in shots, leaving raw Chinese quotes in shots 1 and 2
        u_raw = dict(
            style='硬朗写实青年漫风',
            shots=[
                dict(start=0.0, timing='script', description='【分镜1：中景】<Subject 1>破音大喊：“牛马哥！出大事了！”屏幕显示：“【距离上线还有 15 天】”'),
                dict(start=4.0, timing='script', description='【切分镜2：特写】<Subject 1>惊讶地说：“啊！！！”'),
            ],
            speakers=[dict(id='P1', name='<Subject 1>', character_id='ming', voice='')],
            lines=[
                dict(id='L1', speakers=['P1'], presence='on_screen', language='Chinese', source='verbatim', text='牛马哥！出大事了！', delivery='破音大喊'),
                dict(id='L2', speakers=['P1'], presence='on_screen', language='Chinese', source='verbatim', text='啊！！！', delivery='惊讶地说'),
            ],
            visible_text=[dict(id='T1', text='【距离上线还有 15 天】')],
            sound='', music='', notes=[],
        )
        u = p.prepare_understanding(u_raw, request)
        self.assertIn('{{L1}}', u['shots'][0]['description'])
        self.assertIn('{{T1}}', u['shots'][0]['description'])
        self.assertNotIn('{{L1}}', u['shots'][1]['description'])
        self.assertIn('{{L2}}', u['shots'][1]['description'])
        self.assertNotIn('牛马哥！出大事了！', u['shots'][0]['description'])
        self.assertNotIn('啊！！！', u['shots'][1]['description'])

        # On attempt 1 (strict_english=True), untranslated Chinese shots/deliveries trigger retry
        bad_t = empty_translation()
        bad_t['shots'] = [u['shots'][0]['description'], u['shots'][1]['description']]
        bad_t['deliveries'] = dict(L1='破音大喊', L2='惊讶地说')
        with self.assertRaisesRegex(ValueError, '未翻译为英文'):
            p.render(u, bad_t, request, strict_english=True)

        # On final attempt (strict_english=False), it never fails the job; it normalizes deliveries and lead-ins cleanly
        fallback_plan = p.render(u, bad_t, request, strict_english=False)
        self.assertIn('shouts: <d>[Chinese] 牛马哥！出大事了！</d>', fallback_plan['prompt'])
        self.assertIn('says: <d>[Chinese] 啊！！！</d>', fallback_plan['prompt'])
        self.assertNotIn('破音大喊: <d>', fallback_plan['prompt'])
        self.assertNotIn('惊讶地说: <d>', fallback_plan['prompt'])

        good_t = empty_translation()
        good_t['shots'] = [
            '【分镜1：中景】Medium shot of <Subject 1> shouting: {{L1}} while the monitor displays {{T1}}.',
            '【切分镜2：特写】The camera cuts to a close-up of <Subject 1>, saying in surprise: {{L2}}.',
        ]
        good_t['deliveries'] = dict(L1='shouts hoarsely', L2='exclaims in surprise')
        plan = p.render(u, good_t, request)
        self.assertNotIn('【分镜1', plan['prompt'])
        self.assertNotIn('【切分镜2', plan['prompt'])
        self.assertIn('[Shot 1] Medium shot of <Subject 1> shouting. <Subject 1> (S1), using the voice timbre referenced from <Audio 1>, shouts hoarsely: <d>[Chinese] 牛马哥！出大事了！</d>', plan['prompt'])
        self.assertIn('[Shot 2] At 00:04.000, the camera cuts to a close-up of <Subject 1>. <Subject 1> (S1), using the voice timbre referenced from <Audio 1>, exclaims in surprise: <d>[Chinese] 啊！！！</d>', plan['prompt'])

    def test_model_contexts_never_expose_audio_files_or_sample_words(self):
        request = p.prompt_request(configuration())
        request['characters'][0]['audios'] = [dict(audio_id='secret.wav', name='sample words')]
        u_ctx = json.dumps(p.understanding_context(request), ensure_ascii=False)
        u_raw, _ = two_pass_office()
        t_ctx = json.dumps(p.translation_context(p.prepare_understanding(u_raw, request), request), ensure_ascii=False)
        for raw in (u_ctx, t_ctx):
            self.assertNotIn('secret.wav', raw)
            self.assertNotIn('sample words', raw)
            self.assertNotIn('voice.wav', raw)

    def test_reference_picture_type_and_duplicate_speech_lead_in_cleanup(self):
        c = dict(
            prompt='9:16竖屏构图，硬朗写实青年漫风。[0s-3.5s] 手机外放出妈妈温柔慈祥的声音：“铭铭啊……” [3.5s-6.5s] @吴铭 按住语音键敷衍回复道：“忙死了！”',
            duration=15, mode='base', people=False, veda=False, prompt_processing='faithful',
            resolution='768p', seed=1, job_id='test',
            references=[dict(slot=1, image_id='sheet.png', role='character', frame='reference', description='')],
            characters=[dict(id='ming', name='吴铭', image_ids=['sheet.png'], description='黑色连帽卫衣', voice='')],
            audio_references=[dict(character_id='ming', audio_id='ming.wav', duration=4.0)],
        )
        request = p.prompt_request(c)
        u_raw = dict(
            style='9:16竖屏构图，硬朗写实青年漫风',
            shots=[
                dict(start=0.0, timing='script', description='桌上的手机震动亮起，外放出妈妈温柔慈祥的声音：{{L1}}'),
                dict(start=3.5, timing='script', description='镜头切至<Subject 1>的侧面近景，按住语音键语速极快地敷衍回复道：{{L2}}随后松开手指。'),
            ],
            speakers=[
                dict(id='P1', name='妈妈', character_id=None, voice='温柔慈祥'),
                dict(id='P2', name='吴铭', character_id='ming', voice=''),
            ],
            lines=[
                dict(id='L1', speakers=['P1'], presence='off_screen', language='Chinese', source='verbatim', text='铭铭啊……', delivery=''),
                dict(id='L2', speakers=['P2'], presence='on_screen', language='Chinese', source='verbatim', text='忙死了！', delivery='语速极快地敷衍回复'),
            ],
            visible_text=[], sound='手机震动声', music='', notes=[],
        )
        u = p.prepare_understanding(u_raw, request)
        t_ctx = p.translation_context(u, request)
        self.assertEqual(t_ctx['pictures'][0]['frame'], 'reference')
        self.assertEqual(t_ctx['pictures'][0]['subject'], '<Subject 1>')
        self.assertEqual(t_ctx['frame_pictures'], [])

        t_raw = dict(
            style='9:16 vertical composition, hard-edged realistic young manga style, clear black ink line outlines',
            shots=[
                'A close-up overhead shot of a smartphone on a desk, vibrating and lighting up with a WeChat voice message, playing the gentle, warm voice of a mother: {{L1}}.',
                'The camera cuts to a side close-up of <Subject 1> pressing the voice button on the phone, responding quickly and dismissively: {{L2}}, then releasing the button.',
            ],
            speakers=dict(
                P1=dict(intro='the mother with a gentle, warm voice', short='the mother'),
                P2=dict(intro='the young man shown in a character sheet in <Picture 1>', short='the young man'),
            ),
            deliveries=dict(L1='says', L2='responds quickly and dismissively'),
            soundscape='A smartphone vibrates on the desk.',
            music='N/A',
            subjects={'<Subject 1>': dict(definition='a young man in a black hoodie in <Picture 1>',
                                          marker='fully_preserved', retention='his appearance is retained')},
            pictures={},
            summary='A late-night work call with <Subject 1>.',
        )
        plan = p.render(u, t_raw, request)
        text = plan['prompt']
        self.assertIn('<Subject 1> (appears in [Shot 2]): fully_preserved', text)
        self.assertIn(
            'The target video features 9:16 vertical composition, hard-edged realistic young manga style, clear black ink line outlines.',
            text,
        )
        self.assertIn(
            'lighting up with a WeChat voice message. The mother with a gentle, warm voice (S1), off-screen, says: <d>[Chinese] 铭铭啊……</d>',
            text,
        )
        self.assertNotIn('playing the gentle, warm voice of a mother:', text)
        self.assertIn(
            'pressing the voice button on the phone. <Subject 1> (S2), using the voice timbre referenced from <Audio 1>, responds quickly and dismissively: <d>[Chinese] 忙死了！</d>, then releasing the button.',
            text,
        )
        self.assertNotIn('responding quickly and dismissively: <Subject 1>', text)
        self.assertNotIn('character sheet', text)

    def test_default_formatting_and_explicit_direct_mode(self):
        self.assertEqual(app.JobConfig(prompt='x', prompt_enhance=False).prompt_processing, 'faithful')
        self.assertEqual(app.JobConfig(prompt='x', prompt_processing='direct').prompt_processing, 'direct')

    def test_quoted_sound_effects_and_parenthesized_directions_do_not_become_dialogue(self):
        # Chinese, English, and Japanese scripts with quoted sounds and bracketed stage directions
        cases = [
            (
                '伴随“滴滴滴——”系统告警蜂鸣声，前景@吴铭背影僵住。（手指抽搐、痉挛、滑落）',
                '伴随蜂鸣声 {{L1}}，前景@吴铭背影僵住。{{L2}}',
                [dict(id='P1', name='系统告警声', kind='sound', character_id=None, voice='尖锐刺耳的蜂鸣声')],
                [
                    dict(id='L1', kind='sound', speakers=['P1'], presence='off_screen', language='Chinese', source='verbatim', text='滴滴滴——', delivery=''),
                    dict(id='L2', kind='speech', speakers=['P1'], presence='off_screen', language='Chinese', source='verbatim', text='（手指抽搐、痉挛、滑落）', delivery=''),
                ],
                '滴滴滴——',
            ),
            (
                'A sharp "BEEP BEEP—" alarm rings out as @吴铭 freezes. (fingers twitch and slip off the key)',
                'A sharp alarm rings out {{L1}} as @吴铭 freezes. {{L2}}',
                [],
                [
                    dict(id='L1', kind='sound', speakers=[], presence='off_screen', language='English', source='verbatim', text='"BEEP BEEP—"', delivery=''),
                    dict(id='L2', kind='stage_direction', speakers=[], presence='off_screen', language='English', source='verbatim', text='(fingers twitch and slip off the key)', delivery=''),
                ],
                'BEEP BEEP—',
            ),
            (
                '「ピピピッ——」と警報音が鳴り響き、@吴铭は凍りつく。（指が痙攣してキーから滑り落ちる）',
                '警報音が鳴り響き {{L1}}、@吴铭は凍りつく。{{L2}}',
                [dict(id='P1', name='警報音', kind='sound', character_id=None, voice='電子音')],
                [
                    dict(id='L1', kind='speech', speakers=['P1'], presence='off_screen', language='Japanese', source='verbatim', text='「ピピピッ——」', delivery=''),
                    dict(id='L2', kind='speech', speakers=['P1'], presence='off_screen', language='Japanese', source='verbatim', text='（指が痙攣してキーから滑り落ちる）', delivery=''),
                ],
                'ピピピッ——',
            ),
        ]
        for raw_script, shot_desc, speakers, lines, expected_sound in cases:
            c = configuration(raw_script)
            u = empty_understanding(shot_desc)
            u['speakers'] = speakers
            u['lines'] = lines
            u_prepared = p.prepare_understanding(u, p.prompt_request(c))
            self.assertEqual(len(u_prepared['lines']), 0)
            self.assertEqual(len(u_prepared['speakers']), 0)
            self.assertNotIn('{{L1}}', u_prepared['shots'][0]['description'])
            self.assertNotIn('{{L2}}', u_prepared['shots'][0]['description'])
            self.assertIn(expected_sound, u_prepared['sound'])


if __name__ == '__main__':
    unittest.main()

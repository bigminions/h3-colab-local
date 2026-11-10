"""A Gemma chat is one queue task from "open app" to "close app"; isolated transports, no cloud calls."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from fastapi import HTTPException
from unittest.mock import patch

import app
from apps.gemma_chat.application import APP_ID, GemmaJobConfig
from apps.gemma_chat.workspace import GemmaWorkspace
from tests.test_app import FakeTransport


TUNNEL = 'https://chat.trycloudflare.com'


class ChatTransport(FakeTransport):
    def execute(self, job):
        job['execution_started'] = True
        self.calls.append('execute:' + app.application_id(job))
        if app.application_id(job) == APP_ID:
            job['remote_result'] = {'ready': True, 'endpoint': TUNNEL, 'secret': 's'}
        else:
            super().execute(job)
            self.calls.pop()


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class GemmaSessionQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE, ignore_cleanup_errors=True)
        self.dir = Path(self.temp.name)
        self.clock = [1000.0]
        self.control = app.Controller(self.dir, self.dir, now=lambda: self.clock[0])
        self.fake = ChatTransport(self.control)
        self.control.transport = self.fake
        self.control.chat_health = lambda endpoint: True
        self.control.CHAT_HEALTH_INTERVAL = 0
        self._orig_wait = self.control.chat_wakeup.wait
        self.control.chat_wakeup.wait = lambda timeout=None: self._orig_wait(min(timeout or 0.02, 0.02))
        self.workspace = GemmaWorkspace(self.control, authorized=lambda: True)

    def tearDown(self):
        self.control.monitor_stop.set()
        self.control.chat_wakeup.set()
        self.control.cancel_wakeup.set()
        worker = self.control.worker_thread
        if worker:
            worker.join(5)
        with self.control.lock:
            pass
        self.temp.cleanup()

    def open_chat(self):
        job_id = self.control.start(GemmaJobConfig(operation='open', gpu='L4', secret='s'))
        job = self.control.jobs[job_id]
        self.assertTrue(wait_until(lambda: job.get('chat_session') == 'ready'), job.get('message'))
        return job

    def queue_h3(self):
        job = dict(id='b' * 32, config=app.H3JobConfig(prompt='test').model_dump(), status='queued',
                   stage='connect', message='排队中', events=[], created=time.time(),
                   queue_order=self.control.queue_order())
        with self.control.lock:
            self.control.jobs[job['id']] = job
            self.control.ensure_worker()
        return job

    def test_chat_holds_gpu_until_close_then_hands_over_to_queue(self):
        chat = self.open_chat()
        self.assertEqual(self.control.active, chat['id'])
        self.assertEqual(self.workspace.snapshot()['state'], 'ready')
        h3 = self.queue_h3()
        time.sleep(0.5)
        # The H3 job waits; the chat runtime is neither switched nor released.
        self.assertEqual(h3['status'], 'queued')
        self.assertEqual(self.control.active, chat['id'])
        self.assertNotIn('release', self.fake.calls)
        snapshot = self.workspace.snapshot()
        self.assertEqual(snapshot['waiting_others'], 1)
        self.assertEqual(self.control.occupying_app_info()[0], APP_ID)
        self.assertEqual(self.control.refresh_consumer()['state'], 'busy')

        self.workspace.finish_close()
        self.assertIsNone(self.workspace.close_error)
        self.assertEqual(chat['status'], 'done')
        self.assertEqual(chat['chat_end'], 'close')
        self.assertTrue(wait_until(lambda: h3['status'] == 'done'), h3.get('message'))
        # Exactly one release: the switch to H3, not an extra release by the chat.
        first_h3 = self.fake.calls.index('execute:h3')
        self.assertEqual(self.fake.calls[:first_h3].count('release'), 1)
        self.assertEqual(self.workspace.snapshot()['state'], 'closed')

    def test_close_without_waiting_tasks_releases_gpu(self):
        chat = self.open_chat()
        self.workspace.finish_close()
        self.assertEqual(chat['status'], 'done')
        self.assertIn('显卡已释放', chat['message'])
        self.assertIsNone(self.control.session)
        self.assertTrue(wait_until(lambda: self.control.active is None))

    def test_idle_timeout_ends_session_and_releases(self):
        self.control.configure_release(app.ReleasePolicy(mode='idle', idle_minutes=1))
        chat = self.open_chat()
        self.clock[0] += 30
        self.control.touch_chat_session()
        self.clock[0] += 45
        time.sleep(0.2)
        self.assertEqual(chat['chat_session'], 'ready')  # activity refreshed the timer
        self.clock[0] += 20
        self.assertTrue(wait_until(lambda: chat['status'] == 'done'))
        self.assertEqual(chat['chat_end'], 'idle')
        self.assertIsNone(self.control.session)
        self.assertEqual(self.workspace.snapshot()['notice'], chat['message'])

    def test_manual_mode_times_out_only_when_others_wait(self):
        self.control.configure_release(app.ReleasePolicy(mode='manual', idle_minutes=1))
        chat = self.open_chat()
        self.clock[0] += 3600
        time.sleep(0.3)
        self.assertEqual(chat['chat_session'], 'ready')
        h3 = self.queue_h3()
        self.assertTrue(wait_until(lambda: chat['status'] == 'done'))
        self.assertEqual(chat['chat_end'], 'idle')
        self.assertTrue(wait_until(lambda: h3['status'] == 'done'), h3.get('message'))

    def test_unhealthy_tunnel_ends_session(self):
        self.control.configure_release(app.ReleasePolicy(mode='manual'))
        self.control.chat_health = lambda endpoint: False
        chat = self.open_chat()
        self.clock[0] += 5
        self.assertTrue(wait_until(lambda: chat['status'] == 'done'))
        self.assertEqual(chat['chat_end'], 'lost')

    def test_cancel_on_held_chat_closes_session_cleanly(self):
        chat = self.open_chat()
        result = self.control.cancel(chat['id'])
        self.assertTrue(result['pending'])
        self.assertTrue(wait_until(lambda: chat['status'] == 'done'))
        self.assertNotIn('cancel', self.fake.calls)
        self.assertNotIn('cancel_requested', chat)

    def test_manual_release_is_refused_while_chatting(self):
        chat = self.open_chat()
        with self.assertRaises(HTTPException) as caught:
            self.control.check_and_release()
        self.assertIn('Gemma', caught.exception.detail)
        self.control.end_chat_session(chat['id'])
        self.assertTrue(wait_until(lambda: self.control.jobs[chat['id']]['status'] == 'done'))

    def test_restart_ends_held_session(self):
        job = dict(id='c' * 32, config=GemmaJobConfig(operation='open').model_dump(), status='running',
                   chat_session='ready', safe_release=True, events=[], created=1.0)
        self.control.jobs[job['id']] = job
        self.control.save_jobs()
        restarted = app.Controller(self.dir, self.dir)
        saved = restarted.jobs[job['id']]
        self.assertEqual((saved['status'], saved['chat_session'], saved['chat_end']), ('done', 'ended', 'restart'))
        workspace = GemmaWorkspace(restarted, authorized=lambda: True)
        snapshot = workspace.snapshot()
        self.assertEqual(snapshot['state'], 'closed')
        self.assertIn('重启', snapshot['notice'])

    def test_debug_surfaces_latest_reply_metrics_while_session_held(self):
        chat = self.open_chat()
        with self.control.lock:
            self.control.jobs['reply1'] = dict(
                id='reply1',
                config=dict(app_id=APP_ID, operation='reply', variant='native', gpu='L4', chat_id='a' * 32, prompt='hi'),
                status='done',
                remote_result={'text': 'hello', 'metrics': {'tokens_per_second': 32.5, 'first_text_ms': 180}},
                created=chat['created'] + 5,
                events=[],
            )
        dbg = self.workspace.debug()
        self.assertEqual(dbg['job_id'], chat['id'])
        self.assertEqual(dbg['chat_session'], 'ready')
        self.assertEqual(dbg['metrics'], {'tokens_per_second': 32.5, 'first_text_ms': 180})
        self.control.end_chat_session(chat['id'])
        self.assertTrue(wait_until(lambda: self.control.jobs[chat['id']]['status'] == 'done'))

    def test_global_status_includes_open_session_but_excludes_individual_reply_turns(self):
        chat = self.open_chat()
        with self.control.lock:
            for idx in range(25):
                self.control.jobs[f'reply-{idx}'] = dict(
                    id=f'reply-{idx}',
                    config=dict(app_id=APP_ID, operation='reply', variant='native', gpu='L4', chat_id='a' * 32, prompt='msg'),
                    status='done',
                    remote_result={'text': 'ok'},
                    created=chat['created'] + idx + 1,
                    events=[],
                )
        with patch('app.controller', self.control):
            state = app.status()
        job_ids = {j['id'] for j in state['jobs']}
        self.assertIn(chat['id'], job_ids)
        self.assertTrue(all(not jid.startswith('reply-') for jid in job_ids))
        self.workspace.finish_close()
        self.assertTrue(wait_until(lambda: self.control.jobs[chat['id']]['status'] in ('done', 'warning')))


if __name__ == '__main__':
    unittest.main()

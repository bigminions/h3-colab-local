"""No network or cloud allocation."""
import queue
import unittest
from unittest.mock import MagicMock, patch
from runtime_watchdog import execute_monitored


class WatchdogTests(unittest.TestCase):
    def test_completed_code_returns_outputs(self):
        runtime = MagicMock()
        runtime.execute_code.return_value = ['done']
        self.assertEqual(execute_monitored(runtime, MagicMock(), 'owned','code'), ['done'])

    def test_execution_error_is_preserved(self):
        runtime = MagicMock()
        runtime.execute_code.side_effect = RuntimeError('failed')
        with self.assertRaisesRegex(RuntimeError,'failed'):
            execute_monitored(runtime,MagicMock(),'owned','code')

    def test_three_missing_runtime_checks_stop_stale_wait(self):
        finished, client = MagicMock(), MagicMock()
        finished.get.side_effect = queue.Empty
        client.list_assignments.return_value = []
        tick = iter(range(0,10000,61))
        with patch('runtime_watchdog.queue.Queue',return_value=finished), patch('runtime_watchdog.threading.Thread'), patch('runtime_watchdog.time.time',side_effect=lambda:next(tick)):
            with self.assertRaisesRegex(RuntimeError,'已被回收'):
                execute_monitored(MagicMock(),client,'owned','code')
        self.assertEqual(client.list_assignments.call_count,3)

    def test_wall_clock_deadline_handles_sleep_resume(self):
        finished = MagicMock()
        finished.get.side_effect = queue.Empty
        with patch('runtime_watchdog.queue.Queue',return_value=finished), patch('runtime_watchdog.threading.Thread'), patch('runtime_watchdog.time.time',side_effect=[0,0,8000]):
            with self.assertRaisesRegex(RuntimeError,'超过两小时'):
                execute_monitored(MagicMock(),MagicMock(),'owned','code')

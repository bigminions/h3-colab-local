import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
import app


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.PRIVATE)
        self.path = Path(self.temp.name) / 'account.json'
        self.time = 1000
        self.provider = MagicMock(return_value={'runtimes': [], 'consumption': {'paid_compute_units_balance': 380.94, 'consumption_rate_hourly': 0, 'assignments_count': 0}})
        self.cache = app.AccountCache(self.provider, self.path, now=lambda: self.time)

    def tearDown(self):
        self.temp.cleanup()

    def test_repeated_poll_uses_cached_balance(self):
        self.assertEqual(self.cache.get()['consumption']['paid_compute_units_balance'], 380.94)
        self.time += 10
        self.cache.get()
        self.provider.assert_called_once()
        self.time += 20
        self.cache.get()
        self.assertEqual(self.provider.call_count, 2)

    def test_failed_refresh_keeps_last_balance_and_timestamp(self):
        first = self.cache.get()
        self.provider.side_effect = RuntimeError('network unavailable')
        self.time += 30
        result = self.cache.get()
        self.assertEqual(result['consumption'], first['consumption'])
        self.assertEqual(result['updated_at'], first['updated_at'])
        self.assertTrue(result['stale'])
        self.assertIn('network unavailable', result['error'])

    def test_unknown_balance_is_not_reported_as_zero(self):
        self.provider.side_effect = RuntimeError('network unavailable')
        with self.assertRaises(RuntimeError):
            self.cache.get()

    def test_real_zero_balance_remains_zero(self):
        self.provider.return_value['consumption']['paid_compute_units_balance'] = 0
        result = self.cache.get()
        self.assertEqual(result['consumption']['paid_compute_units_balance'], 0)
        self.assertFalse(result['stale'])

    def test_restart_retains_last_known_balance(self):
        result = self.cache.get()
        cache = app.AccountCache(self.provider, self.path, now=lambda: self.time + 120)
        self.provider.side_effect = RuntimeError('offline')
        restored = cache.get()
        self.assertEqual(restored['consumption'], result['consumption'])
        self.assertTrue(restored['stale'])

    def test_force_refresh_rate_limited(self):
        self.cache.get()
        self.time += 2
        self.cache.get(refresh=True)
        self.provider.assert_called_once()
        self.time += 4
        self.cache.get(refresh=True)
        self.assertEqual(self.provider.call_count, 2)

    def set_live_a100(self, rate=8):
        self.provider.return_value = {'runtimes':[{'gpu':'A100','endpoint':'test'}],
            'consumption':{'paid_compute_units_balance':380.94,'consumption_rate_hourly':rate,'assignments_count':1}}

    def test_idle_reference_estimate_is_finite_and_labeled(self):
        estimate = self.cache.get()['a100_estimate']
        self.assertEqual(estimate['rate_source'], 'reference')
        self.assertEqual(estimate['remaining_minutes'], 56 * 60 + 16)
        self.assertIsNone(estimate['rate_updated_at'])

    def test_single_a100_learns_actual_rate_and_updates(self):
        self.set_live_a100()
        estimate = self.cache.get()['a100_estimate']
        self.assertEqual(estimate['rate_source'], 'live')
        self.assertEqual(estimate['rate_hourly'], 8)
        self.assertAlmostEqual(estimate['hours'], 380.94 / 8)
        self.time += 30
        self.set_live_a100(10)
        self.assertEqual(self.cache.get()['a100_estimate']['rate_hourly'], 10)

    def test_releasing_gpu_keeps_observed_a100_rate(self):
        self.set_live_a100()
        self.cache.get()
        self.time += 30
        self.provider.return_value = {'runtimes':[], 'consumption':{'paid_compute_units_balance':300,'consumption_rate_hourly':0,'assignments_count':0}}
        estimate = self.cache.get()['a100_estimate']
        self.assertEqual(estimate['rate_source'], 'last_observed')
        self.assertEqual(estimate['rate_hourly'], 8)
        self.assertEqual(estimate['remaining_minutes'], 2250)

    def test_multi_gpu_and_other_gpu_do_not_overwrite_a100_rate(self):
        self.set_live_a100()
        self.cache.get()
        for runtimes in ([{'gpu':'A100'}, {'gpu':'A100'}], [{'gpu':'A100'}, {'gpu':'T4'}], [{'gpu':'T4'}]):
            with self.subTest(runtimes=runtimes):
                self.time += 30
                self.provider.return_value = {'runtimes':runtimes, 'consumption':{'paid_compute_units_balance':200,'consumption_rate_hourly':16,'assignments_count':len(runtimes)}}
                estimate = self.cache.get()['a100_estimate']
                self.assertEqual(estimate['rate_hourly'], 8)
                self.assertEqual(estimate['rate_source'], 'last_observed')

    def test_inconsistent_session_count_does_not_learn_rate(self):
        self.set_live_a100()
        self.provider.return_value['consumption']['assignments_count'] = 2
        self.assertEqual(self.cache.get()['a100_estimate']['rate_source'], 'reference')

    def test_restart_retains_a100_rate_when_idle(self):
        self.set_live_a100()
        self.cache.get()
        self.time += 120
        self.provider.return_value = {'runtimes':[], 'consumption':{'paid_compute_units_balance':200,'consumption_rate_hourly':0,'assignments_count':0}}
        restored = app.AccountCache(self.provider, self.path, now=lambda:self.time).get()['a100_estimate']
        self.assertEqual(restored['rate_source'], 'last_observed')
        self.assertEqual(restored['rate_hourly'], 8)
        self.assertEqual(restored['rate_updated_at'], 1000)

    def test_invalid_or_zero_balance_never_becomes_infinite_time(self):
        for balance in (None, -1, float('nan'), float('inf'), 'unknown', True, 0):
            with self.subTest(balance=balance):
                estimate = app.a100_estimate({'consumption':{'paid_compute_units_balance':balance}})
                self.assertEqual(estimate['remaining_minutes'], 0 if balance == 0 else None)

    def test_failed_refresh_uses_last_observation_label(self):
        self.set_live_a100()
        previous = self.cache.get()['a100_estimate']
        self.time += 30
        self.provider.side_effect = RuntimeError('offline')
        result = self.cache.get()
        self.assertTrue(result['stale'])
        self.assertEqual(result['a100_estimate']['rate_source'], 'last_observed')
        self.assertEqual(result['a100_estimate']['remaining_minutes'], previous['remaining_minutes'])

    def test_g4_actual_rate_is_separate_and_retained_after_release(self):
        self.set_live_a100(6.77)
        self.cache.get()
        self.time += 30
        self.provider.return_value = {'runtimes':[{'gpu':'G4'}], 'consumption':{'paid_compute_units_balance':200, 'consumption_rate_hourly':8, 'assignments_count':1}}
        estimates = self.cache.get()['gpu_estimates']
        self.assertEqual(estimates['G4']['rate_source'], 'live')
        self.assertEqual(estimates['G4']['remaining_minutes'], 1500)
        self.assertEqual(estimates['A100']['rate_hourly'], 6.77)
        self.time += 30
        self.provider.return_value = {'runtimes':[], 'consumption':{'paid_compute_units_balance':200, 'consumption_rate_hourly':0, 'assignments_count':0}}
        restored = app.AccountCache(self.provider, self.path, now=lambda:self.time).get()['gpu_estimates']['G4']
        self.assertEqual(restored['rate_source'], 'last_observed')
        self.assertEqual(restored['rate_hourly'], 8)

    def test_unknown_or_mixed_g4_rate_does_not_invent_estimate(self):
        self.assertIsNone(self.cache.get()['gpu_estimates']['G4']['hours'])
        self.time += 30
        self.provider.return_value = {'runtimes':[{'gpu':'G4'}, {'gpu':'A100'}], 'consumption':{'paid_compute_units_balance':200, 'consumption_rate_hourly':16, 'assignments_count':2}}
        self.assertIsNone(self.cache.get()['gpu_estimates']['G4']['hours'])


if __name__ == '__main__':
    unittest.main()

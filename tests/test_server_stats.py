"""Aggregate metrics correctness, cached reads, and sampler lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from server_stats import HostStatsSampler, cpu_counters, cpu_delta, memory_usage


class HostStatsTests(unittest.TestCase):
    def test_cpu_delta_excludes_guest_and_counts_iowait_as_idle(self):
        before = cpu_counters('cpu 100 0 50 500 100 0 0 0 20 0\ncpu0 1 2 3 4\n')
        after = cpu_counters('cpu 120 0 60 560 110 0 0 0 35 0\n')
        self.assertEqual(30.0, cpu_delta(before, after))
        self.assertIsNone(cpu_delta(None, after))
        self.assertIsNone(cpu_delta(after, after))
        # Kernel iowait accounting may decrease without a reboot/reset.
        self.assertEqual(20.0, cpu_delta((100,0,0,100,30,0,0,0), (120,0,0,180,20,0,0,0)))

    def test_cpu_counter_reset_has_no_spike_and_new_baseline_recovers(self):
        self.assertIsNone(cpu_delta((100,0,0,100,0,0,0,0), (10,0,0,5,0,0,0,0)))
        self.assertEqual(25.0, cpu_delta((10,0,0,5,0,0,0,0), (20,0,0,35,0,0,0,0)))
        self.assertIsNone(cpu_delta((0,)*8, (0,)*8))
        for invalid in ('', 'cpu0 1 2 3 4', 'cpu 1 -2 3 4'):
            with self.assertRaises((ValueError, IndexError)):
                cpu_counters(invalid)

    def test_memory_uses_available_instead_of_free_and_handles_missing_fields(self):
        self.assertEqual({'used_bytes': 600*1024, 'total_bytes': 1000*1024, 'percent': 60.0},
                         memory_usage('MemTotal: 1000 kB\nMemFree: 10 kB\nMemAvailable: 400 kB\n'))
        self.assertEqual({'used_bytes': None, 'total_bytes': 1024, 'percent': None}, memory_usage('MemTotal: 1 kB\n'))
        self.assertIsNone(memory_usage('MemTotal: 0 kB\nMemAvailable: 0 kB\n')['percent'])
        self.assertIsNone(memory_usage('MemTotal: 1 kB\nMemAvailable: 2 kB\n')['used_bytes'])

    def test_two_second_delta_and_concurrent_cache_reads_do_not_resample(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'stat').write_text('cpu 100 0 50 500 100 0 0 0\n')
            (root/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\n')
            with mock.patch('server_stats.time.monotonic', side_effect=[10.0,12.0,14.0]), \
                 mock.patch('server_stats.time.time', return_value=1234.0), \
                 mock.patch('server_stats.os.cpu_count', return_value=2), \
                 mock.patch('server_stats.os.getloadavg', return_value=(.1,.2,.3)), \
                 mock.patch('server_stats.shutil.disk_usage', return_value=SimpleNamespace(total=1000,used=250)) as disk:
                sampler = HostStatsSampler(root, proc_root=root, start=False)
                self.addCleanup(sampler.close)
                self.assertIsNone(sampler.snapshot()['cpu_percent'])
                self.assertIsNone(sampler.snapshot()['sample_seconds'])
                (root/'stat').write_text('cpu 120 0 60 560 110 0 0 0\n')
                sampler.sample()
                expected = sampler.snapshot()
                self.assertEqual(30.0, expected['cpu_percent'])
                self.assertEqual(2.0, expected['sample_seconds'])
                self.assertEqual(25.0, expected['disk']['percent'])
                self.assertEqual([.1,.2,.3], expected['load_average'])
                disk.assert_called_with(root)
                with mock.patch.object(sampler,'sample',side_effect=AssertionError('Request resampled CPU')):
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        self.assertTrue(all(item == expected for item in pool.map(lambda _:sampler.snapshot(),range(32))))
                mutated = sampler.snapshot(); mutated['memory']['used_bytes'] = -1; mutated['load_average'][0] = -1
                self.assertEqual(expected,sampler.snapshot())
                (root/'stat').write_text('cpu 0 0 0 0 0 0 0 0\n')
                sampler.sample()
                self.assertIsNone(sampler.snapshot()['cpu_percent'])

    def test_missing_linux_metrics_are_null_and_close_stops_background_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch('server_stats.shutil.disk_usage',side_effect=OSError), \
                 mock.patch('server_stats.os.getloadavg',side_effect=OSError):
                sampler = HostStatsSampler(temp,proc_root=temp)
                self.addCleanup(sampler.close)
                data = sampler.snapshot()
                self.assertIsNone(data['cpu_percent'])
                self.assertEqual({'used_bytes':None,'total_bytes':None,'percent':None},data['memory'])
                self.assertEqual(data['memory'],data['disk'])
                self.assertIsNone(data['load_average'])
                thread = sampler._thread
                sampler.close()
                self.assertFalse(thread.is_alive())
                sampler.start()
                self.assertIs(thread,sampler._thread)


if __name__=='__main__':
    unittest.main()

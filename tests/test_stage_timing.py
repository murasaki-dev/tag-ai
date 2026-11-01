"""Measured timings stay bounded, thread-safe and distinct from cache reuse."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
import providers
from stage_timing import StageTimings, timed_stage, timing_scope
from storage import PersistentResultCache
from PIL import Image


class StageTimingTests(unittest.TestCase):
    def test_nested_live_elapsed_and_completed_average(self):
        now = [0.]; clock = StageTimings(lambda: now[0]); owner = SimpleNamespace(_stage_timings=clock)
        with timed_stage(owner, 'iqdb'):
            now[0] = 2
            with timed_stage(owner, 'iqdb.http'):
                now[0] = 5
                rows, _, active = clock.snapshot()
                self.assertTrue(active); self.assertEqual(rows['iqdb.http']['elapsed'], 3)
            now[0] = 7
        rows, _, active = clock.snapshot()
        self.assertFalse(active); self.assertEqual(rows['iqdb']['average'], 7)
        self.assertEqual(rows['iqdb.http']['average'], 3)

    def test_failures_count_but_cancellations_do_not_change_average(self):
        now = [0.]; clock = StageTimings(lambda: now[0]); owner = SimpleNamespace(_stage_timings=clock, skip_requested=False)
        with self.assertRaises(ValueError):
            with timed_stage(owner, 'vision.request'): now[0] = 4; raise ValueError('failed')
        with timed_stage(owner, 'vision.request'): now[0] = 20; owner.skip_requested = True
        row = clock.snapshot()[0]['vision.request']
        self.assertEqual((row['count'], row['errors'], row['cancelled'], row['average']), (1, 1, 1, 4))
        clock.record('invalid', float('nan')); self.assertNotIn('invalid', clock.snapshot()[0])

    def test_parallel_active_samples_and_reset(self):
        clock = StageTimings(); owner = SimpleNamespace(_stage_timings=clock)
        entered = threading.Barrier(3); release = threading.Event()
        def worker():
            with timed_stage(owner, 'web.lens'):
                entered.wait(timeout=2); release.wait(2)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        entered.wait(timeout=2)
        self.assertEqual(clock.snapshot()[0]['web.lens']['active'], 2); self.assertFalse(clock.reset())
        release.set()
        for thread in threads: thread.join(2)
        self.assertEqual(clock.snapshot()[0]['web.lens']['count'], 2)
        self.assertTrue(clock.reset()); self.assertEqual(clock.snapshot()[0], {})

    def test_http_substeps_and_json_parse_never_store_urls_or_keys(self):
        clock = StageTimings(); owner = SimpleNamespace(_stage_timings=clock)
        response = requests.Response(); response.status_code = 200; response._content = b'{"ok":true}'
        with patch.object(requests.Session, 'request', return_value=response), timed_stage(owner, 'web.guided'):
            result = providers.TimedSession().get('https://serpapi.com/search?api_key=private-key')
            self.assertTrue(result.json()['ok'])
        rows = clock.snapshot()[0]
        self.assertEqual(rows['web.guided.search']['count'], 1); self.assertEqual(rows['web.guided.json']['count'], 1)
        self.assertNotIn('private-key', repr(rows))

    def test_cached_lens_does_not_add_a_service_duration(self):
        class Harness(providers.ProviderMixin):
            stop_requested = skip_requested = False
            context_pipeline = [{'method': 'serpapi', 'enabled': True}]
            def _entries(self, name): return [(None, 'test-key')]
            def _read_widget(self, value): return value
            def update_metrics_display(self): pass
            def log(self, *args, **kwargs): pass
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.png'; Image.new('RGB', (8, 8), 'red').save(path)
            owner = Harness(); owner._stage_timings = StageTimings(); owner.result_cache = PersistentResultCache(str(Path(directory) / 'cache.sqlite3'))
            def success(*args): providers.tracker.serpapi_last_outcome = 'success'; return 'image evidence'
            with patch.object(providers, 'fetch_google_lens_context', side_effect=success) as fetch:
                owner._fetch_vision_context(str(path)); owner._fetch_vision_context(str(path)); fetch.assert_called_once()
            rows = owner._stage_timings.snapshot()[0]
            self.assertEqual(rows['web.lens']['count'], 1); self.assertEqual(rows['web.lens']['cached'], 1)
            self.assertEqual(rows['web.lens.cache.read']['count'], 2)
            owner.result_cache.close()


if __name__ == '__main__': unittest.main()

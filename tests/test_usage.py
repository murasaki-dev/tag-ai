"""Per-key counters follow actual requests without storing credential text."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers
from PIL import Image


class KeyUsageTests(unittest.TestCase):
    def test_attempts_failures_and_services_are_separate_and_private(self):
        tracker, http = providers.ApiTracker(), Mock()
        http.get.side_effect = [SimpleNamespace(status_code=429), SimpleNamespace(status_code=200), RuntimeError('offline')]
        with patch.object(providers, 'tracker', tracker), patch.object(providers, 'get_http_session', return_value=http):
            providers.tracked_key_request('serpapi', 'primary-secret', 'get', 'unused')
            providers.tracked_key_request('serpapi', 'backup-secret', 'get', 'unused')
            with self.assertRaises(RuntimeError):
                providers.tracked_key_request('gvision', 'primary-secret', 'get', 'unused')
        self.assertEqual(tracker.key_snapshot('serpapi', 'primary-secret')['errors'], 1)
        self.assertEqual(tracker.key_snapshot('serpapi', 'backup-secret')['http_ok'], 1)
        self.assertEqual(tracker.key_snapshot('gvision', 'primary-secret')['requests'], 1)
        self.assertNotIn('primary-secret', repr(tracker.key_usage))
        self.assertNotIn('backup-secret', repr(tracker.key_usage))

    def test_serpapi_backup_upload_and_search_are_counted_per_key(self):
        tracker, http = providers.ApiTracker(), Mock()
        http.post.side_effect = lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: {'image_id': kwargs['data']['api_key']})
        http.get.side_effect = lambda *args, **kwargs: SimpleNamespace(status_code=429, json=lambda: {'error': 'out of searches'}) if kwargs['params']['api_key'] == 'primary' else SimpleNamespace(status_code=200, json=lambda: {'visual_matches': [{'title': 'series character'}]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.png'; Image.new('RGB', (8, 8)).save(path)
            with patch.object(providers, 'tracker', tracker), patch.object(providers, 'get_http_session', return_value=http), patch.object(providers, 'refresh_serpapi_account_status', return_value=None):
                result = providers.fetch_google_lens_context(str(path), ['primary', 'backup'])
        self.assertIn('series character', result)
        for key in ('primary', 'backup'):
            self.assertEqual(tracker.key_snapshot('serpapi', key)['requests'], 1)
            self.assertEqual(tracker.key_snapshot('serpapi', key)['uploads'], 1)
        self.assertEqual(tracker.key_snapshot('serpapi', 'primary')['errors'], 1)

    def test_cloud_empty_results_still_count_as_requests(self):
        tracker, http = providers.ApiTracker(), Mock()
        http.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'responses': [{}]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.png'; Image.new('RGB', (8, 8)).save(path)
            with patch.object(providers, 'tracker', tracker), patch.object(providers, 'get_http_session', return_value=http):
                self.assertEqual(providers.fetch_google_vision_web_detection(str(path), ['one', 'two']), '')
        self.assertEqual(tracker.gvision_calls, 0)
        self.assertEqual(tracker.key_snapshot('gvision', 'one')['requests'], 1)
        self.assertEqual(tracker.key_snapshot('gvision', 'two')['requests'], 1)

    def test_saucenao_retains_each_server_quota_instead_of_overwriting_backup(self):
        tracker, http = providers.ApiTracker(), Mock()
        def response(*args, **kwargs):
            balance = 7 if kwargs['params']['api_key'] == 'one' else 93
            return SimpleNamespace(status_code=200, json=lambda: {'header': {'short_remaining': 3, 'long_remaining': balance}, 'results': []})
        http.post.side_effect = response
        app = SimpleNamespace(log=lambda *args, **kwargs: None, update_metrics_display=lambda: None, stop_requested=False, skip_requested=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.png'; Image.new('RGB', (8, 8)).save(path)
            with patch.object(providers, 'tracker', tracker), patch.object(providers, 'get_http_session', return_value=http):
                providers.ProviderMixin.search_saucenao_engine(app, str(path), ['one', 'two'])
        self.assertEqual(tracker.key_snapshot('saucenao', 'one')['quota']['long_remaining'], 7)
        self.assertEqual(tracker.key_snapshot('saucenao', 'two')['quota']['long_remaining'], 93)


if __name__ == '__main__':
    unittest.main()

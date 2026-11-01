"""Independent and dependent evidence stay distinct; no external calls in tests."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import providers
from storage import PersistentResultCache, stream_file_md5
from tagger import EVA_MODEL, PIXAI_MODEL, normalize_predictions, threshold_predictions, guided_query


class Harness(providers.ProviderMixin):
    stop_requested = skip_requested = False
    context_pipeline = [{'method': 'serpapi', 'enabled': True}, {'method': 'gvision', 'enabled': True}]
    def __init__(self, cache):
        self.result_cache = cache; self.logs = []
        self._run_evidence_options = dict(context_mode='First available', tagger_enabled=True,
            tagger_model=EVA_MODEL, tagger_url='http://localhost:7861/tag', tagger_guided_lens=False,
            tagger_identity_threshold=.85, tagger_general_threshold=.5)
    def _entries(self, name): return [(None, 'test-key')]
    def _read_widget(self, value): return value
    def update_metrics_display(self): pass
    def log(self, message, **kwargs): self.logs.append(message)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.image = self.root / 'image.png'; Image.new('RGB', (20, 20), 'red').save(self.image)
        self.cache = PersistentResultCache(str(self.root / 'cache.sqlite3')); self.h = Harness(self.cache)
    def tearDown(self): self.cache.close(); self.temp.cleanup()
    @staticmethod
    def lens(*args, **kwargs):
        providers.tracker.serpapi_last_outcome = 'success'
        return 'Guided character pages' if kwargs.get('query') else 'Independent image results'
    def response(self, model=EVA_MODEL):
        response = Mock()
        response.json.return_value = {'model': model, 'results': {'character': {'test_character': .92, 'weak_guess': .3}, 'copyright': {}, 'general': {'blue_hair': .99}}}
        return response
    def test_independent_search_and_tagger_really_run_concurrently(self):
        barrier = threading.Barrier(2)
        def web(*args, **kwargs): barrier.wait(timeout=2); return self.lens()
        def tagger(*args): barrier.wait(timeout=2); return {'character': {'someone_else': .95}}
        with patch.object(providers, 'fetch_google_lens_context', side_effect=web) as search, patch.object(self.h, '_fetch_tagger_context', side_effect=tagger):
            context, count = self.h._fetch_vision_context(str(self.image))
        self.assertEqual(count, 3)
        self.assertIn('INDEPENDENT IMAGE SEARCH', context); self.assertIn('UNVERIFIED LOCAL TAGGER', context)
        self.assertNotIn('someone_else', str(search.call_args)); self.assertFalse(self.h._evidence_unavailable)
    def test_guided_search_is_extra_labelled_cached_and_does_not_replace_lens(self):
        self.h._run_evidence_options['tagger_guided_lens'] = True
        session = Mock(); session.post.return_value = self.response()
        with patch.object(providers, 'get_http_session', return_value=session), patch.object(providers, 'fetch_google_lens_context', side_effect=self.lens) as search:
            first, _ = self.h._fetch_vision_context(str(self.image))
            second, _ = self.h._fetch_vision_context(str(self.image))
            self.assertEqual(first, second); self.assertEqual(search.call_count, 2); session.post.assert_called_once()
            self.assertNotIn('query', search.call_args_list[0].kwargs)
            self.assertEqual(search.call_args_list[1].kwargs['query'], 'test character')
            self.assertIn('DEPENDENT EVIDENCE', first); self.assertIn('Independent image results', first)
            self.cache.forget_image({stream_file_md5(self.image)}, stream_file_md5(self.image))
            self.h._fetch_vision_context(str(self.image)); self.assertEqual(search.call_count, 4)
            self.assertEqual(session.post.call_count, 2)
    def test_combine_web_providers_is_parallel_and_preserves_both_sources(self):
        self.h._run_evidence_options.update(context_mode='Combine enabled', tagger_enabled=False)
        barrier = threading.Barrier(2)
        def web(*args): barrier.wait(timeout=2); return self.lens()
        def cloud(*args): barrier.wait(timeout=2); return 'Different identity from cloud'
        with patch.object(providers, 'fetch_google_lens_context', side_effect=web), patch.object(providers, 'fetch_google_vision_web_detection', side_effect=cloud):
            context, _ = self.h._fetch_vision_context(str(self.image))
        self.assertIn('Independent image results', context); self.assertIn('Different identity from cloud', context)
        self.assertEqual(self.h._last_vision_context_provider, 'serpapi+gvision')
    def test_failed_tagger_does_not_block_web_and_is_retried(self):
        with patch.object(self.h, '_fetch_tagger_context', side_effect=RuntimeError('offline')) as tagger, patch.object(providers, 'fetch_google_lens_context', side_effect=self.lens):
            for _ in range(2):
                context, _ = self.h._fetch_vision_context(str(self.image))
                self.assertIn('Independent image results', context); self.assertTrue(self.h._evidence_unavailable)
            self.assertEqual(tagger.call_count, 2)
    def test_general_tags_cannot_trigger_guided_identity_search(self):
        self.h._run_evidence_options['tagger_guided_lens'] = True
        with patch.object(self.h, '_fetch_tagger_context', return_value={'general': {'blue_hair': .99}}), patch.object(providers, 'fetch_google_lens_context', side_effect=self.lens) as search:
            self.h._fetch_vision_context(str(self.image)); search.assert_called_once()
    def test_tagger_cache_is_model_and_threshold_specific(self):
        session = Mock(); session.post.return_value = self.response()
        with patch.object(providers, 'get_http_session', return_value=session):
            first = self.h._fetch_tagger_context(str(self.image), self.h._run_evidence_options)
            self.assertEqual(first['character'], {'test_character': .92})
            self.h._fetch_tagger_context(str(self.image), self.h._run_evidence_options)
            self.assertEqual(session.post.call_count, 1)
            self.h._run_evidence_options['tagger_identity_threshold'] = .95
            self.assertFalse(self.h._fetch_tagger_context(str(self.image), self.h._run_evidence_options)['character'])
            self.assertEqual(session.post.call_count, 2)
            self.h._run_evidence_options['tagger_model'] = PIXAI_MODEL
            with self.assertRaisesRegex(ValueError, 'different model'): self.h._fetch_tagger_context(str(self.image), self.h._run_evidence_options)
    def test_scores_are_filtered_and_bounded(self):
        value = {'character': {'valid': .9, 'nan': float('nan'), 'boolean': True, 'negative': -1}, 'general': {'hair': .8}}
        self.assertEqual(normalize_predictions(value)['character'], {'valid': .9})
        self.assertEqual(guided_query(threshold_predictions(value)), 'valid')
        with self.assertRaises(ValueError): normalize_predictions({'copyright': ['not a map']})


if __name__ == '__main__': unittest.main()

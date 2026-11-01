"""SerpApi context survives model changes and failed vision without extra credits."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import providers
from storage import PersistentResultCache
from PIL import Image

class Harness(providers.ProviderMixin):
    stop_requested=skip_requested=False
    context_pipeline=[{'method':'serpapi','enabled':True},{'method':'gvision','enabled':True}]
    def __init__(self,cache):self.result_cache=cache;self.logs=[];self.key='secret-primary'
    def _entries(self,name):return [(None,self.key)]
    def _read_widget(self,value):return value
    def update_metrics_display(self):pass
    def log(self,message,**kwargs):self.logs.append(message)

class ContextCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.image=self.root/'red.png';Image.new('RGB',(20,20),'red').save(self.image)
        self.cache=PersistentResultCache(str(self.root/'cache.sqlite3'));self.h=Harness(self.cache)
    def tearDown(self):self.cache.close();self.temp.cleanup()
    @staticmethod
    def success(*args):providers.tracker.serpapi_last_outcome='success';return 'Series and characters from Lens'
    def test_context_persists_across_reopen_keys_and_move_without_new_requests(self):
        with patch.object(providers,'fetch_google_lens_context',side_effect=self.success) as fetch:
            first=self.h._fetch_vision_context(str(self.image));self.assertEqual(fetch.call_count,1)
            moved=self.root/'moved.png';self.image.rename(moved)
            second=Harness(PersistentResultCache(self.cache.path));second.key='another-secret'
            self.assertEqual(second._fetch_vision_context(str(moved)),first);self.assertEqual(fetch.call_count,1)
            self.assertEqual(second._last_vision_context_provider,'serpapi')
            self.assertIn('no upload, search or balance request',' '.join(second.logs))
        with closing(sqlite3.connect(self.cache.path)) as connection:
            payload=' '.join(str(row) for row in connection.execute('SELECT image_hash,settings_signature,payload FROM image_results'))
        self.assertNotIn('secret-primary',payload);self.assertNotIn('another-secret',payload)
    def test_transient_failures_are_not_cached(self):
        def unavailable(*args):providers.tracker.serpapi_last_outcome='unavailable';return ''
        with patch.object(providers,'fetch_google_lens_context',side_effect=unavailable) as fetch,patch.object(providers,'fetch_google_vision_web_detection',return_value=''):
            self.h._fetch_vision_context(str(self.image));self.h._fetch_vision_context(str(self.image))
            self.assertEqual(fetch.call_count,2)
    def test_empty_completed_results_expire_and_allow_context_fallback(self):
        def miss(*args):providers.tracker.serpapi_last_outcome='no_match';return ''
        with patch.object(providers,'fetch_google_lens_context',side_effect=miss) as fetch,patch.object(providers,'fetch_google_vision_web_detection',return_value='cloud hints'):
            with patch('storage.time.time',return_value=1000):
                self.assertEqual(self.h._fetch_vision_context(str(self.image))[0],'cloud hints')
                self.assertEqual(self.h._fetch_vision_context(str(self.image))[0],'cloud hints')
                self.assertEqual(fetch.call_count,1)
            with patch('storage.time.time',return_value=1000+self.cache.NEGATIVE_TTL_SECONDS+1):
                self.h._fetch_vision_context(str(self.image));self.assertEqual(fetch.call_count,2)
    def test_different_image_fetches_new_hints_and_clear_removes_context(self):
        other=self.root/'blue.png';Image.new('RGB',(20,20),'blue').save(other)
        with patch.object(providers,'fetch_google_lens_context',side_effect=self.success) as fetch:
            self.h._fetch_vision_context(str(self.image));self.h._fetch_vision_context(str(other));self.assertEqual(fetch.call_count,2)
            self.cache.clear();self.h._fetch_vision_context(str(self.image));self.assertEqual(fetch.call_count,3)

if __name__=='__main__':unittest.main()

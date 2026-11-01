import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers
import routing
from storage import PersistentResultCache


class Harness(providers.ProviderMixin):
    stop_requested = skip_requested = False
    _run_booru_aliases = True
    def __init__(self, cache):
        self.result_cache = cache; self.logs = []; self.safe_get_request = Mock()
        self.database_pipelines = self._run_database_pipelines = {'IQDB': [{'method': 'danbooru', 'enabled': True}]}
        self._run_search_pipeline = [{'method': 'IQDB', 'enabled': True}]
    def _entries(self, name): return []
    def _read_widget(self, value): return value
    def log(self, message, **kwargs): self.logs.append(message)
    def update_metrics_display(self): pass


def row(a, b, category=3, status='active'):
    return dict(antecedent_name=a, consequent_name=b, status=status, consequent_tag={'category': category})


def response(rows, status=200):
    result=Mock(status_code=status);result.json.return_value=rows;return result


class BooruAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.cache=PersistentResultCache(str(self.root/'cache.sqlite3'));self.h=Harness(self.cache)
        self.limiter=patch.object(providers.tracker.danbooru_limiter,'record',return_value=1);self.limiter.start()
    def tearDown(self):self.limiter.stop();self.cache.close();self.temp.cleanup()

    def test_active_series_and_character_aliases_route_without_rewriting_tags(self):
        tags=dict(copyright='new_series',character='new_hero',combined='new_series new_hero',_identity_source='booru')
        before=copy.deepcopy(tags);manual={'old_series':{'series':['manual_name']}};manual_before=copy.deepcopy(manual)
        self.h.safe_get_request.side_effect=[response([row('old_series','new_series')]),response([row('old_hero','new_hero',4)])]
        folders=['old_series','old_series/old_hero']
        enriched=self.h.enrich_booru_aliases(tags,folders,manual)
        self.assertEqual(routing.routing_plan(enriched,folders,manual)['direct'],'old_series/old_hero')
        self.assertEqual(tags,before);self.assertEqual(manual,manual_before);self.assertEqual(enriched['combined'],before['combined'])
        self.assertEqual(self.h.safe_get_request.call_count,2)
        self.h.enrich_booru_aliases(tags,folders,manual);self.assertEqual(self.h.safe_get_request.call_count,2)

    def test_exact_existing_match_and_disabled_provider_need_no_requests(self):
        tags=dict(copyright='series',character='',_identity_source='booru')
        self.assertEqual(self.h.enrich_booru_aliases(tags,['series'],{}),tags)
        self.h._run_database_pipelines['IQDB'][0]['enabled']=False
        self.h.enrich_booru_aliases(tags,['different_name'],{})
        self.h.safe_get_request.assert_not_called()

    def test_deleted_unrelated_wrong_category_and_conflicting_aliases_are_rejected(self):
        self.h.safe_get_request.return_value=response([row('folder','series',status='deleted'),row('folder','other'),row('folder','series',4),row('series','first'),row('series','second')])
        tags=self.h.enrich_booru_aliases(dict(copyright='series'),['folder'],{})
        self.assertFalse(tags.get('_booru_aliases'))
        self.assertIsNone(routing.routing_plan(tags,['folder'])['direct'])

    def test_empty_results_are_cached_but_network_failure_is_not(self):
        tags=dict(copyright='series');self.h.safe_get_request.return_value=response([])
        for _ in range(2):self.h.enrich_booru_aliases(tags,['folder'],{})
        self.assertEqual(self.h.safe_get_request.call_count,1)
        self.h.safe_get_request.return_value=response({},503)
        self.h.enrich_booru_aliases(dict(copyright='unknown'),['folder'],{})
        fresh=Harness(self.cache);fresh.safe_get_request.return_value=response([row('folder','unknown')])
        self.assertTrue(fresh.enrich_booru_aliases(dict(copyright='unknown'),['folder'],{}).get('_booru_aliases'))
        self.assertEqual(fresh.safe_get_request.call_count,1)

    def test_vision_aliases_do_not_promote_generated_identity_to_verified(self):
        self.h.safe_get_request.return_value=response([row('folder','series')])
        enriched=self.h.enrich_booru_aliases(dict(copyright='series',_identity_source='vision'),['folder'],{})
        plan=routing.routing_plan(enriched,['folder'])
        self.assertIsNone(plan['direct']);self.assertEqual(plan['candidates'][0]['folder'],'folder')
        self.assertIn('booru_identity_aliases',plan['evidence'])

    def test_shared_alias_entries_expire_without_removing_image_cache(self):
        key='context:booru_alias:danbooru:copyright:test'
        self.cache.put(key,'v1','resolved',{'facts':[]});self.cache.put('a'*32,'image','resolved',{'tags':'keep'})
        with closing(sqlite3.connect(self.cache.path)) as connection,connection:
            connection.execute('UPDATE image_results SET saved_at=? WHERE image_hash=?',(time.time()-31*86400,key))
        self.assertIsNone(self.cache.get(key,'v1'));self.assertEqual(self.cache.get('a'*32,'image')['tags'],'keep')

    def test_alias_graph_cycles_are_bounded_and_unrelated_facts_do_not_match(self):
        facts=[dict(provider='danbooru',status='active',category='copyright',antecedent=a,consequent=b) for a,b in [('one','two'),('two','one'),('unrelated','folder')]]
        self.assertEqual(set(routing.expand_identity_aliases(['one'],facts,'copyright')),{'one','two'})
        self.assertIsNone(routing.routing_plan(dict(copyright='one',_booru_aliases=facts),['folder'])['direct'])

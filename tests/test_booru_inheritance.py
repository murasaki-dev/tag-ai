import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers
from routing import DATABASE_METHODS


class Harness(providers.ProviderMixin):
    stop_requested = skip_requested = False
    def __init__(self, sources=('danbooru',)):
        self.database_pipelines = self._run_database_pipelines = {'IQDB': [{'method': name, 'enabled': name in sources} for name in DATABASE_METHODS]}
        self._run_search_pipeline = [{'method': 'IQDB', 'enabled': True}]
        self.logs = []; self.safe_get_request = Mock()
    def _entries(self, name):
        return [(None,value) for value in {'gb_user_entries':['123'], 'gb_key_entries':['fixture-key']}.get(name,[])]
    def _read_widget(self, value): return value
    def log(self, message, **kwargs): self.logs.append(message)
    def update_metrics_display(self): pass
    def update_match_thumbnail(self, url): pass


def response(value, status=200):
    result=Mock(status_code=status);result.json.return_value=value;return result


class BooruInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.limiters=patch.multiple(providers.tracker, **{name+'_limiter':Mock() for name in providers.HASH_DATABASES});self.limiters.start()
    def tearDown(self):self.limiters.stop()

    def test_gelbooru_missing_credentials_skip_requests_and_warn_once(self):
        h=Harness(('gelbooru',));h._entries=lambda name:[]
        self.assertEqual(h._search_booru_posts('gelbooru','hero'),[])
        self.assertEqual(h._search_booru_posts('gelbooru','hero'),[])
        h.fetch_tags('unused.jpg',{'booru_ids':{'gelbooru':'1'}},'Auto')
        h.safe_get_request.assert_not_called();self.assertTrue(h._hash_lookup_unavailable)
        providers.tracker.gelbooru_limiter.record.assert_not_called()
        warnings=[line for line in h.logs if '[Gelbooru]' in line]
        self.assertEqual(len(warnings),1);self.assertIn('numeric user ID',warnings[0])

    def test_gelbooru_rejected_pairs_are_skipped_and_blank_rows_stay_aligned(self):
        from urllib.parse import parse_qs,urlsplit
        h=Harness(('gelbooru',));del h.safe_get_request
        rows={'gb_user_entries':['','123','456'],'gb_key_entries':['orphan','bad-key','good&key']}
        h._entries=lambda name:[(None,value) for value in rows.get(name,[])]
        denied=response({},401);denied.reason='Unauthorized'
        accepted=response({'post':[{'id':1,'tags':'hero'}]})
        with patch.object(providers,'tracked_key_request',side_effect=[denied,accepted,accepted]) as request:
            self.assertTrue(h._search_booru_posts('gelbooru','hero'))
            self.assertTrue(h._search_booru_posts('gelbooru','hero'))
        queries=[parse_qs(call.args[3].split('?',1)[1]) for call in request.call_args_list]
        self.assertEqual([q['user_id'][0] for q in queries],['123','456','456'])
        self.assertEqual(queries[1]['api_key'],['good&key'])
        self.assertEqual(sum('401' in line for line in h.logs),1)
        self.assertFalse(any('bad-key' in line or 'good&key' in line for line in h.logs))

    def test_exact_hash_inherits_tags_without_a_second_post_request(self):
        h=Harness();digest='a'*32
        h.safe_get_request.return_value=response([{'id':5,'md5':digest,'tag_string':'series hero','tag_string_copyright':'series','tag_string_character':'hero','preview_file_url':'https://example.com/preview.jpg'}])
        matches=h.search_booru_hash('unused.jpg',digest)
        self.assertEqual(len(matches),1);self.assertEqual(matches[0]['tag_dict_override']['copyright'],'series')
        self.assertEqual(matches[0]['tag_dict_override']['matched_url'],'https://danbooru.donmai.us/posts/5')
        self.assertEqual(h.safe_get_request.call_count,1);self.assertIn('md5%3A'+digest,h.safe_get_request.call_args.args[0])

    def test_wrong_or_missing_hash_is_never_accepted(self):
        for value in ('b'*32,None):
            h=Harness();h.safe_get_request.return_value=response([{'id':1,'md5':value,'tag_string':'wrong_series'}])
            self.assertEqual(h.search_booru_hash('unused.jpg','a'*32),[])

    def test_hash_lookup_uses_backup_credentials_and_marks_transient_failure(self):
        h=Harness();h._entries=lambda name: [(None,value) for value in {'db_user_entries':['first','backup'],'db_key_entries':['key-a','key-b']}.get(name,[])]
        h.safe_get_request.side_effect=[response({},403),response([dict(id=3,md5='a'*32,tag_string='series')])]
        self.assertTrue(h.search_booru_hash('unused.jpg','a'*32))
        self.assertEqual([call.kwargs['auth'] for call in h.safe_get_request.call_args_list],[('first','key-a'),('backup','key-b')])
        self.assertTrue(h._hash_lookup_unavailable)

    def test_explicit_source_link_fills_empty_tags_and_stops_at_first_success(self):
        h=Harness(('danbooru','gelbooru'))
        h.safe_get_request.side_effect=[response({'id':1,'tag_string':'','source':'https://gelbooru.com/index.php?page=post&s=view&id=2'}),response({'post':[{'id':2,'tags':'series hero','preview_url':'https://example.com/match.jpg'}]})]
        tags=h.fetch_tags('unused.jpg',{'booru_ids':{'danbooru':'1'}},'Auto')
        self.assertEqual(tags['combined'],'series hero');self.assertIn('id=2',tags['matched_url'])
        self.assertEqual(tags['_source_chain'],['danbooru:1','gelbooru:2']);self.assertEqual(h.safe_get_request.call_count,2)

    def test_disabled_source_and_cycles_do_not_trigger_extra_requests(self):
        h=Harness();h.safe_get_request.return_value=response({'id':1,'source':'https://gelbooru.com/index.php?page=post&s=view&id=2'})
        h.fetch_tags('unused.jpg',{'booru_ids':{'danbooru':'1'}},'Auto')
        self.assertEqual(h.safe_get_request.call_count,1)
        h=Harness(('danbooru','gelbooru'));h.safe_get_request.side_effect=[response({'id':1,'source':'https://gelbooru.com/index.php?page=post&s=view&id=2'}),response({'post':[{'id':2,'source':'https://danbooru.donmai.us/posts/1'}]})]
        h.fetch_tags('unused.jpg',{'booru_ids':{'danbooru':'1'}},'Auto');self.assertEqual(h.safe_get_request.call_count,2)

    def test_source_search_requires_unique_exact_source_url(self):
        url='https://www.pixiv.net/artworks/25'
        for posts,expected in [([{'id':2,'source':url,'tag_string':'series'}],'series'),([{'id':2,'source':url+'/other','tag_string':'wrong'}],''),([{'id':2,'source':url,'tag_string':'ambiguous'},{'id':3,'source':url,'tag_string':'other'}],'')]:
            h=Harness();h.safe_get_request.side_effect=[response({'id':1,'source':url}),response(posts)]
            tags=h.fetch_tags('unused.jpg',{'booru_ids':{'danbooru':'1'}},'Auto')
            self.assertEqual(tags.get('combined',''),expected)

    def test_link_parser_accepts_known_posts_and_rejects_untrusted_hosts(self):
        self.assertEqual(providers.linked_booru_post('https://i.pximg.net/img-original/2020/01/01/00/00/00/123_p0.jpg'),('pixiv','123'))
        for url in ['https://danbooru.donmai.us.evil.example/posts/1','http://127.0.0.1/posts/1','https://pixiv.net/users/12','file:///posts/1']:
            self.assertIsNone(providers.linked_booru_post(url))

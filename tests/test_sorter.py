import os
import errno
import json
import sys
import tempfile
import threading
import time
import queue
import sqlite3
import weakref
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ui as sorter
import providers
import routing
import storage
import history
import workflow
from PIL import Image, PngImagePlugin
import piexif


class SerpApiFailoverTests(unittest.TestCase):
    @staticmethod
    def response(status, data):
        return SimpleNamespace(status_code=status, text=json.dumps(data), json=lambda: data)

    def exercise(self, primary_search, primary_upload=200, balances=None, repeats=1):
        from unittest.mock import Mock
        session = Mock()
        uploads, searches, logs = [], [], []
        def upload(url, **kwargs):
            key = kwargs['data']['api_key']; uploads.append(key)
            if key == 'primary' and primary_upload != 200:
                return self.response(primary_upload, {'error': 'Invalid API key'})
            return self.response(200, {'image_id': f'upload-{key}'})
        def search(url, **kwargs):
            key = kwargs['params']['api_key']; searches.append(key)
            self.assertEqual(kwargs['params']['image_id'], f'upload-{key}')
            if key == 'primary':
                return primary_search
            return self.response(200, {'search_metadata': {'status': 'Success', 'id': 'backup-result'},
                                       'visual_matches': [{'title': 'Frieren Fern', 'source': 'example', 'link': 'https://example.com'}]})
        session.post.side_effect, session.get.side_effect = upload, search
        def balance(key, index, callback=None, force=False):
            return (balances or {}).get(key)
        with tempfile.TemporaryDirectory() as folder, patch.object(providers, 'tracker', providers.ApiTracker()), \
             patch.object(providers, 'get_http_session', return_value=session), \
             patch.object(providers, 'refresh_serpapi_account_status', side_effect=balance):
            path = Path(folder) / 'image.png'; Image.new('RGB', (8, 8), 'red').save(path)
            results = [providers.fetch_google_lens_context(str(path), ['primary', 'backup'],
                       lambda message, **kwargs: logs.append(message)) for _ in range(repeats)]
            count = providers.tracker.serpapi_calls
            health = list(providers.tracker.serpapi_key_cooldowns.values())
        return results, uploads, searches, count, logs, health

    def test_quota_error_rotates_and_skips_exhausted_key_on_next_image(self):
        result, uploads, searches, count, logs, _ = self.exercise(
            self.response(429, {'error': 'Your account has run out of searches.'}), repeats=2)
        self.assertTrue(all('Frieren Fern' in value for value in result))
        self.assertEqual(uploads, ['primary', 'backup', 'backup'])
        self.assertEqual(searches, ['primary', 'backup', 'backup'])
        self.assertEqual(count, 2)
        self.assertTrue(any('exhausted' in line for line in logs))

    def test_http_200_quota_error_also_uses_backup(self):
        result, uploads, _, count, _, _ = self.exercise(self.response(200, {'error': 'Your account has run out of searches.'}))
        self.assertIn('Frieren Fern', result[0]); self.assertEqual(uploads, ['primary', 'backup']); self.assertEqual(count, 1)

    def test_account_balance_skips_upload_and_total_includes_extra_credits(self):
        success = self.response(200, {'search_metadata': {'status': 'Success'}, 'visual_matches': [{'title': 'primary match'}]})
        result = self.exercise(success, balances={'primary': {'total_searches_left': 0.0}})
        self.assertEqual(result[1], ['backup'])
        result = self.exercise(success, balances={'primary': {'plan_searches_left': 0, 'total_searches_left': 5}})
        self.assertEqual(result[1], ['primary']); self.assertIn('primary match', result[0][0])

    def test_bad_upload_key_and_temporary_rate_limit_rotate(self):
        result = self.exercise(self.response(200, {}), primary_upload=401)
        self.assertEqual(result[1], ['primary', 'backup']); self.assertIn('Frieren Fern', result[0][0])
        result = self.exercise(self.response(429, {'error': 'Hourly throughput exceeded'}),
                               balances={'primary': {'total_searches_left': 10}})
        self.assertIn('temporarily rate limited', result[-1][0][1])

    def test_success_metadata_and_no_match_do_not_spend_backup_credits(self):
        result = self.exercise(self.response(200, {'search_metadata': {'status': 'Success', 'id': 'abc'},
                              'visual_matches': [{'title': 'success'}]}))
        self.assertEqual(result[1], ['primary']); self.assertIn('success', result[0][0]); self.assertEqual(result[3], 1)
        result = self.exercise(self.response(200, {'search_metadata': {'status': 'Success'}, 'error': "Google hasn't returned any results"}))
        self.assertEqual(result[0], ['']); self.assertEqual(result[1], ['primary'])


class HistoryThumbnailQueueTests(unittest.TestCase):
    def test_image_application_is_limited_per_ui_tick(self):
        class Label:
            _history_thumb_token = 'current'
            def winfo_exists(self):
                return True
        label = Label()
        applied = []
        target = SimpleNamespace(_history_image_results=queue.SimpleQueue(), winfo_exists=lambda: True,
                                 tabview=SimpleNamespace(get=lambda: 'History'), after=lambda *args: None,
                                 _apply_history_thumbnail=lambda *args: applied.append(args), _drain_history_thumbnails=lambda: None)
        for index in range(5):
            target._history_image_results.put((weakref.ref(label), 'current', object(), index))
        sorter.BooruManagerApp._drain_history_thumbnails(target)
        self.assertEqual(len(applied), 2)
        self.assertEqual(target._history_image_results.qsize(), 3)

    def test_recycled_row_rejects_old_thumbnail(self):
        class Label:
            _history_thumb_token = 'new-entry'
        label = Label()
        target = SimpleNamespace(_history_image_results=queue.SimpleQueue())
        sorter.BooruManagerApp._queue_history_thumbnail(target, weakref.ref(label), 'old-entry', object(), 'stale')
        self.assertTrue(target._history_image_results.empty())
        sorter.BooruManagerApp._queue_history_thumbnail(target, weakref.ref(label), 'new-entry', object(), 'current')
        self.assertEqual(target._history_image_results.get_nowait()[-1], 'current')


class SearchPipelineTests(unittest.TestCase):
    def test_database_priority_orders_sources_then_similarity_and_deduplicates(self):
        flow = [{'method': 'danbooru', 'enabled': True}, {'method': 'yandere', 'enabled': True}]
        matches = [{'similarity': 99, 'booru_ids': {'danbooru': '5', 'yandere': '1'}},
                   {'similarity': 76, 'booru_ids': {'danbooru': '2'}},
                   {'similarity': 80, 'booru_ids': {'danbooru': '5', 'pixiv': '3'}}]
        ranked = routing.database_ordered_candidates(matches, flow, 'SauceNAO')
        self.assertEqual([item['booru_ids'] for item in ranked], [{'danbooru': '5'}, {'danbooru': '2'}, {'yandere': '1'}])
        self.assertTrue(all(item['_search_engine'] == 'SauceNAO' for item in ranked))
        self.assertEqual(matches[0]['booru_ids'], {'danbooru': '5', 'yandere': '1'})

    def test_database_legacy_preference_and_context_disabled_defaults(self):
        flow = routing.legacy_database_flow('Gelbooru')
        self.assertEqual(routing.enabled_flow_methods(flow, routing.DATABASE_METHODS)[0], 'gelbooru')
        self.assertEqual(len(flow), 6)
        self.assertEqual(routing.enabled_flow_methods([], routing.CONTEXT_METHODS), [])
        with self.assertRaises(ValueError):
            routing.normalize_named_flow([{'method': 'IQDB', 'enabled': True}], routing.CONTEXT_METHODS)

    def test_legacy_settings_keep_their_original_order(self):
        self.assertEqual(routing.enabled_search_methods(routing.legacy_search_pipeline('SauceNAO', True, True)),
                         ['SauceNAO', 'IQDB', 'Local Vision LLM'])
        self.assertEqual(routing.enabled_search_methods(routing.legacy_search_pipeline('Local Vision LLM', True, True)),
                         ['Local Vision LLM'])

    def test_custom_order_and_disabled_steps(self):
        flow = [{'method': 'Local Vision LLM', 'enabled': True}, {'method': 'IQDB', 'enabled': False},
                {'method': 'SauceNAO', 'enabled': True}]
        self.assertEqual(routing.enabled_search_methods(flow), ['Local Vision LLM', 'SauceNAO'])
        self.assertEqual(routing.enabled_search_methods([]), [])
        self.assertEqual(len(routing.normalize_search_pipeline([])), 3)

    def test_unknown_duplicate_and_malformed_steps_are_rejected(self):
        for value in ({}, ['IQDB'], [{'method': 'other', 'enabled': True}], [{'method': 'IQDB', 'enabled': 1}],
                      [{'method': 'IQDB', 'enabled': True}, {'method': 'IQDB', 'enabled': False}]):
            with self.assertRaises(ValueError):
                routing.normalize_search_pipeline(value)


class FileSafetyTests(unittest.TestCase):
    def test_cross_volume_move_copies_only_when_rename_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source', Path(folder) / 'target'
            source.write_bytes(b'original')
            with patch.object(sorter.os, 'replace', side_effect=OSError(errno.EXDEV, 'different volumes')):
                storage.safe_move(str(source), str(target))
            self.assertEqual(target.read_bytes(), b'original')
            self.assertFalse(source.exists())

    def test_collision_preserves_both_files_and_undo_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source.png', Path(folder) / 'target.png'
            source.write_bytes(b'new'); target.write_bytes(b'existing')
            moved = storage.safe_move(str(source), str(target))
            self.assertEqual(target.read_bytes(), b'existing')
            self.assertEqual(Path(moved).read_bytes(), b'new')
            source.write_bytes(b'new arrival')
            restored = storage.safe_move(moved, str(source))
            self.assertEqual(source.read_bytes(), b'new arrival')
            self.assertEqual(Path(restored).read_bytes(), b'new')

    def test_png_preserves_pixels_and_other_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'image.png'
            info = PngImagePlugin.PngInfo()
            info.add_text('Description', 'keep me')
            Image.new('RGBA', (23, 17), (12, 34, 56, 78)).save(path, pnginfo=info)
            with Image.open(path) as image:
                pixels = image.tobytes()
            storage.write_image_tags(str(path), 'series character 日本語')
            with Image.open(path) as image:
                self.assertEqual(image.tobytes(), pixels)
                self.assertEqual(image.info['Description'], 'keep me')
                self.assertEqual(image.info['booru_tags'], 'series character 日本語')
            storage.write_image_tags(str(path), '')
            with Image.open(path) as image:
                self.assertNotIn('booru_tags', image.info)
                self.assertEqual(image.info['Description'], 'keep me')

    def test_jpeg_preserves_compressed_scan_and_existing_exif(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'image.jpg'
            exif = piexif.dump({'0th': {piexif.ImageIFD.Artist: b'Original artist'}, 'Exif': {}})
            Image.new('RGB', (25, 25), 'orange').save(path, exif=exif)
            before = path.read_bytes()
            scan = before[before.index(b'\xff\xda'):]
            for text in ('new tags', ''):
                storage.write_image_tags(str(path), text)
                after = path.read_bytes()
                self.assertEqual(after[after.index(b'\xff\xda'):], scan)
                self.assertEqual(piexif.load(str(path))['0th'][piexif.ImageIFD.Artist], b'Original artist')

    def test_invalid_png_leaves_original_intact(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'broken.png'; path.write_bytes(b'broken')
            with self.assertRaises(ValueError):
                storage.write_image_tags(str(path), 'tags')
            self.assertEqual(path.read_bytes(), b'broken')


class RoutingTests(unittest.TestCase):
    def test_aliases_and_qualified_characters_route_without_llm(self):
        aliases = {'Frieren': {'series': ['sousou_no_frieren']}}
        plan = routing.routing_plan({'copyright': 'sousou_no_frieren', 'character': 'fern_(sousou_no_frieren)'},
                                   ['Frieren', 'Frieren/Fern', 'Other', 'Other/Fern'], aliases)
        self.assertEqual(plan['direct'], 'Frieren/Fern')
        self.assertEqual({c['folder'] for c in plan['candidates']}, {'Frieren', 'Frieren/Fern'})

    def test_wrong_folders_and_appearance_only_evidence_are_rejected(self):
        plan = routing.routing_plan({'copyright': 'frieren', 'character': 'fern'}, ['Frieren', 'Frieren/Fern', 'Other'])
        for raw in ('{"candidate_id":999}', '{"destination":"Other"}', '{"candidate_id":true}',
                    '{"candidate_id":2.0}', 'I choose Frieren', '{"candidate_id":2,"extra":"guess"}'):
            self.assertIsNone(routing.validate_routing_response(raw, plan)[0])
        self.assertEqual(routing.validate_routing_response('{"candidate_id":2}', plan)[0], 'Frieren/Fern')
        empty = routing.routing_plan({'general': 'blue_hair 1girl dress'}, ['Frieren', 'Frieren/Fern'])
        self.assertFalse(empty['candidates'])

    def test_conflicting_series_and_multiple_characters_abstain_or_use_root(self):
        folders = ['Series A', 'Series A/Alice', 'Series A/Bob', 'Series B']
        plan = routing.routing_plan({'copyright': 'series_a series_b', 'character': 'alice'}, folders)
        self.assertFalse(plan['candidates'])
        plan = routing.routing_plan({'copyright': 'series_a', 'character': 'alice bob'}, folders)
        self.assertEqual(plan['direct'], 'Series A')
        self.assertEqual([c['folder'] for c in plan['candidates']], ['Series A'])

    def test_web_hints_cannot_override_verified_series(self):
        plan = routing.routing_plan({'copyright': 'unknown_series', '_web_context': 'Frieren and Fern'},
                                   ['Frieren', 'Frieren/Fern'])
        self.assertFalse(plan['candidates'])
        plan = routing.routing_plan({'copyright': 'frieren', 'character': 'fern', '_identity_source': 'vision'},
                                   ['Frieren', 'Frieren/Fern'])
        self.assertIsNone(plan['direct'])
        self.assertEqual(plan['evidence']['source'], 'vision')
        plan = routing.routing_plan({'copyright': 'other', '_identity_source': 'vision', '_web_context': 'Frieren'},
                                   ['Frieren', 'Other'])
        self.assertFalse(plan['candidates'])

    def test_real_local_api_model_list_and_candidate_request(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send_json({'object': 'list', 'data': [{'id': 'test-small', 'object': 'model', 'created': 0, 'owned_by': 'local'}]})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(payload)
                self.send_json({'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': payload['model'],
                    'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"candidate_id":2}'}}]})
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        worker.start()
        try:
            with providers.OpenAI(base_url=f'http://127.0.0.1:{server.server_port}/v1', api_key='test', timeout=3, max_retries=0) as client:
                self.assertEqual(routing.available_model_ids(client), ['test-small'])
                plan = routing.routing_plan({'copyright': 'some_series', 'character': 'hero'}, ['Series', 'Series/Hero'])
                result = sorter.BooruManagerApp.route_with_model(SimpleNamespace(log=lambda message, **kwargs: None), client, 'test-small', plan)
                self.assertEqual(result[0], 'Series/Hero')
            self.assertEqual(requests[0]['model'], 'test-small')
            self.assertEqual(requests[0]['response_format']['json_schema']['schema']['properties']['candidate_id']['enum'], [0, 1, 2])
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=2)

    def test_unicode_aliases_and_prompt_preserve_categories(self):
        plan = routing.routing_plan({'copyright': '葬送のフリーレン', 'character': 'フェルン', 'general': 'blue_hair'},
                                   ['Frieren', 'Frieren/Fern'], {'Frieren': {'series': ['葬送のフリーレン']},
                                   'Frieren/Fern': {'character': ['フェルン']}})
        self.assertEqual(plan['direct'], 'Frieren/Fern')
        prompt = routing.routing_messages(plan)
        data = __import__('json').loads(prompt[1]['content'])
        self.assertEqual(data['identity_evidence']['series'], ['葬送のフリーレン'])
        self.assertNotIn('blue_hair', prompt[1]['content'])
        self.assertEqual(data['candidates'][0]['id'], 0)

    def test_alias_validation_rejects_invalid_types(self):
        for aliases in ([], {'Frieren': ['alias']}, {'Frieren': {'series': 'alias'}}, {'Frieren': {'series': [None]}}):
            with self.assertRaises(ValueError):
                routing.validate_folder_aliases(aliases)

    def test_model_list_preserves_real_ids_and_removes_duplicates(self):
        client = SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[
            SimpleNamespace(id='model-b'), SimpleNamespace(id='model-a'), SimpleNamespace(id='model-b'), SimpleNamespace(id='')])))
        self.assertEqual(routing.available_model_ids(client), ['model-b', 'model-a'])

    def test_general_attributes_do_not_select_a_character_folder(self):
        plan = routing.routing_plan({'copyright': 'frieren', 'character': '', 'general': 'blue_hair'},
                                   ['Frieren', 'Frieren/Blue Hair'])
        self.assertEqual(plan['direct'], 'Frieren')
        self.assertEqual([c['folder'] for c in plan['candidates']], ['Frieren'])

    def test_unclassified_identity_tokens_are_visible_to_the_router(self):
        plan = routing.routing_plan({'general': 'frieren fern blue_hair', '_identity_source': 'embedded'},
                                   ['Frieren', 'Frieren/Fern'])
        self.assertEqual(set(plan['evidence']['uncategorized_identity_tokens']), {'frieren', 'fern'})
        self.assertIsNone(plan['direct'])

    def test_schema_fallback_is_limited_to_explicit_format_rejections(self):
        plan = routing.routing_plan({'copyright': 'frieren', 'character': 'fern'}, ['Frieren', 'Frieren/Fern'])
        class UnsupportedFormat(Exception):
            status_code = 400
        from unittest.mock import Mock
        create = Mock(side_effect=[UnsupportedFormat('response_format json_schema unsupported'),
                      SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"candidate_id":2}'))])])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        harness = SimpleNamespace(log=lambda message, **kwargs: None)
        result = sorter.BooruManagerApp.route_with_model(harness, client, 'small-model', plan)
        self.assertEqual(result[0], 'Frieren/Fern')
        self.assertIn('response_format', create.call_args_list[0].kwargs)
        self.assertNotIn('response_format', create.call_args_list[1].kwargs)
        create.reset_mock(side_effect=True)
        create.side_effect = TimeoutError('timeout')
        with self.assertRaises(TimeoutError):
            sorter.BooruManagerApp.route_with_model(harness, client, 'small-model', plan)
        self.assertEqual(create.call_count, 1)

    def test_series_scopes_nested_character(self):
        folders = ['Series A', 'Series A/Hero', 'Series B', 'Series B/Hero']
        tags = {'copyright': 'series_a', 'character': 'hero'}
        self.assertEqual(routing.exact_identity_folder(tags, folders), 'Series A/Hero')
        tags['character'] = 'unknown'
        self.assertEqual(routing.exact_identity_folder(tags, folders), 'Series A')
        tags['copyright'] = 'unknown'
        self.assertIsNone(routing.exact_identity_folder(tags, folders))

    def test_multiple_characters_fall_back_to_series(self):
        self.assertEqual(routing.exact_identity_folder(
            {'copyright': 'series', 'character': 'alice bob'},
            ['Series', 'Series/Alice', 'Series/Bob']), 'Series')

    def test_search_and_routing_cache_are_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = storage.PersistentResultCache(str(Path(folder) / 'cache.sqlite3'))
            cache.put('hash', 'search', 'resolved', {'tags': 'series'})
            cache.put('hash:route', 'folders-v1', 'resolved', {'folder': 'Old'})
            self.assertIsNone(cache.get('hash:route', 'folders-v2'))
            self.assertEqual(cache.get('hash', 'search')['tags'], 'series')


class ResizeLayoutTests(unittest.TestCase):
    class Scheduler:
        _schedule_wrap_update = sorter.BooruManagerApp._schedule_wrap_update
        _watch_wrap_width = sorter.BooruManagerApp._watch_wrap_width

        def __init__(self):
            self.jobs = {}
            self.serial = 0

        def after(self, delay, callback):
            self.serial += 1
            self.jobs[self.serial] = callback
            return self.serial

        def after_cancel(self, job):
            self.jobs.pop(job, None)

        def flush(self):
            callbacks = list(self.jobs.values())
            self.jobs.clear()
            with patch.object(sorter.time, 'monotonic', return_value=time.monotonic() + 1):
                for callback in callbacks:
                    callback()

    class Label:
        def __init__(self):
            self.width = 640
            self.updates = []

        def winfo_exists(self):
            return True

        def cget(self, name):
            return self.width

        def configure(self, *, wraplength):
            self.width = wraplength
            self.updates.append(wraplength)

    def test_resize_burst_reflows_once_using_latest_width(self):
        scheduler, label = self.Scheduler(), self.Label()
        for width in range(300, 1300):
            scheduler._schedule_wrap_update(label, width)
        self.assertEqual(len(scheduler.jobs), 1)
        self.assertEqual(label.updates, [])
        scheduler.flush()
        self.assertEqual(label.updates, [1296])
        scheduler._schedule_wrap_update(label, 1299)
        scheduler.flush()
        self.assertEqual(label.updates, [1296])

    def test_position_events_do_not_reflow_and_dpi_is_converted(self):
        scheduler, label = self.Scheduler(), self.Label()
        callbacks = {}
        container = SimpleNamespace(winfo_width=lambda: 1200,
                                    _reverse_widget_scaling=lambda width: width / 1.5,
                                    bind=lambda event, callback, add: callbacks.update({event: callback}))
        scheduler._watch_wrap_width(container, label, 24)
        for _ in range(100):
            callbacks['<Configure>'](None)
        self.assertEqual(len(scheduler.jobs), 1)
        scheduler.flush()
        self.assertEqual(label.updates, [776])
        callbacks['<Configure>'](None)
        self.assertFalse(scheduler.jobs)

    def test_destroy_cancels_pending_resize_work(self):
        scheduler, label = self.Scheduler(), self.Label()
        callbacks = {}
        container = SimpleNamespace(winfo_width=lambda: 1200,
                                    _reverse_widget_scaling=lambda width: width,
                                    bind=lambda event, callback, add: callbacks.update({event: callback}))
        scheduler._watch_wrap_width(container, label, 24)
        callbacks['<Configure>'](None)
        callbacks['<Destroy>'](None)
        self.assertFalse(scheduler.jobs)
        self.assertIsNone(label._wrap_resize_job)

    def test_wrapping_waits_until_resize_stops(self):
        scheduler, label = self.Scheduler(), self.Label()
        with patch.object(sorter.time, 'monotonic', return_value=100):
            scheduler._schedule_wrap_update(label, 900)
        callback = scheduler.jobs.pop(label._wrap_resize_job)
        with patch.object(sorter.time, 'monotonic', return_value=100.05):
            callback()
        self.assertEqual(label.updates, [])
        self.assertEqual(len(scheduler.jobs), 1)
        with patch.object(sorter.time, 'monotonic', return_value=100.1):
            scheduler._schedule_wrap_update(label, 1100)
        callback = scheduler.jobs.pop(label._wrap_resize_job)
        with patch.object(sorter.time, 'monotonic', return_value=100.3):
            callback()
        self.assertEqual(label.updates, [1096])

    def test_scroll_content_skips_intermediate_canvas_sizes(self):
        scheduler = self.Scheduler()
        sizes = {'width': 600}
        updates = []
        canvas = SimpleNamespace(winfo_exists=lambda: True, winfo_width=lambda: sizes['width'],
                                 itemconfigure=lambda item, **kwargs: updates.append(kwargs))
        pane = SimpleNamespace(_fit_job=None, _last_fitted_size=None, _orientation='vertical',
                               _parent_canvas=canvas, _create_window_id=1, after=scheduler.after)
        pane._apply_canvas_fit = lambda: sorter.SmoothScrollableFrame._apply_canvas_fit(pane)
        for width in range(600, 1600):
            sizes['width'] = width
            sorter.SmoothScrollableFrame._fit_frame_dimensions_to_canvas(pane)
        self.assertEqual(len(scheduler.jobs), 1)
        scheduler.flush()
        self.assertEqual(updates, [{'width': 1599}])
        sorter.SmoothScrollableFrame._fit_frame_dimensions_to_canvas(pane)
        scheduler.flush()
        self.assertEqual(updates, [{'width': 1599}])

    def test_root_dimension_handler_ignores_child_notifications(self):
        # No Tk window is needed to verify event routing into the parent handler.
        app = sorter.BooruManagerApp.__new__(sorter.BooruManagerApp)
        with patch.object(sorter.ctk.CTk, '_update_dimensions_event') as parent:
            for _ in range(1000):
                app._update_dimensions_event(SimpleNamespace(widget=object()))
            parent.assert_not_called()
            event = SimpleNamespace(widget=app)
            app._update_dimensions_event(event)
            parent.assert_called_once_with(event)


class NavigationPaintingTests(unittest.TestCase):
    def test_old_page_is_unmapped_before_preparing_the_next_page(self):
        events = []
        def page(name):
            return SimpleNamespace(grid=lambda **kwargs: events.append(('grid', name)),
                                   lift=lambda: events.append(('lift', name)),
                                   update_idletasks=lambda: events.append(('paint', name)),
                                   grid_forget=lambda: events.append(('hide', name)))
        navigation = sorter.AppNavigation.__new__(sorter.AppNavigation)
        old, new = page('History'), page('Settings')
        navigation.pages = {'History': old, 'Settings': new}
        navigation.buttons = {name: SimpleNamespace(configure=lambda **kwargs: None) for name in navigation.pages}
        navigation.selection = 'History'
        navigation.master = SimpleNamespace(_prepare_view=lambda view: events.append(('prepare', 'Settings')), _queue_native_repaint=lambda: None)
        navigation.set('Settings')
        self.assertEqual(events, [('hide', 'History'), ('grid', 'Settings'), ('prepare', 'Settings'),
                                  ('lift', 'Settings'), ('paint', 'Settings')])
        navigation.set('Settings')
        self.assertEqual(len(events), 5)


class DesktopWorkflowTests(unittest.TestCase):
    def test_nested_flow_expansion_drag_context_toggles_and_persistence(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            app = None
            try:
                with patch.object(sorter, '__file__', str(Path(folder) / 'ui.py')):
                    app = sorter.BooruManagerApp()
                errors = []; app.report_callback_exception = lambda *error: errors.append(error)
                app.tabview.set('Settings'); app.show_settings_section('Search'); app.update()
                parent = app.search_flow
                parent.selected = 'IQDB'; parent.expand_selected(); app.update()
                chart = app._database_flow_widgets['IQDB']
                self.assertEqual(len(chart._boxes), 6)
                first, last = chart._boxes[0], chart._boxes[5]
                x, y = int((first[0] + first[2]) / 2), int((first[1] + first[3]) / 2)
                target_x, target_y = int((last[0] + last[2]) / 2), int((last[1] + last[3]) / 2)
                chart.canvas.event_generate('<ButtonPress-1>', x=x, y=y)
                chart.canvas.event_generate('<B1-Motion>', x=target_x, y=target_y)
                chart.canvas.event_generate('<ButtonRelease-1>', x=target_x, y=target_y)
                app.update()
                self.assertEqual(app.database_pipelines['IQDB'][-1]['method'], 'danbooru')
                self.assertEqual(app.database_pipelines['SauceNAO'][0]['method'], 'danbooru')
                chart.toggle('pixiv')
                self.assertNotIn('pixiv', routing.enabled_flow_methods(app.database_pipelines['IQDB'], routing.DATABASE_METHODS))
                parent.selected = 'Local Vision LLM'; parent.expand_selected(); app.update()
                context = app._context_flow_widget
                self.assertIn('Parent step is off', app.flow_details_note.cget('text'))
                context.toggle('serpapi'); context.toggle('gvision'); context.reorder('gvision', 0)
                self.assertEqual(routing.enabled_flow_methods(app.context_pipeline, routing.CONTEXT_METHODS), ['gvision', 'serpapi'])
                self.assertTrue(app.switch_serpapi.get()); self.assertTrue(app.switch_gvision.get())
                self.assertNotIn('Local Vision LLM', routing.enabled_search_methods(parent.get_config()))
                self.assertTrue(app.save_settings())
                expected = json.loads(Path(app.config_file).read_text())
                context.toggle('serpapi'); chart.reorder('danbooru', 0)
                app.load_settings()
                self.assertEqual(app.database_pipelines, expected['database_pipelines'])
                self.assertEqual(app.context_pipeline, expected['context_pipeline'])
                self.assertEqual(chart.get_config(), expected['database_pipelines']['IQDB'])
                legacy = Path(folder) / 'legacy.json'
                legacy.write_text(json.dumps({'engine': 'IQDB', 'tags': 'Gelbooru', 'serpapi_enabled': 0, 'gvision_enabled': 1}))
                app.load_settings(str(legacy))
                self.assertEqual(app.database_pipelines['IQDB'][0]['method'], 'gelbooru')
                self.assertEqual(app.database_pipelines['SauceNAO'][0]['method'], 'gelbooru')
                self.assertEqual(routing.enabled_flow_methods(app.context_pipeline, routing.CONTEXT_METHODS), ['gvision'])
                parent.selected = 'Local Vision LLM'; parent.expand_selected(); app.update()
                self.assertFalse(app.flow_details_host.winfo_manager())
                self.assertFalse(errors, errors)
            finally:
                if app:
                    app.history_local_executor.shutdown(wait=True, cancel_futures=True)
                    app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
                    for timer in app.tk.call('after', 'info'):
                        app.tk.call('after', 'cancel', timer)
                    app.destroy()
                sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_actual_context_and_tag_fetch_respect_inner_flows(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            app = None
            try:
                with patch.object(sorter, '__file__', str(Path(folder) / 'ui.py')):
                    app = sorter.BooruManagerApp()
                app._on_context_flow_changed([{'method': 'gvision', 'enabled': True}, {'method': 'serpapi', 'enabled': True}])
                app.gvision_entries[0][1].insert(0, 'test-cloud-key'); app.serpapi_entries[0][1].insert(0, 'test-lens-key')
                calls = []
                with patch.object(providers, 'fetch_google_vision_web_detection', side_effect=lambda *args: calls.append('cloud') or ''), \
                     patch.object(providers, 'fetch_google_lens_context', side_effect=lambda *args: calls.append('lens') or 'context'):
                    self.assertEqual(app._fetch_vision_context('unused.png')[0], 'context')
                    self.assertEqual(calls, ['cloud', 'lens'])
                calls.clear()
                with patch.object(providers, 'fetch_google_vision_web_detection', side_effect=lambda *args: calls.append('cloud') or 'context'), \
                     patch.object(providers, 'fetch_google_lens_context', side_effect=lambda *args: calls.append('lens') or 'context'):
                    self.assertEqual(app._fetch_vision_context('unused.png')[0], 'context')
                    self.assertEqual(calls, ['cloud'])
                app._run_database_pipelines = {
                    'IQDB': routing.normalize_named_flow([{'method': 'yandere', 'enabled': True}, {'method': 'danbooru', 'enabled': True}], routing.DATABASE_METHODS),
                    'SauceNAO': routing.normalize_named_flow([{'method': 'danbooru', 'enabled': True}], routing.DATABASE_METHODS)}
                ids = {'yandere': '10', 'danbooru': '20', 'pixiv': '30'}
                with patch.object(app, 'safe_get_request', return_value=None) as get, \
                     patch.object(providers.tracker.yandere_limiter, 'record', return_value=1), \
                     patch.object(providers.tracker.danbooru_limiter, 'record', return_value=1):
                    app.fetch_tags('unused.png', {'engine': 'IQDB', 'booru_ids': ids}, 'Auto')
                    urls = [call.args[0] for call in get.call_args_list]
                    self.assertEqual(len(urls), 2)
                    self.assertIn('yande.re', urls[0]); self.assertIn('danbooru', urls[1])
                    get.reset_mock()
                    app.fetch_tags('unused.png', {'engine': 'SauceNAO (#1)', 'booru_ids': ids}, 'Auto')
                    self.assertEqual(get.call_count, 1); self.assertIn('danbooru', get.call_args.args[0])
            finally:
                if app:
                    app.history_local_executor.shutdown(wait=True, cancel_futures=True)
                    app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
                    for timer in app.tk.call('after', 'info'):
                        app.tk.call('after', 'cancel', timer)
                    app.destroy()
                sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_flow_drag_toggle_save_and_actual_execution_order(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            app = None
            try:
                with patch.object(sorter, '__file__', str(Path(folder) / 'ui.py')):
                    app = sorter.BooruManagerApp()
                errors = []; app.report_callback_exception = lambda *error: errors.append(error)
                app.tabview.set('Settings'); app.show_settings_section('Search'); app.update()
                chart = app.search_flow
                def drag(method, destination):
                    app.update()
                    index = next(i for i, step in enumerate(chart.steps) if step['method'] == method)
                    x1, y1, x2, y2 = chart._boxes[index]
                    target = chart._boxes[destination]
                    x, y = int((x1 + x2) / 2), int((y1 + y2) / 2)
                    target_x = int((target[0] + target[2]) / 2)
                    chart.canvas.event_generate('<ButtonPress-1>', x=x, y=y)
                    chart.canvas.event_generate('<B1-Motion>', x=target_x, y=y)
                    chart.canvas.event_generate('<ButtonRelease-1>', x=target_x, y=y)
                    app.update()
                drag('SauceNAO', 0)
                self.assertEqual(routing.enabled_search_methods(chart.get_config()), ['SauceNAO', 'IQDB'])
                x1, y1, x2, y2 = chart._boxes[2]
                chart.canvas.event_generate('<ButtonPress-1>', x=int(x2 - 16), y=int(y1 + 16))
                app.update()
                drag('Local Vision LLM', 0)
                self.assertEqual(routing.enabled_search_methods(chart.get_config()), ['Local Vision LLM', 'SauceNAO', 'IQDB'])
                chart._keyboard_move(1)
                configured = chart.get_config()
                self.assertEqual(routing.enabled_search_methods(configured), ['SauceNAO', 'Local Vision LLM', 'IQDB'])
                self.assertTrue(app.save_settings())
                chart.set_config(routing.legacy_search_pipeline('IQDB'), notify=True)
                app.load_settings()
                self.assertEqual(chart.get_config(), configured)
                app.switch_serpapi.select(); app.switch_gvision.select(); app.update_search_options()
                self.assertIn('Google Lens / SerpApi → Cloud Vision', chart.context_label.cget('text'))
                app.switch_use_embedded_tags.select(); app.on_use_embedded_tags_toggle()
                self.assertIn('embedded tags', chart.preflight_label.cget('text'))
                app.switch_use_embedded_tags.deselect(); app.on_use_embedded_tags_toggle()
                source, library = Path(folder) / 'source', Path(folder) / 'library'
                source.mkdir(); (library / 'Series').mkdir(parents=True)
                Image.new('RGB', (8, 8), 'red').save(source / 'flow.png')
                app.target_source, app.dest_path = str(source), str(library)
                calls, started = [], threading.Event()
                def sauce(*args, **kwargs):
                    calls.append(('SauceNAO', kwargs['is_fallback'])); started.set(); time.sleep(.2); return []
                def vision(*args):
                    calls.append(('Local Vision LLM', None)); return []
                def iqdb(*args, **kwargs):
                    calls.append(('IQDB', kwargs['is_fallback']))
                    return [{'engine': 'IQDB', 'title': 'match', 'booru_ids': {'danbooru': 1}}]
                app.search_saucenao_engine, app.search_local_vision_engine, app.search_iqdb_engine = sauce, vision, iqdb
                app.fetch_tags = lambda *args: {'combined': 'series', 'copyright': 'series'}
                client = SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id='test')])), close=lambda: None)
                with patch.object(providers, 'OpenAI', return_value=client):
                    app.start_thread()
                    deadline, changed = time.monotonic() + 8, False
                    while app.is_processing and time.monotonic() < deadline:
                        app.update(); time.sleep(.005)
                        if started.is_set() and not changed:
                            chart.reorder('IQDB', 0); changed = True
                    self.assertFalse(app.is_processing)
                    self.assertEqual(calls, [('SauceNAO', False), ('Local Vision LLM', None), ('IQDB', True)])
                    self.assertTrue((library / 'Series' / 'flow.png').exists())
                    # The changed UI order applies to the next batch, which stops at its first success.
                    Image.new('RGB', (9, 8), 'blue').save(source / 'next.png')
                    calls.clear(); app.start_thread(); deadline = time.monotonic() + 8
                    while app.is_processing and time.monotonic() < deadline:
                        app.update(); time.sleep(.005)
                    self.assertFalse(app.is_processing)
                    self.assertEqual(calls, [('IQDB', False)])
                self.assertFalse(errors, errors)
            finally:
                if app:
                    app.history_local_executor.shutdown(wait=True, cancel_futures=True)
                    app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
                    for timer in app.tk.call('after', 'info'):
                        app.tk.call('after', 'cancel', timer)
                    app.destroy()
                sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_serpapi_outages_do_not_poison_negative_image_cache(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            app = None
            try:
                with patch.object(sorter, '__file__', str(Path(folder) / 'ui.py')):
                    app = sorter.BooruManagerApp()
                source = Path(folder) / 'source'; source.mkdir()
                Image.new('RGB', (8, 8), 'red').save(source / 'retry.png')
                app.target_source, app.dest_path = str(source), str(Path(folder) / 'library')
                app.opt_engine.set('Local Vision LLM'); app.switch_serpapi.select()
                state, calls = {'outcome': 'unavailable'}, []
                def vision(*args):
                    calls.append(1); providers.tracker.serpapi_last_outcome = state['outcome']; return []
                app.search_local_vision_engine = vision
                client = SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id='test')])), close=lambda: None)
                def run():
                    app.start_thread(); deadline = time.monotonic() + 5
                    while app.is_processing and time.monotonic() < deadline:
                        app.update(); time.sleep(.005)
                    self.assertFalse(app.is_processing)
                with patch.object(providers, 'OpenAI', return_value=client):
                    run(); run(); self.assertEqual(len(calls), 2)
                    state['outcome'] = 'no_match'
                    run(); run(); self.assertEqual(len(calls), 3)
                    connection = sqlite3.connect(app.result_cache.path)
                    with connection:
                        connection.execute("UPDATE image_results SET payload = '{}' WHERE status = 'miss'")
                    connection.close()
                    run(); self.assertEqual(len(calls), 4)  # Legacy failure is retried under the fixed policy.
                self.assertTrue((source / 'retry.png').exists())
            finally:
                if app:
                    app.history_local_executor.shutdown(wait=True, cancel_futures=True)
                    app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
                    for timer in app.tk.call('after', 'info'):
                        app.tk.call('after', 'cancel', timer)
                    app.destroy()
                sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_history_reuses_rows_and_local_thumbnail_decodes_across_pages(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            app = None
            try:
                with patch.object(sorter, '__file__', str(Path(folder) / 'ui.py')):
                    app = sorter.BooruManagerApp()
                errors = []; app.report_callback_exception = lambda *error: errors.append(error)
                files = []
                for index in range(12):
                    path = Path(folder) / f'{index}.png'; Image.new('RGB', (20, 20), (index, 0, 0)).save(path); files.append(path)
                for index in range(2000):
                    app.history_items.append({'id': str(index), 'filename': f'{index}.png', 'original_path': str(Path(folder) / 'incoming' / str(index)),
                        'current_path': str(files[index % 12]), 'is_unmatched': False, 'match_url': f'https://example.com/{index}', 'match_thumb_url': '', 'reason': 'test'})
                app.switch_history_attempts.select()  # Exercise the full audit with 2,000 attempts.
                app.refresh_history_view()
                self.assertFalse(app._history_cards)  # Hidden history does no widget work.
                def settle():
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        app.update(); time.sleep(.005)
                        if app._history_page_signature is not None and all(
                            getattr(entry.get('local_thumbnail'), 'image', None) is not None for entry in app._history_visible_entries):
                            return
                    self.fail('history page did not finish rendering')
                with patch.object(sorter.Image, 'open', wraps=Image.open) as decode:
                    app.tabview.set('History'); settle()
                    rows = list(app._history_cards)
                    self.assertEqual(len(rows), app.history_per_page)
                    app.change_history_page(1); settle()
                    self.assertEqual(app._history_cards, rows)
                    entry = app.history_items[6]
                    with patch.object(app, 'open_file_external') as local, patch.object(history.webbrowser, 'open') as web:
                        entry['local_thumbnail']._label.event_generate('<Button-1>'); entry['btn_open_match'].invoke()
                        local.assert_called_once_with(entry['current_path']); web.assert_called_once_with('https://example.com/6')
                    decoded = decode.call_count
                    app.change_history_page(-1); settle()
                    self.assertEqual(decode.call_count, decoded)
                    with patch.object(app, 'render_history_card', wraps=app.render_history_card) as render:
                        app.refresh_history_view()
                        render.assert_not_called()
                    self.assertEqual(app._history_cards, rows)
                    self.assertFalse(errors, errors)
            finally:
                if app:
                    app.history_local_executor.shutdown(wait=True, cancel_futures=True)
                    app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
                    for timer in app.tk.call('after', 'info'):
                        app.tk.call('after', 'cancel', timer)
                    app.destroy()
                sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_server_model_refresh_selection_persistence_and_worker_model(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            app = None
            try:
                with patch.object(sorter, '__file__', str(Path(folder) / 'ui.py')):
                    app = sorter.BooruManagerApp()
                errors = []
                app.report_callback_exception = lambda *error: errors.append(error)
                def pump_until(predicate):
                    deadline = time.monotonic() + 8
                    while not predicate() and time.monotonic() < deadline:
                        app.update(); time.sleep(.005)
                    self.assertTrue(predicate())
                with patch.object(providers, 'OpenAI') as factory:
                    client = factory.return_value
                    client.__enter__.return_value = client
                    client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id='large-model'), SimpleNamespace(id='small-model')])
                    app.refresh_local_models()
                    pump_until(lambda: app.btn_test_model.cget('state') == 'normal')
                    self.assertIn('small-model', app.opt_model.cget('values'))
                    app.opt_model.set('small-model')
                    self.assertTrue(app.save_settings())
                    app.opt_model.set(routing.AUTO_MODEL)
                    app.load_settings()
                    self.assertEqual(app.opt_model.get(), 'small-model')
                    source, library = Path(folder) / 'source', Path(folder) / 'library'
                    source.mkdir(); (library / 'Series' / 'Hero').mkdir(parents=True)
                    Image.new('RGB', (8, 8), 'red').save(source / 'route.png')
                    app.target_source, app.dest_path = str(source), str(library)
                    app.opt_engine.set('IQDB'); app.switch_engine_fallback.deselect(); app.switch_vision_fallback.deselect()
                    app.switch_scan_subfolders.select()
                    app.search_iqdb_engine = lambda *args, **kwargs: [{'engine': 'IQDB', 'title': 'test', 'booru_ids': {'danbooru': 1}}]
                    app.fetch_tags = lambda *args: {'combined': 'some_series hero', 'copyright': 'some_series', 'character': 'hero'}
                    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"candidate_id":2}'))])
                    app.start_thread(); pump_until(lambda: not app.is_processing)
                    self.assertTrue((library / 'Series' / 'Hero' / 'route.png').exists())
                    self.assertEqual(client.chat.completions.create.call_args.kwargs['model'], 'small-model')
                    records = [json.loads(line) for line in (Path(folder) / 'routing_decisions.jsonl').read_text(encoding='utf-8').splitlines()]
                    self.assertEqual(records[-1]['model'], 'small-model')
                    self.assertEqual(records[-1]['destination'], 'Series/Hero')
                    client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id='large-model')])
                    app.refresh_local_models(); pump_until(lambda: app.btn_test_model.cget('state') == 'normal')
                    self.assertEqual(app.opt_model.get(), 'small-model')
                    self.assertIn('unavailable', app.lbl_model_status.cget('text'))
                    Image.new('RGB', (9, 9), 'blue').save(source / 'leave.png')
                    app.start_thread(); pump_until(lambda: not app.is_processing)
                    self.assertTrue((source / 'leave.png').exists())
                    self.assertEqual(client.chat.completions.create.call_count, 1)
                self.assertFalse(errors, errors)
            finally:
                if app:
                    app.history_local_executor.shutdown(wait=True, cancel_futures=True)
                    app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
                    for timer in app.tk.call('after', 'info'):
                        app.tk.call('after', 'cancel', timer)
                    app.destroy()
                sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_settings_navigation_and_history_comparison_actions(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            app = None
            try:
                with patch.object(sorter, '__file__', str(Path(folder) / 'ui.py')):
                    app = sorter.BooruManagerApp()
                errors = []
                app.report_callback_exception = lambda *error: errors.append(error)
                app.tabview.set('Settings')
                self.assertIsInstance(app.tabview, sorter.AppNavigation)
                self.assertEqual(app.tabview.get(), 'Settings')
                self.assertEqual(app.tabview.buttons['Settings'].cget('fg_color'), ('#edf0f5', '#2b323c'))
                self.assertEqual(app.tabview.brand.cget('text'), 'Tag-AI')
                self.assertIsInstance(app.switch_engine_fallback, sorter.ctk.CTkCheckBox)
                self.assertEqual(app.opt_engine.cget('fg_color'), ('#ffffff', '#20242b'))
                self.assertEqual(app.opt_engine.cget('border_width'), 1)
                self.assertEqual(app.opt_engine.get(), 'IQDB')
                self.assertEqual(app.opt_exec_mode.get(), 'Automated Full Folder Batch')
                for name in ('Search', 'Local model', 'Folder routing', 'Image tags'):
                    app.show_settings_section(name)
                    app.update()
                    self.assertTrue(app.settings_panes[name].grid_info())
                    self.assertTrue(all(not pane.grid_info() for title, pane in app.settings_panes.items() if title != name))
                app.switch_serpapi.select(); app.update_search_options()
                self.assertEqual(app.serpapi_options.winfo_manager(), 'pack')
                app.switch_serpapi.deselect(); app.update_search_options()
                self.assertFalse(app.serpapi_options.winfo_manager())
                app.switch_embed_metadata.select(); app.on_embed_metadata_toggle()
                self.assertEqual(app.frame_tag_categories.winfo_manager(), 'pack')
                app.switch_embed_metadata.deselect(); app.on_embed_metadata_toggle()
                self.assertFalse(app.frame_tag_categories.winfo_manager())
                self.assertFalse(app.activity_frame.winfo_manager())
                app.toggle_activity()
                self.assertEqual(app.activity_frame.winfo_manager(), 'panedwindow')
                app.toggle_activity()
                path = Path(folder) / 'original.png'
                Image.new('RGB', (12, 15), 'red').save(path)
                app._download_history_thumbnail = lambda *args: Image.new('RGB', (12, 15), 'blue')
                app.is_processing = True  # Queue entries without immediately navigating history.
                app.add_history_item(str(Path(folder) / 'incoming.png'), str(path), path.name,
                                     match_thumb_url='https://example.com/thumb.png', match_url='https://example.com/post/123')
                uncertain_path = Path(folder) / 'uncertain.png'
                Image.new('RGB', (12, 15), 'green').save(uncertain_path)
                app.add_history_item(str(uncertain_path), str(uncertain_path), 'uncertain.png', True)
                app.update(); app.is_processing = False
                app.tabview.set('History'); app.refresh_history_view(); app.update()
                matched, uncertain = app.history_items
                self.assertTrue(matched['local_thumbnail'].winfo_exists())
                self.assertTrue(matched['match_thumbnail'].winfo_exists())
                with patch.object(app, 'open_file_external') as local, patch.object(app, 'open_folder_selected') as folder_open, patch.object(history.webbrowser, 'open') as browser:
                    matched['local_thumbnail']._label.event_generate('<Button-1>')
                    matched['btn_show_folder'].invoke()
                    matched['btn_open_match'].invoke()
                    local.assert_called_once_with(str(path))
                    folder_open.assert_called_once_with(str(path))
                    browser.assert_called_once_with('https://example.com/post/123')
                    local.reset_mock(); browser.reset_mock()
                    matched['local_thumbnail']._label.event_generate('<Button-1>')
                    matched['match_thumbnail']._label.event_generate('<Button-1>')
                    app.update()
                    local.assert_called_once_with(str(path))
                    browser.assert_called_once_with('https://example.com/post/123')
                self.assertEqual(uncertain['btn_open_match'].cget('state'), 'disabled')
                app.history_filter.set('Needs attention'); app.on_history_filter()
                self.assertEqual(app.get_filtered_history_items(), [uncertain])
                app.history_filter.set('Moved'); app.on_history_filter()
                self.assertEqual(app.get_filtered_history_items(), [matched])
                for _ in range(3):
                    app.tabview.set('Settings')
                    app.show_settings_section('Search')
                    self.assertIsNone(app.settings_panes['Search']._fit_job)
                    self.assertTrue(app.tab_settings.grid_info())
                    self.assertFalse(app.tab_history.grid_info())
                    app.tabview.set('History')
                    self.assertIsNone(app.history_scroll._fit_job)
                    self.assertFalse(app.tab_settings.grid_info())
                self.assertIs(app._ui_icon('search'), app._ui_icon('search'))
                app.save_settings_action()
                app.load_settings()
                self.assertFalse(errors, errors)
            finally:
                if app:
                    app.history_local_executor.shutdown(wait=True, cancel_futures=True)
                    app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
                    for timer in app.tk.call('after', 'info'):
                        app.tk.call('after', 'cancel', timer)
                    app.destroy()
                sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_batch_keeps_uncertain_files_and_reroutes_cached_tags(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            app = None
            try:
                with patch.object(sorter, '__file__', str(Path(folder) / 'ui.py')):
                    app = sorter.BooruManagerApp()
                source = Path(folder) / 'source'; source.mkdir()
                library = Path(folder) / 'library'; (library / 'Series').mkdir(parents=True)
                for index in range(80):
                    Image.new('RGB', (5, 5), (index, 0, 0)).save(source / f'{index}.png')
                (source / 'broken.png').write_bytes(b'unreadable test fixture')
                app.target_source, app.dest_path = str(source), str(library)
                app.switch_engine_fallback.deselect(); app.switch_vision_fallback.deselect()
                app.opt_engine.set('IQDB')
                calls = []
                def identify(*args, **kwargs):
                    calls.append(1)
                    return [{'engine': 'IQDB', 'title': 'test', 'similarity': 99, 'booru_ids': {'danbooru': 1}}]
                app.search_iqdb_engine = identify
                app.fetch_tags = lambda *args: {'combined': 'series hero', 'copyright': 'series', 'character': 'hero'}
                client = SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id='test')])), close=lambda: None)
                def run_batch():
                    app.start_thread()
                    deadline = time.monotonic() + 20
                    while app.is_processing and time.monotonic() < deadline:
                        app.update(); time.sleep(0.005)
                    self.assertFalse(app.is_processing)
                def read_hash(path):
                    if os.path.basename(path) == 'broken.png':
                        raise OSError('Simulated per-file read failure')
                    return storage.stream_file_md5(path)
                with patch.object(providers, 'OpenAI', return_value=client), \
                     patch.object(workflow, 'stream_file_md5', side_effect=read_hash):
                    run_batch()
                    self.assertEqual(len(list((library / 'Series').glob('*.png'))), 80)
                    self.assertTrue((source / 'broken.png').exists())
                    self.assertTrue(any('File error:' in e['reason'] for e in app.history_items))
                    # A new nested folder should reroute cached identification without an API call.
                    (library / 'Series' / 'Hero').mkdir()
                    storage.safe_move(str(library / 'Series' / '0.png'), str(source / '0.png'))
                    old_calls = len(calls)
                    app.switch_scan_subfolders.select()
                    run_batch()
                    self.assertEqual(len(calls), old_calls)
                    self.assertTrue((library / 'Series' / 'Hero' / '0.png').exists())
                    # LLM-selected nested paths must survive validation unchanged.
                    Image.new('RGB', (7, 7), 'green').save(source / 'llm.png')
                    app.fetch_tags = lambda *args: {'combined': 'some_series hero', 'copyright': 'some_series', 'character': 'hero'}
                    client.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs:
                        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"candidate_id":2}'))])))
                    run_batch()
                    self.assertTrue((library / 'Series' / 'Hero' / 'llm.png').exists())
                    # No match stays in place and is visible in history, with no review prompt.
                    Image.new('RGB', (6, 6), 'blue').save(source / 'unknown.png')
                    app.search_iqdb_engine = lambda *args, **kwargs: []
                    run_batch()
                    self.assertTrue((source / 'unknown.png').exists())
                    self.assertTrue(any(e['filename'] == 'unknown.png' and e['is_unmatched'] for e in app.history_items))
            finally:
                if app:
                    app.history_local_executor.shutdown(wait=True, cancel_futures=True)
                    app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
                    for timer in app.tk.call("after", "info"):
                        app.tk.call("after", "cancel", timer)
                    app.destroy()
                sys.stdout, sys.stderr = original_stdout, original_stderr

    def test_slow_request_does_not_block_ui_or_allow_overlapping_cancel(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            app = None
            try:
                with patch.object(sorter, '__file__', str(Path(folder) / 'ui.py')):
                    app = sorter.BooruManagerApp()
                source = Path(folder) / 'source'; source.mkdir()
                Image.new('RGB', (10, 10), 'red').save(source / 'image.png')
                app.target_source = str(source)
                app.dest_path = str(Path(folder) / 'library')
                app.switch_engine_fallback.deselect()
                app.switch_vision_fallback.deselect()
                app.opt_engine.set('IQDB')
                entered = threading.Event()
                release = threading.Event()
                def slow_search(*args, **kwargs):
                    entered.set(); release.wait(6); return []
                app.search_iqdb_engine = slow_search
                client = SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id='test')])) )
                beats = []
                def heartbeat():
                    beats.append(time.monotonic())
                    if app.is_processing:
                        app.after(10, heartbeat)
                with patch.object(providers, 'OpenAI', return_value=client):
                    app.start_thread()
                    app.after(10, heartbeat)
                    deadline = time.monotonic() + 8
                    cancelled = False
                    while app.is_processing and time.monotonic() < deadline:
                        app.update()
                        if entered.is_set() and len(beats) >= 6 and not cancelled:
                            app.cancel_processing(); app.cancel_processing()
                            self.assertTrue(app.is_processing)
                            self.assertTrue(app.stop_requested)
                            cancelled = True
                            release.set()
                        time.sleep(0.005)
                self.assertFalse(app.is_processing)
                self.assertGreater(len(beats), 5)
                self.assertTrue((source / 'image.png').exists())
                self.assertEqual(app.btn_start.cget('state'), 'normal')
                app.add_history_item(str(source / 'image.png'), str(source / 'image.png'), 'image.png', True)
                app.update()
                self.assertEqual(len(app.history_items), 1)
                app.history_items.clear(); app._load_history()
                self.assertEqual(len(app.history_items), 1)
            finally:
                if app:
                    app.history_local_executor.shutdown(wait=True, cancel_futures=True)
                    app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
                    for timer in app.tk.call("after", "info"):
                        app.tk.call("after", "cancel", timer)
                    app.destroy()
                sys.stdout, sys.stderr = original_stdout, original_stderr


if __name__ == '__main__':
    unittest.main()


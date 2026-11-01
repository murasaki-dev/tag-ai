"""Stopped batches retain completed work, including files left at source."""
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers
import storage
import ui
import routing
from PIL import Image


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_reopens_advances_and_closes_its_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.sqlite3'
            files = [str(Path(directory) / f'{index}.png') for index in range(2000)]
            store = storage.BatchSessionStore(str(path))
            store.begin(directory, directory, 'Automated Full Folder Batch', files)
            store.advance(1730)
            restored = storage.BatchSessionStore(str(path)).load()
            self.assertEqual(restored['files'], files)
            self.assertEqual(restored['completed'], 1730)
            store.advance(2000)
            self.assertIsNone(store.load())
            store.clear()
            self.assertIsNone(store.load())
            path.unlink()  # No leaked SQLite handles on Windows.

    def test_invalid_checkpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.sqlite3'
            store = storage.BatchSessionStore(str(path))
            with sqlite3.connect(path) as connection:
                connection.execute('INSERT INTO batch_session VALUES (1, ?, 0)', (json.dumps({'version': 999}),))
            connection.close()
            with self.assertRaises(ValueError):
                store.load()


class SessionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stdout, self.stderr = sys.stdout, sys.stderr
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        self.library = self.root / 'library'; self.library.mkdir()
        for index, color in enumerate(('red', 'blue', 'green')):
            Image.new('RGB', (8, 8), color).save(self.source / f'{index}.png')
        self.app = None
        self.release = threading.Event()
        self.calls = []
        client = SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id='test')])), close=lambda: None)
        self.factory = patch.object(providers, 'OpenAI', return_value=client)
        self.factory.start()
        self._open()

    def _open(self):
        with patch.object(ui, '__file__', str(self.root / 'ui.py')):
            self.app = ui.BooruManagerApp()
        self.errors = []
        self.app.report_callback_exception = lambda *error: self.errors.append(error)
        self.app.target_source, self.app.dest_path = str(self.source), str(self.library)
        self.app.opt_engine.set('IQDB')
        self.app.switch_engine_fallback.deselect(); self.app.switch_vision_fallback.deselect()

    def _pump(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            self.app.update(); time.sleep(.005)
        self.assertTrue(predicate(), 'Timed out waiting for batch state')

    def _close(self):
        if not self.app:
            return
        self.release.set()
        self.app.cancel_processing()
        self._pump(lambda: not self.app.is_processing)
        self.app.history_local_executor.shutdown(wait=True, cancel_futures=True)
        self.app.history_thumb_executor.shutdown(wait=True, cancel_futures=True)
        self.app.result_cache.close()
        for timer in self.app.tk.call('after', 'info'):
            self.app.tk.call('after', 'cancel', timer)
        self.app.destroy(); self.app = None
        sys.stdout, sys.stderr = self.stdout, self.stderr

    def tearDown(self):
        try:
            self._close()
        finally:
            self.factory.stop()
            sys.stdout, sys.stderr = self.stdout, self.stderr
            self.temp.cleanup()

    def _stop_on_second_request(self):
        entered = threading.Event()
        def search(path, **kwargs):
            self.calls.append(Path(path).name)
            if Path(path).name == '1.png':
                entered.set(); self.release.wait(6)
            return []
        self.app.search_iqdb_engine = search
        self.app.start_thread()
        self._pump(entered.is_set)
        self.app.cancel_processing()
        self.assertTrue(self.app.is_processing)
        self.assertFalse(self.app.session_controls.winfo_manager())
        self.release.set()
        self._pump(lambda: not self.app.is_processing)
        self.assertEqual(self.app.pending_session['completed'], 1)
        self.assertEqual(self.app.btn_resume.cget('state'), 'normal')
        self.assertTrue(self.app.session_controls.winfo_manager())

    def test_resume_retries_interrupted_file_and_skips_completed_unmatched_after_reopen(self):
        self._stop_on_second_request()
        self.assertEqual(self.calls, ['0.png', '1.png'])
        self._close(); self._open()
        self.assertEqual(self.app.pending_session['completed'], 1)
        self.assertTrue(self.app.session_controls.winfo_manager())
        # A new source file belongs to the next batch, not the saved queue.
        Image.new('RGB', (8, 8), 'purple').save(self.source / 'new.png')
        self.app.search_iqdb_engine = lambda path, **kwargs: self.calls.append(Path(path).name) or []
        self.app.btn_resume.invoke()
        self._pump(lambda: not self.app.is_processing)
        self.assertEqual(self.calls, ['0.png', '1.png', '1.png', '2.png'])
        self.assertIsNone(self.app.pending_session)
        self.assertIsNone(self.app.session_store.load())
        self.assertFalse(self.app.session_controls.winfo_manager())
        self.assertTrue(all((self.source / f'{index}.png').exists() for index in range(3)))
        self.assertFalse(self.errors, self.errors)

    def test_start_over_rescans_source_keeps_moves_and_revisits_unmatched(self):
        self._stop_on_second_request()
        # Simulate a completed move and add a newly arrived file before restart.
        storage.safe_move(str(self.source / '2.png'), str(self.library / '2.png'))
        Image.new('RGB', (8, 8), 'purple').save(self.source / 'new.png')
        self.app.result_cache.clear()
        self.calls.clear()
        self.app.search_iqdb_engine = lambda path, **kwargs: self.calls.append(Path(path).name) or []
        self.app.btn_restart.invoke()
        self._pump(lambda: not self.app.is_processing)
        self.assertEqual(self.calls, ['0.png', '1.png', 'new.png'])
        self.assertTrue((self.library / '2.png').exists())
        self.assertIsNone(self.app.pending_session)
        self.assertFalse(self.errors, self.errors)

    def test_resume_skips_missing_files_and_keeps_queue_after_model_failure(self):
        self._stop_on_second_request()
        (self.source / '1.png').unlink()
        with patch.object(providers, 'OpenAI', side_effect=RuntimeError('Server offline')):
            self.app.resume_session()
            self._pump(lambda: not self.app.is_processing)
        self.assertEqual(self.app.pending_session['completed'], 1)
        self.app.search_iqdb_engine = lambda path, **kwargs: self.calls.append(Path(path).name) or []
        self.app.resume_session()
        self._pump(lambda: not self.app.is_processing)
        self.assertEqual(self.calls, ['0.png', '1.png', '2.png'])
        self.assertIsNone(self.app.pending_session)
        self.assertFalse(self.errors, self.errors)

    def test_history_follows_sort_manual_move_and_undo_with_previews(self):
        source = self.source / '0.png'
        self.app.add_history_item(str(source), str(source), source.name, True)
        self._pump(lambda: len(self.app.history_items) == 1)
        older = self.app.history_items[0]
        target = storage.safe_move(str(source), str(self.library / '0.png'))
        self.app.add_history_item(str(source), target, source.name)
        self._pump(lambda: len(self.app.history_items) == 2)
        latest = self.app.history_items[1]
        self.assertEqual(older['current_path'], target)
        self.app.tabview.set('History')
        self._pump(lambda: getattr(latest.get('local_thumbnail'), 'image', None) is not None)
        self.app.move_to_unmatched_action(latest, latest['card'])
        self.assertEqual(older['current_path'], latest['current_path'])
        self.assertTrue(Path(older['current_path']).is_file())
        self._pump(lambda: getattr(latest.get('local_thumbnail'), 'image', None) is not None)
        self.app.undo_move(latest, latest['card'])
        self.assertEqual(older['current_path'], str(source))
        self.assertTrue(source.is_file())
        self.assertNotIn(latest, self.app.history_items)
        self._pump(lambda: getattr(older.get('local_thumbnail'), 'image', None) is not None)
        self.assertFalse(self.errors, self.errors)

    def test_legacy_history_repairs_recorded_move_chain_after_reopen(self):
        source = str(self.source / '0.png')
        intermediate = str(self.library / 'old.png')
        final = str(self.library / 'final.png')
        storage.safe_move(source, final)
        for index, (old, current) in enumerate(((source, source), (source, intermediate), (intermediate, final))):
            self.app._persist_history({'id': str(index), 'original_path': old, 'current_path': current,
                                      'filename': '0.png', 'is_unmatched': index == 0})
        self._close(); self._open()
        self.assertEqual([e['current_path'] for e in self.app.history_items], [final] * 3)
        self.app.tabview.set('History')
        self._pump(lambda: self.app._history_page_signature is not None and bool(self.app._history_visible_entries) and
                   all(getattr(e.get('local_thumbnail'), 'image', None) is not None for e in self.app._history_visible_entries))
        self.assertEqual(len(self.app._history_visible_entries), 1)
        self.assertFalse(self.errors, self.errors)

    def test_unmatched_selection_persists_and_flow_shows_actual_outcomes(self):
        self.app.tabview.set('Settings'); self.app.show_settings_section('Folder routing'); self.app.update()
        self.assertTrue(self.app.opt_unmatched.winfo_manager())
        self.assertEqual(self.app.opt_unmatched.get(), routing.UNMATCHED_ACTIONS[0])
        self.app.opt_unmatched.set(routing.UNMATCHED_ACTIONS[1]); self.app.on_unmatched_change(routing.UNMATCHED_ACTIONS[1])
        self.assertTrue(self.app.save_settings())
        self.app.opt_unmatched.set(routing.UNMATCHED_ACTIONS[0]); self.app.load_settings()
        self.assertEqual(self.app.opt_unmatched.get(), routing.UNMATCHED_ACTIONS[1])
        self.app.show_settings_section('Search'); self.app.update()
        chart = self.app.search_flow
        chart._draw_flow()
        text = '\n'.join(chart.canvas.itemcget(item, 'text') for item in chart.canvas.find_all() if chart.canvas.type(item) == 'text')
        self.assertIn('destination library', text)
        self.assertIn('Searches exhausted / no supported folder', text)
        self.assertIn('Move to Unmatched folder', text)
        self.app.opt_unmatched.set(routing.UNMATCHED_ACTIONS[0]); self.app.on_unmatched_change(routing.UNMATCHED_ACTIONS[0]); chart._draw_flow()
        text = '\n'.join(chart.canvas.itemcget(item, 'text') for item in chart.canvas.find_all() if chart.canvas.type(item) == 'text')
        self.assertIn('Leave in original source folder', text)
        self.assertFalse(self.errors, self.errors)

    def test_unmatched_search_and_folder_failures_use_frozen_destination_without_overwrite(self):
        custom = self.root / 'custom-unmatched'; custom.mkdir()
        (custom / '0.png').write_bytes(b'keep existing file')
        self.app.unmatched_path = str(custom)
        self.app.opt_unmatched.set(routing.UNMATCHED_ACTIONS[1])
        self.app.search_iqdb_engine = lambda path, **kwargs: ([] if Path(path).name == '0.png' else
            [{'engine': 'IQDB', 'title': 'test', 'booru_ids': {'danbooru': 1}}])
        self.app.fetch_tags = lambda *args: {'combined': 'unknown_series', 'copyright': 'unknown_series'}
        self.app.start_thread()
        self.app.unmatched_path = str(self.root / 'changed-during-batch')
        self.app.opt_unmatched.set(routing.UNMATCHED_ACTIONS[0])
        self._pump(lambda: not self.app.is_processing)
        self.assertEqual((custom / '0.png').read_bytes(), b'keep existing file')
        self.assertTrue((custom / '0 (1).png').is_file())
        self.assertTrue((custom / '1.png').is_file()); self.assertTrue((custom / '2.png').is_file())
        self.assertFalse(list(self.source.glob('*.png')))
        self.assertEqual(len(self.app.history_items), 3)
        self.assertTrue(all(e['is_unmatched'] and Path(e['current_path']).parent == custom for e in self.app.history_items))
        self.assertIsNone(self.app.pending_session)
        self.assertFalse(self.errors, self.errors)

    def test_history_scope_latest_results_filters_and_audit_survive_reopen(self):
        source = str(self.source / '0.png')
        self.app._current_session_id = 'older-session'
        self.app.add_history_item(source, source, '0.png', True)
        target = storage.safe_move(source, str(self.library / '0.png'))
        self.app.add_history_item(source, target, '0.png')
        self._pump(lambda: len(self.app.history_items) == 2)
        self.app._current_session_id = 'current-session'
        attention = str(self.source / '1.png')
        self.app.add_history_item(attention, attention, '1.png', True)
        self.app.add_history_item(attention, attention, '1.png', True)
        other = str(self.source / '2.png')
        unmatched = self.root / 'Unmatched'; unmatched.mkdir()
        moved_unmatched = storage.safe_move(other, str(unmatched / '2.png'))
        self.app.add_history_item(other, moved_unmatched, '2.png', True)
        self._pump(lambda: len(self.app.history_items) == 5)
        self.app.tabview.set('History')
        expected = {'All results': ['0.png', '1.png', '2.png'], 'Moved': ['0.png'], 'Needs attention': ['1.png', '2.png']}
        for name in ('Needs attention', 'Moved', 'All results', 'Moved', 'Needs attention'):
            self.app.history_filter.buttons[name].invoke()
            self._pump(lambda: self.app._history_page_signature is not None)
            entries = self.app.get_filtered_history_items()
            self.assertEqual([e['filename'] for e in entries], expected[name])
            self.assertEqual([e['filename'] for e in self.app._history_visible_entries], expected[name])
            with patch.object(self.app, 'open_file_external') as opened:
                entries[0]['local_thumbnail']._label.event_generate('<Button-1>')
                opened.assert_called_once_with(entries[0]['current_path'])
        self.app.history_scope.set('This session'); self.app.on_history_filter()
        self.app.history_filter.buttons['All results'].invoke()
        self.assertEqual(len(self.app.get_filtered_history_items()), 2)
        self.app.switch_history_attempts.select(); self.app.on_history_filter()
        self.assertEqual(len(self.app.get_filtered_history_items()), 3)
        self.app.history_scope.set('Overall history'); self.app.on_history_filter()
        self.assertEqual(len(self.app.get_filtered_history_items()), 5)
        self.assertEqual(len(self.app.history_items), 5)  # No destructive deduplication.
        self._close(); self._open()
        self.assertEqual(len(self.app.history_items), 5)
        self.assertEqual(self.app._current_session_id, 'current-session')
        self.app._load_history(); self.app._load_history()
        self.assertEqual(len(self.app.history_items), 5)
        self.assertEqual(len(self.app.get_filtered_history_items()), 3)
        self.assertFalse(self.errors, self.errors)

    def test_logs_and_usage_are_independent_in_horizontal_layout(self):
        self.app.geometry('1100x820'); self.app.update()
        self.assertFalse(self.app.activity_frame.winfo_manager())
        self.assertFalse(self.app.api_usage_host.winfo_manager())
        self.app.btn_activity.invoke(); self.app.update()
        self.assertEqual(self.app.activity_frame.winfo_manager(), 'panedwindow')
        self.assertFalse(self.app.api_usage_host.winfo_manager())
        self.app.btn_api_usage.invoke(); self.app.update()
        self.assertEqual(self.app.api_usage_host.winfo_manager(), 'panedwindow')
        self.assertGreater(self.app.diagnostics_frame.winfo_x(), 0)
        self.assertGreater(self.app.txt_console_simp.winfo_height(), 145)
        self.app.btn_activity.invoke(); self.app.update()
        self.assertFalse(self.app.activity_frame.winfo_manager())
        self.assertEqual(self.app.api_usage_host.winfo_manager(), 'panedwindow')
        self.app.btn_api_usage.invoke(); self.app.update()
        self.assertFalse(self.app.diagnostics_frame.winfo_manager())
        self.assertFalse(self.errors, self.errors)

    def test_per_key_display_preserves_both_balances_and_does_not_expose_keys(self):
        self.app.serpapi_entries[0][1].insert(0, 'primary-secret')
        self.app.add_serpapi_field('backup-secret')
        with patch.object(ui, 'tracker', providers.ApiTracker()) as tracker:
            tracker.key_record('serpapi', 'primary-secret', operation='requests', status=429,
                               quota={'total_searches_left': 0, 'observed_at': time.time()})
            tracker.key_record('serpapi', 'backup-secret', operation='requests', status=200,
                               quota={'total_searches_left': 87, 'observed_at': time.time()})
            self.app.toggle_api_usage(); self.app._update_key_usage()
            text = self.app.lbl_key_usage.cget('text')
            self.assertIn('Key 1: 1 requests', text); self.assertIn('Key 2: 1 requests', text)
            self.assertIn('0 searches left', text); self.assertIn('87 searches left', text)
            self.assertNotIn('primary-secret', text); self.assertNotIn('backup-secret', text)
        self.assertFalse(self.errors, self.errors)

    def test_history_drag_coalesces_offsets_without_scrolling_embedded_windows(self):
        for index in range(1200):
            self.app.history_items.append({'id': str(index), 'filename': f'{index}.png', 'original_path': str(self.source / str(index)),
                'current_path': str(self.source / f'{index % 3}.png'), 'is_unmatched': True, 'reason': 'No supported series folder'})
        self.app.switch_history_attempts.select()
        self.app.tabview.set('History')
        self._pump(lambda: self.app._history_page_signature is not None)
        viewport = self.app.history_scroll
        self.assertIsInstance(viewport, ui.HistoryViewport)
        self.assertIs(viewport.content.master, viewport.viewport)
        self.assertFalse(hasattr(viewport, '_parent_canvas'))
        cards = list(self.app._history_cards)
        viewport._apply_canvas_fit()
        self.assertGreater(viewport._content_height, viewport.viewport.winfo_height())
        jobs = []
        for index in range(100):
            viewport.yview('moveto', index / 100)
            jobs.append(viewport._scroll_job)
        self.assertEqual(len(set(jobs)), 1)
        self._pump(lambda: viewport._scroll_job is None)
        self.assertEqual(viewport.yview()[1], 1)
        self.assertEqual(viewport.content.winfo_y(), -viewport._offset)
        self.assertEqual(self.app._history_cards, cards)
        # Mouse wheel over a History child stays scoped to this viewport.
        viewport._mouse_wheel(SimpleNamespace(widget=cards[0], delta=120))
        self._pump(lambda: viewport._scroll_job is None)
        self.assertLess(viewport.yview()[1], 1)
        offset = viewport._offset
        viewport._mouse_wheel(SimpleNamespace(widget=self.app.ent_lm, delta=-120))
        self.assertEqual(viewport._offset, offset)
        self.app.history_filter.buttons['Moved'].invoke()
        self._pump(lambda: self.app._history_page_signature is not None)
        self._pump(lambda: viewport.yview() == (0, 1))
        self.assertEqual(viewport.yview(), (0, 1))
        self.assertFalse(self.errors, self.errors)

    def test_matching_mode_persists_and_legacy_settings_default_to_exact(self):
        self.app.tabview.set('Settings'); self.app.show_settings_section('Folder routing'); self.app.update()
        self.assertTrue(self.app.opt_matching_mode.winfo_manager())
        self.assertEqual(self.app.opt_matching_mode.get(), 'Exact')
        self.app.opt_matching_mode.set('Flexible'); self.app.on_matching_mode_change('Flexible')
        self.assertTrue(self.app.save_settings())
        self.app.opt_matching_mode.set('Exact'); self.app.load_settings()
        self.assertEqual(self.app.opt_matching_mode.get(), 'Flexible')
        path = self.root / 'legacy.json'; path.write_text('{}')
        self.app.load_settings(str(path))
        self.assertEqual(self.app.opt_matching_mode.get(), 'Exact')
        self.assertFalse(self.errors, self.errors)

    def test_flexible_batch_uses_its_prompt_and_frozen_mode_without_repeating_searches(self):
        category = self.library / 'Broader category'; category.mkdir()
        searches = []
        self.app.search_iqdb_engine = lambda path, **kwargs: searches.append(path) or [
            {'engine': 'IQDB', 'title': 'identified image', 'booru_ids': {'danbooru': 1}}]
        self.app.fetch_tags = lambda *args: {'combined': 'unlisted_series known_person', 'copyright': 'unlisted_series', 'character': 'known_person'}
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"candidate_id":1}'), finish_reason='stop')]))
        client = SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id='test')])),
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
        with patch.object(providers, 'OpenAI', return_value=client):
            self.app.start_thread(); self._pump(lambda: not self.app.is_processing)
            self.assertEqual(len(list(self.source.glob('*.png'))), 3)
            self.assertEqual(create.call_count, 0)
            self.app.opt_matching_mode.set('Flexible')
            self.app.start_thread()
            self.app.opt_matching_mode.set('Exact')  # The active worker must keep Flexible.
            self._pump(lambda: not self.app.is_processing)
            self.assertEqual(len(list(category.glob('*.png'))), 3)
            self.assertEqual(len(searches), 3)  # Identification cache is shared between modes.
            self.assertEqual(create.call_count, 3)
            self.assertTrue(all('general knowledge' in call.kwargs['messages'][0]['content'] for call in create.call_args_list))
            entry = next(e for e in reversed(self.app.history_items) if e['filename'] == '0.png' and not e['is_unmatched'])
            self.app.undo_move(entry, None)
            self.app.start_thread(); self._pump(lambda: not self.app.is_processing)
            self.assertTrue((self.source / '0.png').is_file())
            self.assertEqual(create.call_count, 3)  # Exact cannot reuse the Flexible routing decision.
            self.assertEqual(len(searches), 3)
        self.assertFalse(self.errors, self.errors)

    def test_draggable_diagnostics_preserve_user_splits_when_panels_toggle(self):
        self.app.geometry('1250x900'); self.app.update()
        self.app.toggle_activity(); self.app.toggle_api_usage(); self.app.update()
        horizontal, vertical = self.app.sort_split, self.app.diagnostics_split
        self._pump(lambda: self.app._main_sash_job is None and self.app._diagnostics_sash_job is None)
        self.assertEqual(len(horizontal.panes()), 2); self.assertEqual(len(vertical.panes()), 2)
        horizontal.sash_place(0, int(horizontal.winfo_width() * .62), 0)
        vertical.sash_place(0, 0, int(vertical.winfo_height() * .52))
        self.app.update(); self.app._capture_sashes()
        width_ratio, height_ratio = self.app._sort_sash_ratio, self.app._diagnostics_sash_ratio
        self.app.toggle_api_usage(); self.app.update()
        self.assertEqual(len(vertical.panes()), 1)
        self.app.toggle_activity(); self.app.update()
        self.assertEqual(len(horizontal.panes()), 1)
        self.app.toggle_activity(); self.app.toggle_api_usage(); self.app.update()
        self._pump(lambda: self.app._main_sash_job is None and self.app._diagnostics_sash_job is None)
        self.assertAlmostEqual(horizontal.sash_coord(0)[0] / horizontal.winfo_width(), width_ratio, delta=.04)
        self.assertAlmostEqual(vertical.sash_coord(0)[1] / vertical.winfo_height(), height_ratio, delta=.04)
        self.assertFalse(self.errors, self.errors)

    def test_history_actions_share_one_row_then_wrap_without_an_open_image_button(self):
        path = str(self.source / '0.png')
        self.app.add_history_item(str(self.root / 'incoming.png'), path, '0.png')
        self._pump(lambda: bool(self.app.history_items))
        self.app.geometry('1250x900'); self.app.tabview.set('History')
        entry = self.app.history_items[0]
        self._pump(lambda: self.app._history_page_signature is not None and entry['card']._history_actions._placement is not None)
        bar = entry['card']._history_actions
        self.assertNotIn('btn_open_local', entry)
        self._pump(lambda: len({button.grid_info()['row'] for button in bar._buttons}) == 1)
        buttons = list(bar._buttons)
        with patch.object(self.app, 'open_file_external') as opened:
            entry['local_thumbnail']._label.event_generate('<Button-1>')
            opened.assert_called_once_with(path)
        bar.grid_propagate(False); bar.configure(width=330, height=110); bar.grid_configure(sticky='w')
        self._pump(lambda: len({button.grid_info()['row'] for button in bar._buttons}) > 1)
        self.assertEqual(bar._buttons, buttons)
        bar.grid_propagate(True); bar.grid_configure(sticky='ew')
        self._pump(lambda: len({button.grid_info()['row'] for button in bar._buttons}) == 1)
        self.assertFalse(self.errors, self.errors)

    def test_resize_and_tab_repaint_requests_are_coalesced(self):
        self.app.update()
        with patch.object(ui, 'invalidate_native_window') as paint:
            jobs = []
            for _ in range(100):
                self.app._queue_native_repaint(); jobs.append(self.app._native_paint_job)
            self.assertEqual(len(set(jobs)), 1)
            self._pump(lambda: self.app._native_paint_job is None)
            self.assertTrue(paint.called)
            for name in ('History', 'Settings', 'Sort', 'History', 'Sort'):
                self.app.tabview.set(name); self.app.update()
                self.assertTrue(self.app.tabview.pages[name].grid_info())
                self.assertTrue(all(not page.grid_info() for title, page in self.app.tabview.pages.items() if title != name))
        self.assertFalse(self.errors, self.errors)


if __name__ == '__main__':
    unittest.main()

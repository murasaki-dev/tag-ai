"""Qt rendering checks using the dependencies in requirements.txt."""
import base64
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Unit tests render offscreen; frozen_app_smoke exercises the real native window.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
try:
    import qt_core as qt
except ModuleNotFoundError as error:
    if error.name and error.name.startswith('PySide6'):
        raise unittest.SkipTest('Qt dependencies are not installed')
    raise
from PIL import Image
from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuickControls2 import QQuickStyle
from shiboken6 import delete as delete_qt_object


class QtCoreTests(unittest.TestCase):
    def test_destroyed_response_wrapper_is_retired_on_gui_queue(self):
        self.provider = qt.ThumbnailProvider()
        response = self.provider.requestImageResponse(qt.image_token('local',str(self.red),0).split('/')[-1],QSize(256,256))
        self.provider.local_pool.shutdown(wait=True)
        identity=id(response)
        delete_qt_object(response)
        self.assertIn(identity,self.provider.active)
        self.pump(lambda:identity not in self.provider.active)

    @classmethod
    def setUpClass(cls):
        cls.app = QGuiApplication.instance() or QGuiApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        QQuickStyle.setStyle('Fusion')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.red, self.blue = self.root / 'red.png', self.root / 'blue.png'
        Image.new('RGB', (64, 48), '#d12a2a').save(self.red)
        Image.new('RGB', (64, 48), '#2a4ed1').save(self.blue)
        self.backend = self.engine = self.provider = None

    def tearDown(self):
        if self.backend:
            self.backend.close()
        if self.engine:
            for window in self.engine.rootObjects():
                window.setVisible(False)
        if self.provider:
            self.provider.close()
        if self.engine:
            delete_qt_object(self.engine); self.app.processEvents()
        self.temp.cleanup()

    def pump(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.005)
        self.assertTrue(condition(), 'Qt event loop timed out')

    def row(self, identity='one', path=None, unmatched=False, session='new'):
        return {'id': identity, 'original_path': str(self.root / 'incoming' / identity),
                'current_path': str(path or self.red), 'filename': identity + '.png',
                'is_unmatched': unmatched, 'session_id': session, 'reason': 'test', 'match_url': 'https://example.com/post/1'}

    @staticmethod
    def visual_items(window, name):
        found, pending = [], [window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name:
                found.append(item)
            pending.extend(item.childItems())
        return found

    def make_database(self, rows):
        path = self.root / 'operation_history.sqlite3'
        with closing(qt.sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE history(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            connection.executemany('INSERT INTO history VALUES(?,?)', [(r['id'], json.dumps(r)) for r in rows])
        return path

    def test_existing_database_is_read_only_and_recorded_moves_resolve_in_memory(self):
        old = str(self.root / 'missing.png')
        earlier = self.row('old', path=old, unmatched=True)
        earlier['original_path'] = old
        later = self.row('new'); later['original_path'] = old
        path = self.make_database([earlier, later])
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        loaded = qt.read_history(path)
        self.assertEqual([r['current_path'] for r in loaded], [str(self.red)] * 2)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        with closing(qt.sqlite3.connect(path)) as connection:
            self.assertEqual(json.loads(connection.execute("SELECT payload FROM history WHERE id='old'").fetchone()[0])['current_path'], old)
        self.assertEqual(len(qt.read_history(path)), 2)

    def test_latest_file_scope_and_outcome_filters_match_original_history(self):
        rows = [self.row('earlier', unmatched=True, session='old'), self.row('sorted', session='old'), self.row('attention', path=self.blue, unmatched=True)]
        all_rows, counts, attempts = qt.history_view(rows)
        self.assertEqual([r['id'] for r in all_rows], ['sorted', 'attention'])
        self.assertEqual(counts, {'All results': 2, 'Needs attention': 1, 'Moved': 1})
        self.assertEqual(attempts, 3)
        self.assertEqual([r['id'] for r in qt.history_view(rows, 'Moved')[0]], ['sorted'])
        self.assertEqual([r['id'] for r in qt.history_view(rows, scope='Latest session')[0]], ['attention'])
        self.assertEqual(len(qt.history_view(rows, attempts=True)[0]), 3)

    def test_source_and_match_tokens_stay_distinct_and_paths_follow_current_location(self):
        row = self.row(); row['match_thumb_url'] = 'https://example.com/matched.jpg'
        presented = qt.model_row(row, 1)
        def unpack(value):
            token = value.split('/')[-1]
            return json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
        self.assertEqual(unpack(presented['localThumbnail'])[:2], ['local', str(self.red)])
        self.assertEqual(unpack(presented['matchThumbnail'])[:2], ['remote', row['match_thumb_url']])
        moved = self.root / 'moved.png'; os.replace(self.red, moved); row['current_path'] = str(moved)
        self.assertEqual(unpack(qt.model_row(row, 2)['localThumbnail'])[1], str(moved))

    def test_thumbnail_cache_preserves_orientation_alpha_and_bounded_storage(self):
        rotated = self.root / 'rotated.jpg'
        image = Image.new('RGB', (64, 32), 'red'); exif = image.getexif(); exif[274] = 6; image.save(rotated, exif=exif)
        cache = qt.ThumbnailCache(limit=2)
        decoded = cache.load('local', str(rotated))
        self.assertEqual((decoded.width(), decoded.height()), (32, 64))
        transparent = self.root / 'alpha.png'; Image.new('RGBA', (8, 8), (10, 20, 30, 0)).save(transparent)
        decoded = cache.load('local', str(transparent))
        self.assertEqual(decoded.pixelColor(2, 2).alpha(), 0)
        red = cache.load('local', str(self.red)); blue = cache.load('local', str(self.blue))
        self.assertGreater(red.pixelColor(20, 20).red(), red.pixelColor(20, 20).blue())
        self.assertGreater(blue.pixelColor(20, 20).blue(), blue.pixelColor(20, 20).red())
        self.assertEqual(len(cache.cache), 2)

    def test_slow_remote_thumbnail_does_not_block_local_image_or_gui(self):
        entered, release = threading.Event(), threading.Event()
        data = self.blue.read_bytes()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                entered.set(); release.wait(6)
                self.send_response(200); self.send_header('Content-Type', 'image/png'); self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.provider = qt.ThumbnailProvider()
        try:
            remote_token = qt.image_token('remote', f'http://127.0.0.1:{server.server_port}/image').split('/')[-1]
            remote = self.provider.requestImageResponse(remote_token, QSize(256, 256))
            self.pump(entered.is_set)
            local = self.provider.requestImageResponse(qt.image_token('local', str(self.red)).split('/')[-1], QSize(256, 256))
            self.pump(lambda: not local.image.isNull())
            self.assertTrue(remote.image.isNull())
            ticks = []
            timer = qt.QTimer(); timer.setInterval(10); timer.timeout.connect(lambda: ticks.append(1)); timer.start()
            self.pump(lambda: len(ticks) >= 6); timer.stop()
            finished = threading.Event(); remote.finished.connect(finished.set)
            remote.cancel(); release.set()
            self.pump(finished.is_set)
            self.assertTrue(remote.image.isNull())
        finally:
            release.set(); server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_local_folder_and_source_actions_use_distinct_current_targets(self):
        self.backend = qt.HistoryBackend(self.root)
        row = self.row(); self.backend.lookup = {'one': row}
        with patch.object(qt.QDesktopServices, 'openUrl', return_value=True) as opened:
            self.backend.openLocal('one')
            self.assertEqual(qt.path_key(opened.call_args.args[0].toLocalFile()), qt.path_key(str(self.red)))
            self.backend.openSource('one')
            self.assertEqual(opened.call_args.args[0].toString(), row['match_url'])
            opened.reset_mock(); row['match_url'] = 'javascript:alert(1)'
            self.backend.openSource('one'); opened.assert_not_called()
        with patch.object(qt.subprocess, 'Popen') as reveal:
            self.backend.showFolder('one')
            if os.name == 'nt':
                self.assertIn(os.path.normpath(str(self.red)), reveal.call_args.args[0])

    def test_stale_load_and_view_results_cannot_replace_new_dataset(self):
        self.backend = qt.HistoryBackend(self.root); self.backend.generation = 3; self.backend.view_generation = 4
        self.backend._loaded(2, [self.row()], '')
        self.backend._viewed(3, 3, [qt.model_row(self.row(), 1)], {}, 1, 1)
        self.assertFalse(self.backend.raw); self.assertEqual(self.backend.model.rowCount(), 0)

    def test_queued_thumbnail_cancellation_still_completes_each_qt_response(self):
        self.provider = qt.ThumbnailProvider()
        release = threading.Event()
        blockers = [self.provider.local_pool.submit(lambda: release.wait(6)) for _ in range(2)]
        self.pump(lambda: all(future.running() for future in blockers))
        finished = []
        responses = [self.provider.requestImageResponse(qt.image_token('local', str(self.red), i).split('/')[-1], QSize(256, 256)) for i in range(10)]
        for response in responses:
            response.finished.connect(lambda: finished.append(1))
        closer = threading.Thread(target=self.provider.close)
        closer.start()
        try:
            self.pump(lambda: len(finished) == len(responses))
            self.assertTrue(all(response.image.isNull() for response in responses))
        finally:
            release.set(); closer.join(timeout=8)
        self.assertFalse(closer.is_alive())


if __name__ == '__main__':
    unittest.main()

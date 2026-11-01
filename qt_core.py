"""Shared Qt History models, asynchronous thumbnails, and desktop file actions."""
from __future__ import annotations

import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import threading
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps
from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Property, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QImage
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickAsyncImageProvider, QQuickImageResponse, QQuickTextureFactory


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


def read_history(path):
    """Read the existing schema without creating a DB, changing rows or migrating it."""
    path = Path(path)
    if not path.is_file():
        return []
    rows, known = [], set()
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as connection:
        connection.execute('PRAGMA query_only=ON')
        for (payload,) in connection.execute('SELECT payload FROM history ORDER BY rowid'):
            try:
                row = json.loads(payload)
                if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ('id', 'original_path', 'current_path')):
                    continue
                if row['id'] in known:
                    continue
                known.add(row['id']); rows.append(row)
            except (ValueError, TypeError):
                continue
    # Resolve recorded move chains in memory only, preserving the on-disk audit.
    links = {path_key(r['original_path']): r['current_path'] for r in rows if r['original_path'] != r['current_path']}
    exists = {}
    def is_file(value):
        key = path_key(value)
        if key not in exists:
            exists[key] = os.path.isfile(value)
        return exists[key]
    for row in rows:
        current, seen = row['current_path'], set()
        while not is_file(current) and path_key(current) in links and path_key(current) not in seen:
            seen.add(path_key(current)); current = links[path_key(current)]
        if is_file(current):
            row['current_path'] = current
    return rows


def history_view(rows, category='All results', scope='Overall history', attempts=False, demo=False, session_id=None):
    session = session_id or next((r.get('session_id') for r in reversed(rows) if r.get('session_id')), None)
    selected = [r for r in rows if scope == 'Overall history' or session and r.get('session_id') == session]
    recorded = len(selected)
    if not attempts and not demo:
        selected = list({path_key(r['current_path']): r for r in selected}.values())
    counts = {'All results': len(selected), 'Needs attention': sum(bool(r.get('is_unmatched')) for r in selected),
              'Moved': sum(not r.get('is_unmatched') and r['original_path'] != r['current_path'] for r in selected)}
    if category == 'Needs attention':
        selected = [r for r in selected if r.get('is_unmatched')]
    elif category == 'Moved':
        selected = [r for r in selected if not r.get('is_unmatched') and r['original_path'] != r['current_path']]
    return selected, counts, recorded


def image_token(kind, source, revision=0):
    if not source:
        return ''
    data = json.dumps([kind, source, revision], ensure_ascii=False).encode()
    return 'image://thumbnails/' + base64.urlsafe_b64encode(data).decode().rstrip('=')


def source_url(row):
    value = row.get('match_url', '') or ''
    if value.startswith('//'):
        value = 'https:' + value
    return value if urlparse(value).scheme in ('http', 'https') else ''


def identification_method(row):
    """Prefer explicit provenance; infer only a known leading legacy reason."""
    method = row.get('identification_method') or ''
    if not isinstance(method, str): method = ''
    if not method:
        method = row.get('reason') or ''
        if not isinstance(method, str): method = ''
    for prefix in ('IQDB', 'Booru hash', 'SauceNAO', 'Local Vision LLM', 'Embedded Metadata'):
        if method == prefix or method.startswith(prefix + ' ') or method.startswith(prefix + ' ·'):
            return prefix
    return ''


def model_row(row, revision):
    row = dict(row)
    moved = row['original_path'] != row['current_path']
    unmatched = bool(row.get('is_unmatched'))
    row['recordId'] = row['id']
    row['filename'] = row.get('filename') or os.path.basename(row['current_path'])
    row['folder'] = os.path.dirname(row['current_path'])
    row['statusText'] = ('Needs attention · moved to Unmatched' if moved else 'Needs attention · left in source') if unmatched else ('Sorted into' if moved else 'At source')
    if row.get('move_undone'): row['statusText'] = 'Move undone · returned to source'
    row['reasonText'] = str(row.get('reason') or '')
    if row['reasonText'].startswith('Model abstained'):
        row['reasonText'] = 'Model could not confidently choose a supported folder'
    row['localThumbnail'] = image_token('local', row.get('_local_preview_path') or row['current_path'], revision)
    row['matchThumbnail'] = (image_token('local', row['_match_preview_path'], revision) if row.get('_match_preview_path') else
                             image_token('remote', row.get('match_thumb_url', ''), revision))
    row['hasSource'] = bool(source_url(row))
    row['unmatched'] = unmatched
    row['canUndo'] = moved and not row.get('move_undone')
    row['canUnmatched'] = bool(row.get('move_undone')) or not unmatched or not moved
    method = identification_method(row)
    label = {'Local Vision LLM': 'Vision LLM', 'Embedded Metadata': 'Embedded tags'}.get(method, method)
    row['methodLabel'] = (label + ' · needs attention' if label else 'Needs attention') if unmatched else (label or 'Method not recorded')
    row['methodColorKey'] = 'Unmatched' if unmatched or not method else method
    row['cachedResult'] = row.get('from_cache') is True
    return row


class HistoryModel(QAbstractListModel):
    names = ('recordId', 'filename', 'folder', 'statusText', 'reasonText', 'localThumbnail', 'matchThumbnail', 'hasSource', 'unmatched', 'canUndo', 'canUnmatched', 'dateText', 'methodLabel', 'methodColorKey', 'cachedResult')
    roles = {int(Qt.ItemDataRole.UserRole) + i + 1: name.encode() for i, name in enumerate(names)}

    def __init__(self, parent=None):
        super().__init__(parent); self.rows = []

    def roleNames(self):
        return self.roles

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        name = self.roles.get(role)
        return self.rows[index.row()].get(name.decode()) if name else None

    def replace(self, rows):
        if getattr(self, 'incremental', False):
            rows = list(rows); common = min(len(rows), len(self.rows)); changed = []
            for index in range(common):
                if rows[index] != self.rows[index]: self.rows[index] = rows[index]; changed.append(index)
            if changed: self.dataChanged.emit(self.index(min(changed)), self.index(max(changed)), list(self.roles))
            if len(rows) < len(self.rows):
                self.beginRemoveRows(QModelIndex(), len(rows), len(self.rows) - 1); del self.rows[len(rows):]; self.endRemoveRows()
            elif len(rows) > len(self.rows): self.append(rows[len(self.rows):])
            return
        self.beginResetModel(); self.rows = list(rows); self.endResetModel()

    def append(self, rows):
        if not rows:
            return
        first = len(self.rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self.rows.extend(rows); self.endInsertRows()


@contextmanager
def shared_image_stream(path):
    """Allow a Windows sort/undo to rename the image while its preview decodes."""
    if os.name != 'nt':
        with open(path, 'rb') as stream:
            yield stream
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        yield stream


class ThumbnailCache:
    """Decode off the GUI thread; store bounded QImages, never worker QPixmaps."""
    def __init__(self, limit=192):
        self.limit = limit; self.cache = OrderedDict(); self.lock = threading.Lock()
        self.sessions = threading.local()

    def load(self, kind, source, cancelled=None):
        if kind == 'local':
            stat = os.stat(source)
            key = ('local', path_key(source), stat.st_mtime_ns, stat.st_size)
        elif kind == 'remote':
            if source.startswith('//'):
                source = 'https:' + source
            if urlparse(source).scheme not in ('http', 'https'):
                raise ValueError('Unsupported thumbnail URL')
            key = ('remote', source)
        else:
            raise ValueError('Unsupported thumbnail type')
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return QImage(self.cache[key])
        if kind == 'remote':
            if not hasattr(self.sessions, 'session'):
                self.sessions.session = requests.Session()
            headers = {'User-Agent': 'TagAI/1.0'}
            host = (urlparse(source).hostname or '').lower()
            for domain, referer in (('zerochan.net', 'https://www.zerochan.net/'), ('gelbooru.com', 'https://gelbooru.com/'),
                                    ('donmai.us', 'https://danbooru.donmai.us/'), ('yande.re', 'https://yande.re/'), ('pixiv.net', 'https://www.pixiv.net/')):
                if host == domain or host.endswith('.' + domain):
                    headers['Referer'] = referer; break
            started = time.monotonic()
            with self.sessions.session.get(source, headers=headers, timeout=(5, 10), stream=True) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(65536):
                    if cancelled is not None and cancelled.is_set():
                        raise RuntimeError('Thumbnail cancelled')
                    if time.monotonic() - started > 20:
                        raise TimeoutError('Thumbnail download exceeded its time budget')
                    data.extend(chunk)
                    if len(data) > 12 * 1024 * 1024:
                        raise ValueError('Thumbnail response too large')
            source_image = BytesIO(data)
        else:
            source_image = None
        with (shared_image_stream(source) if kind == 'local' else closing(source_image)) as stream, Image.open(stream) as opened:
            opened.draft('RGB', (512, 512))
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((256, 256))
            image = image.convert('RGBA')
            decoded = QImage(image.tobytes(), image.width, image.height, image.width * 4, QImage.Format.Format_RGBA8888).copy()
        with self.lock:
            self.cache[key] = decoded; self.cache.move_to_end(key)
            while len(self.cache) > self.limit:
                self.cache.popitem(last=False)
        return QImage(decoded)


class ThumbnailResponse(QQuickImageResponse):
    def __init__(self, loader, token):
        super().__init__()
        self.loader, self.token = loader, token
        self.image = QImage(); self.error = ''; self.cancelled = threading.Event()
        self.destroyed.connect(self.cancelled.set)

    def cancel(self):
        self.cancelled.set()

    def textureFactory(self):
        return QQuickTextureFactory.textureFactoryForImage(self.image)

    def errorString(self):
        return self.error

    def finish(self):
        try:
            self.finished.emit()
        except RuntimeError:
            pass

    def run(self):
        try:
            if not self.cancelled.is_set():
                kind, source, _revision = json.loads(base64.urlsafe_b64decode(self.token + '=' * (-len(self.token) % 4)))
                image = self.loader.load(kind, source, self.cancelled)
                if not self.cancelled.is_set():
                    self.image = image
        except Exception:
            self.error = 'Preview unavailable'
        # Qt owns the response after requestImageResponse returns. A cancelled
        # delegate can already have destroyed it; never deliver into freed C++.
        self.finish()


class ResponseRetainer(QObject):
    retired = Signal(object)

    def __init__(self, active, lock):
        super().__init__()
        self.active, self.lock = active, lock
        self.retired.connect(self.release, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def release(self, identity):
        # Qt emits destroyed from its render/image thread. Dropping the last
        # Python wrapper there can re-enter Qt's in-progress native destructor.
        with self.lock: self.active.pop(identity, None)


class ThumbnailProvider(QQuickAsyncImageProvider):
    def __init__(self):
        super().__init__()
        self.loader = ThumbnailCache()
        self.local_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='qt-local-image')
        self.remote_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='qt-remote-image')
        self.active = {}; self.lock = threading.Lock(); self.closed = False
        self.retainer = ResponseRetainer(self.active, self.lock)

    def requestImageResponse(self, request_id, requested_size):
        response = ThumbnailResponse(self.loader, request_id)
        identity = id(response)
        with self.lock:
            self.active[identity] = response
        retainer = self.retainer
        def released():
            try: retainer.retired.emit(identity)
            except RuntimeError: pass  # Qt may already be destroying the app-wide receiver.
        response.destroyed.connect(released)
        try:
            kind = json.loads(base64.urlsafe_b64decode(request_id + '=' * (-len(request_id) % 4)))[0]
        except Exception:
            kind = 'local'
        pool = self.remote_pool if kind == 'remote' else self.local_pool
        if not self.closed:
            future = pool.submit(response.run)
            future.add_done_callback(lambda done: response.finish() if done.cancelled() else None)
        else:
            response.cancel()
            QTimer.singleShot(0, response.finished.emit)
        return response

    def close(self):
        if self.closed:
            return
        self.closed = True
        with self.lock:
            for response in self.active.values():
                response.cancel()
        self.local_pool.shutdown(wait=False, cancel_futures=True)
        self.remote_pool.shutdown(wait=False, cancel_futures=True)


class HistoryBackend(QObject):
    changed = Signal()
    logsChanged = Signal()
    loaded = Signal(int, object, str)
    viewed = Signal(int, int, object, object, int, int)

    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.database = Path(data_dir) / 'operation_history.sqlite3'
        self.model = HistoryModel(self)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='qt-history')
        self.raw = []; self.lookup = {}; self.generation = 0; self.view_generation = 0
        self.category = 'All results'; self.scope = 'Overall history'; self.attempts = False
        self._running = False; self._loading = False; self.closed = False
        self.stop = threading.Event()
        self._status = 'Loading saved history…'; self._summary = ''; self._log_lines = []
        self._counts = {}
        self._latest_local = ''; self._latest_match = ''; self._latest_id = ''
        self.loaded.connect(self._loaded, Qt.ConnectionType.QueuedConnection)
        self.viewed.connect(self._viewed, Qt.ConnectionType.QueuedConnection)

    @Property(QObject, constant=True)
    def historyModel(self): return self.model

    @Property(str, notify=changed)
    def status(self): return self._status

    @Property(str, notify=changed)
    def summary(self): return self._summary

    @Property(bool, notify=changed)
    def running(self): return self._running

    @Property(bool, notify=changed)
    def loading(self): return self._loading

    @Property(str, notify=logsChanged)
    def logText(self): return '\n'.join(self._log_lines)

    @Property(str, notify=changed)
    def databasePath(self): return str(self.database)

    @Property(str, notify=changed)
    def latestLocal(self): return self._latest_local

    @Property(str, notify=changed)
    def latestMatch(self): return self._latest_match

    def log(self, text):
        self._log_lines.append(text); self._log_lines = self._log_lines[-250:]; self.logsChanged.emit()

    @Slot()
    def loadSaved(self):
        self.stop.set(); self.stop = threading.Event(); self.generation += 1
        generation = self.generation; self._running = False; self._loading = True
        self.raw = []; self.lookup = {}; self.model.replace([]); self._summary = 'Loading saved History…'
        self._status = 'Reading saved History…'; self.changed.emit()
        path = self.database
        def load():
            try:
                rows, error = read_history(path), ''
            except Exception as exc:
                rows, error = [], f'Could not read History: {exc}'
            if not self.closed:
                self.loaded.emit(generation, rows, error)
        self.pool.submit(load)

    @Slot(QUrl)
    def openHistory(self, url):
        path = Path(url.toLocalFile())
        if path.is_file():
            self.database = path; self.loadSaved()

    @Slot(int, object, str)
    def _loaded(self, generation, rows, error):
        if generation != self.generation or self.closed:
            return
        self.raw = rows; self.lookup = {r['id']: r for r in rows}
        self._loading = False; self._status = error or f'Saved History · {len(rows)} attempts · read-only'
        self.log(self._status); self._request_view(); self.changed.emit()

    @Slot(str, str, bool)
    def setFilters(self, category, scope, attempts):
        self.category, self.scope, self.attempts = category, scope, attempts
        self._request_view()

    def _request_view(self):
        self.view_generation += 1
        generation, view = self.generation, self.view_generation
        rows = tuple(self.raw); category, scope, attempts = self.category, self.scope, self.attempts
        def build():
            selected, counts, recorded = history_view(rows, category, scope, attempts)
            selected = [model_row(row, generation) for row in selected]
            if not self.closed:
                self.viewed.emit(generation, view, selected, counts, recorded, len(rows))
        self.pool.submit(build)

    @Slot(int, int, object, object, int, int)
    def _viewed(self, generation, view, rows, counts, recorded, snapshot_count):
        if generation != self.generation or view != self.view_generation or self.closed:
            return
        self.model.replace(rows); self._counts = counts
        self._summary = f"{len(rows)} shown · {recorded} recorded attempts · latest result per file"
        self.changed.emit()

    @Slot(str)
    def openLocal(self, record_id):
        row = self.lookup.get(record_id)
        if row:
            path = row.get('_local_preview_path') or row['current_path']
            if os.path.isfile(path) and Path(path).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            else:
                self.log('Local image is unavailable at its recorded location.')

    @Slot(str)
    def showFolder(self, record_id):
        row = self.lookup.get(record_id)
        if row:
            path = row.get('_local_preview_path') or row['current_path']
            if os.path.isfile(path) and os.name == 'nt':
                subprocess.Popen(['explorer.exe', '/select,', os.path.normpath(path)])
            elif os.path.isdir(os.path.dirname(path)):
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    @Slot(str)
    def openSource(self, record_id):
        row = self.lookup.get(record_id)
        if row and source_url(row):
            QDesktopServices.openUrl(QUrl(source_url(row)))

    def close(self, wait=True):
        if self.closed:
            return
        self.closed = True; self.stop.set()
        self.pool.shutdown(wait=wait, cancel_futures=True)


def create_engine(backend, qml_file='App.qml'):
    engine = QQmlApplicationEngine()
    provider = ThumbnailProvider()
    engine.addImageProvider('thumbnails', provider)
    engine.rootContext().setContextProperty('preview', backend)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / 'qml' / qml_file)))
    return engine, provider



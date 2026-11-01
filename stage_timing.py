"""Bounded, thread-safe per-step timing; no prompts, URLs or images are stored."""
from contextlib import contextmanager
from functools import wraps
import math
import threading
import time


STEPS = []
def _steps(prefix, title, children=()):
    STEPS.append({'key': prefix, 'label': title})
    STEPS.extend({'key': prefix + '.' + key, 'label': title + ' / ' + label} for key, label in children)


_steps('setup', 'Session setup', [('files', 'Scan source files'), ('client', 'Connect local client'), ('models', 'Discover available models'), ('reasoning', 'Discover reasoning capabilities'), ('folders', 'Scan destination folders'), ('settings', 'Build request / cache settings')])
_steps('source', 'Source preparation', [('hash', 'Hash image'), ('embedded', 'Read embedded tags'), ('cache.read', 'Identification cache read'), ('cache.write', 'Identification cache write'), ('refresh', 'Check fresh-search flag')])
for key, name in [('iqdb', 'IQDB'), ('saucenao', 'SauceNAO')]:
    _steps(key, name, [('rate_limit', 'Rate-limit pacing'), ('http', 'Upload + search request'), ('json', 'Decode JSON'), ('parse', 'Parse response')])
_steps('database', 'Database tag lookup')
_steps('booru_hash', 'Exact booru MD5 lookup', [(key, name) for key, name in [('danbooru', 'Danbooru'), ('gelbooru', 'Gelbooru'), ('yandere', 'Yande.re'), ('konachan', 'Konachan')]])
_steps('database.source_link', 'Exact source URL lookup')
for key, name in [('danbooru', 'Danbooru'), ('yandere', 'Yande.re'), ('gelbooru', 'Gelbooru'), ('konachan', 'Konachan'), ('zerochan', 'Zerochan'), ('pixiv', 'Pixiv')]:
    _steps('database.' + key, name + ' tag lookup', [('rate_limit', 'Rate-limit pacing'), ('http', 'Post request'), ('json', 'Decode JSON'), ('parse', 'Extract tags')])
_steps('localvision', 'Whole local-vision search (includes evidence)')
for key, name in [('web.lens', 'Lens / SerpApi'), ('web.guided', 'Tagger-guided Lens')]:
    _steps(key, name, [('cache.read', 'Evidence cache read'), ('cache.write', 'Evidence cache write'), ('account', 'API balance check'), ('prepare', 'Prepare upload'), ('upload', 'Upload image'), ('search', 'Lens search request'), ('json', 'Decode JSON'), ('parse', 'Extract web hints')])
_steps('web.cloud', 'Cloud Vision', [('prepare', 'Read / encode image'), ('search', 'Web-detection request'), ('json', 'Decode JSON'), ('parse', 'Extract web hints')])
_steps('tagger', 'Supporting classifier', [('cache.read', 'Prediction cache read'), ('cache.write', 'Prediction cache write'), ('startup', 'Start / connect service'), ('encode', 'Prepare request image'), ('http', 'Classifier HTTP request'), ('queue', 'Helper queue wait (reported)'), ('model_load', 'Load / download model (reported)'), ('preprocess', 'Helper preprocessing (reported)'), ('inference', 'Helper inference / pipeline (reported)'), ('postprocess', 'Helper label processing (reported)'), ('json', 'Decode JSON'), ('parse', 'Validate predictions')])
_steps('vision', 'Vision LLM', [('encode', 'Encode image'), ('request', 'Model request (per attempt)'), ('parse', 'Parse final tags')])
_steps('routing', 'Folder routing', [('plan', 'Build evidence shortlist'), ('prompt', 'Build / measure routing prompt'), ('request', 'Model request (per attempt)'), ('validate', 'Validate chosen candidate'), ('audit', 'Write decision log')])
_steps('routing.aliases', 'Booru identity aliases', [('cache.read', 'Shared alias cache read'), ('cache.write', 'Shared alias cache write'), ('request', 'Danbooru alias request'), ('parse', 'Validate active identity mappings')])
_steps('move', 'File move', [('directory', 'Prepare destination directory')])
_steps('unmatched', 'Unmatched outcome')
_steps('metadata', 'Metadata selection / writing', [('write', 'Write image tags')])
_steps('history', 'History record', [('persist', 'Update SQLite / move index'), ('notify', 'Queue UI update'), ('ui', 'Apply History model update (UI thread)')])
_steps('checkpoint', 'Batch checkpoint', [('begin', 'Save file manifest'), ('advance', 'Save completed position')])
_steps('network.other', 'Other HTTP request')
_steps('network.json', 'Other response JSON decoding')

_local = threading.local()


class StageTimings:
    def __init__(self, clock=time.perf_counter):
        self.clock = clock; self.lock = threading.Lock(); self.values = {}; self.active = {}; self.revision = 0

    def start(self, key):
        token = object()
        with self.lock:
            self.active[token] = (key, self.clock()); self.revision += 1
        return token

    def finish(self, token, failed=False, cancelled=False):
        with self.lock:
            entry = self.active.pop(token, None)
            if entry:
                key, started = entry
                self._record(key, max(0., self.clock() - started), failed, cancelled)

    def _record(self, key, duration, failed=False, cancelled=False):
        row = self.values.setdefault(key, {'count': 0, 'total': 0., 'last': 0., 'errors': 0, 'cancelled': 0, 'cached': 0})
        if cancelled: row['cancelled'] += 1
        else:
            row['count'] += 1; row['total'] += duration; row['last'] = duration; row['errors'] += int(failed)
        self.revision += 1

    def record(self, key, duration):
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration < 0: return
        with self.lock: self._record(key, float(duration))

    def cache_hit(self, key):
        with self.lock:
            row = self.values.setdefault(key, {'count': 0, 'total': 0., 'last': 0., 'errors': 0, 'cancelled': 0, 'cached': 0})
            row['cached'] += 1; self.revision += 1

    def snapshot(self):
        with self.lock:
            rows = {key: dict(value, average=value['total'] / value['count'] if value['count'] else 0., active=0, elapsed=0.) for key, value in self.values.items()}
            now = self.clock()
            for key, start in self.active.values():
                row = rows.setdefault(key, {'count': 0, 'average': 0., 'last': 0., 'errors': 0, 'cancelled': 0, 'cached': 0, 'active': 0, 'elapsed': 0.})
                row['active'] += 1; row['elapsed'] = max(row['elapsed'], now - start)
            return rows, self.revision, bool(self.active)

    def reset(self):
        with self.lock:
            if self.active: return False
            self.values.clear(); self.revision += 1
            return True


@contextmanager
def timing_scope(owner):
    previous = getattr(_local, 'collector', None)
    previous_owner = getattr(_local, 'owner', None)
    _local.collector = getattr(owner, '_stage_timings', previous)
    if hasattr(owner, 'skip_requested'): _local.owner = owner
    try: yield
    finally:
        _local.collector = previous
        _local.owner = previous_owner


def current_stage(): return getattr(_local, 'stage', '')


def current_owner(): return getattr(_local, 'owner', None)


class Span:
    def __init__(self): self.failed = False


@contextmanager
def timed_stage(owner, key):
    with timing_scope(owner):
        collector = getattr(_local, 'collector', None)
        previous = current_stage(); _local.stage = key
        token = collector.start(key) if collector else None
        span = Span()
        try: yield span
        except BaseException:
            span.failed = True
            raise
        finally:
            if collector: collector.finish(token, span.failed, bool(getattr(current_owner(), 'skip_requested', False)))
            _local.stage = previous


def timed_current(key): return timed_stage(None, key)


def profile(key):
    def decorate(function):
        @wraps(function)
        def wrapped(owner, *args, **kwargs):
            name = key(owner, *args, **kwargs) if callable(key) else key
            with timed_stage(owner, name): return function(owner, *args, **kwargs)
        return wrapped
    return decorate


def scoped(function):
    @wraps(function)
    def wrapped(owner, *args, **kwargs):
        with timing_scope(owner): return function(owner, *args, **kwargs)
    return wrapped


def cache_stage(image_hash):
    if image_hash.startswith('context:booru_alias:'): return 'routing.aliases.cache'
    if image_hash.startswith('context:tagger:'): return 'tagger.cache'
    if image_hash.startswith('context:serpapi:'): return 'web.guided.cache' if ':guided:' in image_hash else 'web.lens.cache'
    return 'source.cache'


def cache_hit(owner, key):
    collector = getattr(owner, '_stage_timings', getattr(_local, 'collector', None))
    if collector: collector.cache_hit(key)

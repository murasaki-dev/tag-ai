"""Live Qt frontend. Reuses the existing workflow/providers without creating Tk."""
from __future__ import annotations
import argparse
from contextlib import closing
import copy
import json
import math
from datetime import datetime
import os
from pathlib import Path
import queue
import re
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
import asyncio
import ssl
import httpx
import subprocess
import shutil
import logging
import logging.handlers
import faulthandler

# Window-only executables have no console streams. Third-party download and
# installer libraries still write progress/warnings to them.
for _stream_name in ('stdout', 'stderr'):
    if getattr(sys, _stream_name) is None:
        setattr(sys, _stream_name, open(os.devnull, 'w', encoding='utf-8'))

from PySide6.QtCore import Property, QLockFile, QTimer, Qt, QUrl, Signal, Slot, qInstallMessageHandler
import providers
from tagger import TAGGER_MODELS, TaggerManager, ModelStore, runtime_python, runtime_requirements
from openai import AsyncOpenAI
from stage_timing import StageTimings, STEPS, timed_stage, scoped, profile
from providers import ProviderMixin, tracker
from routing import (AUTO_MODEL, CONTEXT_METHODS, DATABASE_METHODS, FLOW_LABELS, MATCHING_MODES,
                     UNMATCHED_ACTIONS, RoutingMixin, available_model_ids, enabled_flow_methods,
                     enabled_search_methods, legacy_database_flow, legacy_search_pipeline,
                     normalize_named_flow, normalize_search_pipeline, validate_folder_aliases)
from storage import BatchSessionStore, PersistentResultCache, StorageMixin, safe_move, write_image_tags, stream_file_md5
from workflow import WorkflowMixin
from qt_core import HistoryBackend, create_engine, image_token, path_key, read_history, history_view, model_row
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuickControls2 import QQuickStyle
from shiboken6 import delete as delete_qt_object


HISTORY_COLORS = {'IQDB': '#59a980', 'Booru hash': '#59a980', 'SauceNAO': '#cb7474', 'Local Vision LLM': '#6299e0',
                  'Embedded Metadata': '#ab85d6', 'Unmatched': '#7d8794'}

runtime_logger = None
_crash_stream = None


def install_diagnostics(directory):
    global runtime_logger, _crash_stream
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    runtime_logger = logging.getLogger('tagai-runtime'); runtime_logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(directory / 'tagai-runtime.log', maxBytes=2_000_000, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(threadName)s %(message)s')); runtime_logger.addHandler(handler)
    _crash_stream = open(directory / 'tagai-crash.log', 'a', encoding='utf-8', buffering=1)
    _crash_stream.write('\nApplication started ' + datetime.now().astimezone().isoformat() + '\n')
    faulthandler.enable(file=_crash_stream, all_threads=True)
    sys.excepthook = lambda kind, value, trace: runtime_logger.error('Unhandled Python exception', exc_info=(kind, value, trace))
    threading.excepthook = lambda args: runtime_logger.error('Unhandled worker exception', exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    qInstallMessageHandler(lambda kind, context, message: runtime_logger.warning('Qt: %s', message))
    runtime_logger.info('Application startup')


def normalized_history_colors(value):
    value = value if isinstance(value, dict) else {}
    return {key: value[key].lower() if isinstance(value.get(key), str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value[key]) else default
            for key, default in HISTORY_COLORS.items()}


def normalized_config(value, directory):
    defaults = dict(exec_mode='Automated Full Folder Batch', source_folder='', dest_folder=str(Path(directory) / 'organized_library'),
        matching_mode='Exact', unmatched_action=UNMATCHED_ACTIONS[0], unmatched_folder='', engine='IQDB', tags='Auto (Highest Confidence)',
        fallback=1, vision_fallback=0, serpapi_enabled=0, gvision_enabled=0, skip_on_web_fail=1, scan_subfolders=0,
        use_embedded_tags=0, skip_no_embedded=0, embed_metadata=0, meta_artist=1, meta_series=1, meta_character=1, meta_general=1, meta_meta=0, booru_hash_lookup=True, booru_alias_lookup=True,
        lm_url='http://localhost:1234/v1', selected_model=AUTO_MODEL, vision_model='Same as routing', folder_aliases={}, vision_reasoning='Server default', routing_reasoning='Server default', routing_output_tokens=1024,
        context_mode='First available', tagger_enabled=False, tagger_model=TAGGER_MODELS[0], tagger_url='http://127.0.0.1:7861/tag',
        tagger_guided_lens=False, tagger_identity_threshold=.85, tagger_general_threshold=.5, tagger_backend='Managed by app', ui_theme='Dark')
    for name in ('serpapi_keys', 'gvision_keys', 'skeys', 'db_users', 'db_keys', 'gb_users', 'gb_keys'):
        defaults[name] = []
    config = {**defaults, **copy.deepcopy(value)}
    if type(config['routing_output_tokens']) is not int or not 1024 <= config['routing_output_tokens'] <= 16384:
        raise ValueError('Routing output budget must be between 1024 and 16384 tokens')
    if config['ui_theme'] not in ('Dark', 'Light'): config['ui_theme'] = 'Dark'
    if config['context_mode'] not in ('First available', 'Combine enabled'): config['context_mode'] = 'First available'
    if config['tagger_backend'] not in ('Managed by app', 'External service'): config['tagger_backend'] = 'Managed by app'
    for name in ('tagger_identity_threshold', 'tagger_general_threshold'):
        number = config[name]
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not 0 <= number <= 1:
            raise ValueError(name + ' must be a score threshold between 0 and 1')
    for key in ('vision_reasoning', 'routing_reasoning'):
        if config[key] not in providers.REASONING_MODES: config[key] = 'Server default'
    config['history_colors'] = normalized_history_colors(config.get('history_colors'))
    if config['matching_mode'] not in MATCHING_MODES: config['matching_mode'] = 'Exact'
    if config['unmatched_action'] not in UNMATCHED_ACTIONS: config['unmatched_action'] = UNMATCHED_ACTIONS[0]
    if config['engine'] not in ('IQDB', 'SauceNAO', 'Local Vision LLM'): config['engine'] = 'IQDB'
    config['search_pipeline'] = normalize_search_pipeline(config.get('search_pipeline', legacy_search_pipeline(config['engine'], config['fallback'], config['vision_fallback'])))
    flows = config.get('database_pipelines', {})
    config['database_pipelines'] = {engine: normalize_named_flow(flows.get(engine, legacy_database_flow(config['tags'])), DATABASE_METHODS) for engine in ('IQDB', 'SauceNAO')}
    context = config.get('context_pipeline', [{'method': 'serpapi', 'enabled': bool(config['serpapi_enabled'])}, {'method': 'gvision', 'enabled': bool(config['gvision_enabled'])}])
    config['context_pipeline'] = normalize_named_flow(context, CONTEXT_METHODS)
    active = enabled_search_methods(config['search_pipeline'])
    if active: config['engine'] = active[0]
    config['fallback'] = int('IQDB' in active and 'SauceNAO' in active)
    config['vision_fallback'] = int('Local Vision LLM' in active and active[0] != 'Local Vision LLM')
    for key in CONTEXT_METHODS:
        config[key + '_enabled'] = int(key in enabled_flow_methods(config['context_pipeline'], CONTEXT_METHODS))
    config['folder_aliases'] = validate_folder_aliases(config['folder_aliases'])
    for name in ('serpapi_keys', 'gvision_keys', 'skeys', 'db_users', 'db_keys', 'gb_users', 'gb_keys'):
        if not isinstance(config[name], list) or any(not isinstance(v, str) for v in config[name]):
            raise ValueError(f'{name} must be a list of strings')
    return config


def history_date(row):
    """Old records without dates retain SQLite order; never invent file dates."""
    try:
        value = float(row.get('recorded_at'))
        return value if math.isfinite(value) and not isinstance(row.get('recorded_at'), bool) else None
    except (ValueError, TypeError, OverflowError): return None


def ordered_history(rows, descending=True):
    direction = -1 if descending else 1
    def key(item):
        index, row = item; timestamp = history_date(row)
        return (timestamp is None, direction * (timestamp or 0), direction * index)
    return [row for index, row in sorted(enumerate(rows), key=key)]


def dated_model_row(row, revision):
    value = model_row(row, revision); timestamp = history_date(row)
    try: value['dateText'] = datetime.fromtimestamp(timestamp).astimezone().strftime('%Y-%m-%d %H:%M:%S') if timestamp is not None else 'Date not recorded'
    except (ValueError, OverflowError, OSError): value['dateText'] = 'Date not recorded'
    return value


def atomic_config(path, config):
    descriptor, temporary = tempfile.mkstemp(dir=Path(path).parent, suffix='.json')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(config, output, indent=2, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


class HistoryStore:
    """Serialize history/file actions. Keep updates in row order with UPSERT."""
    def __init__(self, path):
        self.path = Path(path); self.lock = threading.RLock()
        self.active_paths = set(); self.manually_changed = set(); self.manual_session_id = None
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE IF NOT EXISTS history(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        self.rows = {row['id']: row for row in read_history(path)}
        self.path_index = {}
        for row in self.rows.values(): self.path_index.setdefault(path_key(row['current_path']), set()).add(row['id'])

    def _write(self, changed, deleted=()):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            connection.executemany('DELETE FROM history WHERE id=?', [(identity,) for identity in deleted])
            connection.executemany('INSERT INTO history(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                [(row['id'], json.dumps(row, ensure_ascii=False)) for row in changed])

    def _relocate(self, source, target):
        changed = []
        before, after = path_key(source), path_key(target)
        if before != after:
            identities = self.path_index.pop(before, set())
            self.path_index.setdefault(after, set()).update(identities)
            for identity in identities:
                row = self.rows[identity]; row['current_path'] = target; changed.append(row)
        return changed

    def record(self, row):
        with self.lock:
            changed = self._relocate(row['original_path'], row['current_path'])
            self.rows[row['id']] = row; self.path_index.setdefault(path_key(row['current_path']), set()).add(row['id'])
            changed.append(row); self._write(changed)
            return copy.deepcopy(changed), []

    def action(self, identity, action, unmatched_folder):
        with self.lock:
            row = self.rows.get(identity)
            if not row: raise ValueError('This History entry is no longer available')
            current = row['current_path']
            if path_key(current) in self.active_paths:
                raise ValueError('This image is currently being processed; retry after it finishes')
            if not os.path.isfile(current): raise FileNotFoundError('The local image is no longer at its recorded location')
            if action == 'tags':
                if Path(current).suffix.lower() not in ('.png', '.jpg', '.jpeg'):
                    raise ValueError('Tag removal currently supports PNG and JPEG images')
                write_image_tags(current, ''); self.manually_changed.add(path_key(current)); return [], []
            if action == 'undo':
                if row.get('move_undone') or path_key(current) == path_key(row['original_path']): raise ValueError('This entry has no move to undo')
                destination = os.path.dirname(row['original_path']); os.makedirs(destination, exist_ok=True)
                target = safe_move(current, os.path.join(destination, os.path.basename(row['original_path'])))
                self.manually_changed.update((path_key(current), path_key(target)))
                changed = self._relocate(current, target)
                tracker.files_moved = max(0, tracker.files_moved - 1)
                attribute = 'unmatched_files_moved' if row.get('is_unmatched') else 'matched_files_moved'
                setattr(tracker, attribute, max(0, getattr(tracker, attribute) - 1))
                row.setdefault('reason_before_undo', row.get('reason', ''))
                row.update(current_path=target, is_unmatched=True, move_undone=True,
                           reason='Move undone · returned to source')
                if row not in changed: changed.append(row)
                self._write(changed)
                return copy.deepcopy(changed), []
            if action != 'unmatched': raise ValueError('Unknown History action')
            os.makedirs(unmatched_folder, exist_ok=True)
            target = safe_move(current, os.path.join(unmatched_folder, os.path.basename(current)))
            self.manually_changed.update((path_key(current), path_key(target)))
            changed = self._relocate(current, target)
            if not row.get('is_unmatched'): tracker.matched_files_moved = max(0, tracker.matched_files_moved - 1)
            if path_key(current) == path_key(row['original_path']): tracker.files_moved += 1
            tracker.unmatched_files_moved += 1
            row['current_path'] = target; row['is_unmatched'] = True; row['reason'] = 'Manually moved to Unmatched'
            row.pop('move_undone', None)
            if row not in changed: changed.append(row)
            self._write(changed); return copy.deepcopy(changed), []


class Value:
    """Frozen workflow input / UI-event sink, not a hidden Tk widget."""
    def __init__(self, value=None, event=None): self.value, self.event, self.image = value, event, None
    def get(self): return self.value
    def set(self, value): self.value = value
    def configure(self, **values):
        if self.event: self.event(values)


class SortRunner(WorkflowMixin, ProviderMixin, RoutingMixin, StorageMixin):
    """Bridge the existing tested sorting workflow to queued Qt events."""
    inputs = {'ent_lm': 'lm_url', 'opt_engine': 'engine', 'opt_tags': 'tags', 'opt_exec_mode': 'exec_mode',
        'opt_model': 'selected_model', 'opt_vision_model': 'vision_model', 'opt_matching_mode': 'matching_mode', 'opt_unmatched': 'unmatched_action',
        'switch_engine_fallback': 'fallback', 'switch_vision_fallback': 'vision_fallback', 'switch_use_embedded_tags': 'use_embedded_tags',
        'switch_skip_no_embedded': 'skip_no_embedded', 'switch_scan_subfolders': 'scan_subfolders', 'switch_skip_on_web_fail': 'skip_on_web_fail',
        'switch_serpapi': 'serpapi_enabled', 'switch_gvision': 'gvision_enabled', 'switch_embed_metadata': 'embed_metadata',
        'switch_meta_artist': 'meta_artist', 'switch_meta_series': 'meta_series', 'switch_meta_character': 'meta_character',
        'switch_meta_general': 'meta_general', 'switch_meta_meta': 'meta_meta'}
    keys = {'skey_entries': 'skeys', 'serpapi_entries': 'serpapi_keys', 'gvision_entries': 'gvision_keys',
        'db_user_entries': 'db_users', 'db_key_entries': 'db_keys', 'gb_user_entries': 'gb_users', 'gb_key_entries': 'gb_keys'}

    def __init__(self, backend, config, session, session_id):
        self.backend = backend; self.config_file = str(backend.config_path)
        self.session_store = backend.session_store; self.result_cache = backend.cache; self.store = backend.store
        config = copy.deepcopy(config)
        self._settings_lock = threading.Lock(); self._pending_settings = None
        self._applied_settings = copy.deepcopy(config)
        with self.store.lock:
            if self.store.manual_session_id != session_id:
                self.store.manually_changed.clear(); self.store.manual_session_id = session_id
        for attribute, key in self.inputs.items(): setattr(self, attribute, Value(config[key]))
        self.entry_values = {name: [(None, Value(value)) for value in config[key]] for name, key in self.keys.items()}
        self._run_reasoning_modes = {stage: config[stage + '_reasoning'] for stage in ('vision', 'routing')}
        self._run_routing_output_tokens = config['routing_output_tokens']
        self._run_booru_hash = bool(config['booru_hash_lookup'])
        self._run_booru_aliases = bool(config['booru_alias_lookup'])
        self._run_evidence_options = {key: config[key] for key in ('context_mode', 'tagger_enabled', 'tagger_model', 'tagger_url', 'tagger_guided_lens', 'tagger_identity_threshold', 'tagger_general_threshold', 'tagger_backend')}
        self._tagger_manager = backend.tagger_manager
        self._stage_timings = backend.timings
        self._active_path = None
        self._interrupt_on_stop = True
        self._run_session = copy.deepcopy(session); self._current_session_id = session_id
        self._run_paths = (config['source_folder'], config['dest_folder']); self._run_unmatched_path = config['unmatched_folder']
        self._run_aliases = config['folder_aliases']; self._run_search_pipeline = config['search_pipeline']
        self.database_pipelines = self._run_database_pipelines = config['database_pipelines']
        self.context_pipeline = self._run_context_pipeline = config['context_pipeline']
        self._preview_generation = 0; self.is_processing = True; self.stop_requested = self.skip_requested = self.file_in_progress = False
        for name, kind in (('lbl_run_status', 'status'), ('lbl_details', 'details'), ('lbl_match_thumb', 'clear_match'), ('lbl_history_counter', 'counter')):
            setattr(self, name, Value(event=lambda values, kind=kind: backend.runnerEvent.emit(kind, values)))
        for name in ('btn_start', 'btn_skip', 'btn_cancel'): setattr(self, name, Value())

    @scoped
    def run_workflow(self): return WorkflowMixin.run_workflow(self)

    def queue_settings(self, config):
        with self._settings_lock: self._pending_settings = copy.deepcopy(config)

    def settings_boundary(self, state, stage):
        with self._settings_lock:
            config = self._pending_settings; self._pending_settings = None
        if config is None or config == self._applied_settings: return state
        old = self._applied_settings
        changed = [key for key in config if config[key] != old.get(key)]
        new = dict(state)
        client = state['client']; replacement = None
        model_keys = ('lm_url', 'selected_model', 'vision_model', 'vision_reasoning', 'routing_reasoning')
        try:
            if any(key in changed for key in model_keys) or ('search_pipeline' in changed and 'Local Vision LLM' in enabled_search_methods(config['search_pipeline'])):
                if config['lm_url'] != old['lm_url']:
                    replacement = providers.local_model_client(config['lm_url']); client = replacement
                if isinstance(client, providers.REAL_OPENAI_CLASS):
                    response = providers.get_http_session().get(str(client.base_url).rstrip('/') + '/models',
                        headers={'Authorization': 'Bearer ' + client.api_key}, timeout=10)
                    response.raise_for_status()
                    ids = list(dict.fromkeys(item['id'] for item in response.json().get('data', []) if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']))
                else: ids = available_model_ids(client)
                if not ids: raise ValueError('Local server has no available models')
                route_model = ids[0] if config['selected_model'] == AUTO_MODEL else config['selected_model']
                vision_selection = config['vision_model']
                vision_model = route_model if vision_selection == 'Same as routing' else ids[0] if vision_selection == AUTO_MODEL else vision_selection
                if route_model not in ids: raise ValueError('Selected routing model is unavailable')
                modes = {name: config[name + '_reasoning'] for name in ('vision', 'routing')}
                if 'Local Vision LLM' not in enabled_search_methods(config['search_pipeline']): modes['vision'] = 'Server default'
                elif vision_model not in ids: raise ValueError('Selected vision model is unavailable')
                reasoning = providers.prepare_reasoning_requests(config['lm_url'], {'routing': route_model, 'vision': vision_model}, modes)
                new.update(client=client, model_id=route_model, vision_model_id=vision_model)
            else: reasoning = self._run_reasoning_requests
            folders = state['available_folders']
            if any(key in changed for key in ('dest_folder', 'scan_subfolders')):
                folders = []
                with os.scandir(config['dest_folder']) as entries:
                    for entry in entries:
                        if not entry.is_dir() or entry.name in ('Unmatched', 'Unsorted') or entry.name.startswith('.'): continue
                        folders.append(entry.name)
                        if config['scan_subfolders']:
                            with os.scandir(entry.path) as children:
                                folders.extend(entry.name + '/' + child.name for child in children if child.is_dir() and not child.name.startswith('.'))
        except Exception as exc:
            if replacement: replacement.close()
            self.log(f'[Live settings] Could not apply changes at {stage}: {exc}. Previous settings remain active; edit settings to retry.')
            return state
        for attribute, key in self.inputs.items():
            if key != 'exec_mode': setattr(self, attribute, Value(config[key]))
        self.entry_values = {name: [(None, Value(value)) for value in config[key]] for name, key in self.keys.items()}
        self._run_reasoning_modes = {name: config[name + '_reasoning'] for name in ('vision', 'routing')}
        self._run_reasoning_requests = reasoning; self._run_routing_output_tokens = config['routing_output_tokens']
        self._run_booru_aliases = bool(config['booru_alias_lookup'])
        if any(key in changed for key in ('db_users', 'db_keys')): self._alias_retry_after = 0
        self._run_booru_hash = bool(config['booru_hash_lookup'])
        self._run_evidence_options = {key: config[key] for key in self._run_evidence_options}
        self._run_aliases = copy.deepcopy(config['folder_aliases']); self._run_search_pipeline = copy.deepcopy(config['search_pipeline'])
        self.database_pipelines = self._run_database_pipelines = copy.deepcopy(config['database_pipelines'])
        self.context_pipeline = self._run_context_pipeline = copy.deepcopy(config['context_pipeline'])
        self._run_paths = (self._run_paths[0], config['dest_folder']); self._run_unmatched_path = config['unmatched_folder']
        new.update(dest_path=config['dest_folder'], available_folders=folders, routing_aliases=self._run_aliases,
                   search_pipeline=self._run_search_pipeline, matching_mode=config['matching_mode'],
                   tag_choice=config['tags'], engine_choice=config['engine'], use_embedded=bool(config['use_embedded_tags']),
                   skip_no_embedded=bool(config['skip_no_embedded']), saucenao_keys=[key.strip() for key in config['skeys'] if key.strip()])
        signature = dict(state['cache_settings'])
        signature['booru_hash_lookup'] = bool(config['booru_hash_lookup'])
        signature.update(search_pipeline=enabled_search_methods(config['search_pipeline']),
            database_pipelines={engine: enabled_flow_methods(value, DATABASE_METHODS) for engine, value in config['database_pipelines'].items()},
            context_pipeline=enabled_flow_methods(config['context_pipeline'], CONTEXT_METHODS), engine=config['engine'], tag_choice=config['tags'],
            engine_fallback=bool(config['fallback']), vision_fallback=bool(config['vision_fallback']), skip_on_web_fail=bool(config['skip_on_web_fail']),
            embedded=bool(config['use_embedded_tags']), skip_no_embedded=bool(config['skip_no_embedded']),
            serpapi_enabled=bool(config['serpapi_enabled']), gvision_enabled=bool(config['gvision_enabled']),
            model=new['vision_model_id'] if 'Local Vision LLM' in enabled_search_methods(config['search_pipeline']) else None,
            llm_url=config['lm_url'] if 'Local Vision LLM' in enabled_search_methods(config['search_pipeline']) else None)
        import hashlib
        for service, key in (('serpapi', 'serpapi_keys'), ('gvision', 'gvision_keys'), ('saucenao', 'skeys')):
            signature[service + '_keys_hash'] = hashlib.sha256('\n'.join(value.strip() for value in config[key] if service != 'saucenao' or value.strip()).encode()).hexdigest()
        for key in ('evidence_options', 'vision_reasoning', 'vision_response_version'): signature.pop(key, None)
        if 'Local Vision LLM' in enabled_search_methods(config['search_pipeline']):
            signature['vision_response_version'] = 2
            if self._run_evidence_options.get('tagger_enabled') or self._run_evidence_options.get('context_mode') == 'Combine enabled': signature['evidence_options'] = self._run_evidence_options
            if reasoning['vision']: signature['vision_reasoning'] = reasoning['vision']
        new['cache_settings'] = signature
        new['cache_signature'] = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        self._applied_settings = copy.deepcopy(config)
        if replacement:
            try: state['client'].close()
            except Exception: pass
        self.log('[Live settings] Applied at ' + stage + ': ' + ', '.join(changed) + '.', full_only=True)
        if any(key in changed for key in ('source_folder', 'exec_mode')):
            self.log('[Live settings] Source file queue and batch mode stay fixed until a new batch.')
        return new

    def claim_processing_file(self, path):
        with self.store.lock:
            key = path_key(path)
            if key in self.store.manually_changed: return False
            self.store.active_paths.add(key)
            self._active_path = path
            self.backend.runnerEvent.emit('image_active', {'path': path})
            return True

    def release_processing_file(self, path):
        with self.store.lock:
            self.store.active_paths.discard(path_key(path))
            self._active_path = None
            self.backend.runnerEvent.emit('image_inactive', {'path': path})

    def request_skip(self, expected_path):
        with self.store.lock:
            if not self.file_in_progress or not self._active_path or self._active_path != expected_path or self.skip_requested:
                return False
            self.skip_requested = True
            self.log('[User Action] Skip requested for ' + os.path.basename(self._active_path))
            return True

    def begin_file_mutation(self):
        with self.store.lock:
            if self.skip_requested or self.stop_requested: return False
            self.file_in_progress = False
            if self._active_path: self.backend.runnerEvent.emit('image_inactive', {'path': self._active_path})
            return True

    def _completion_request(self, client, **kwargs):
        # Test/legacy clients retain their synchronous contract. Real Qt calls
        # use cancellable HTTP so Skip need not wait for minutes of generation.
        if not isinstance(client, providers.REAL_OPENAI_CLASS):
            return client.chat.completions.create(**kwargs)
        async def request():
            async with AsyncOpenAI(base_url=str(client.base_url), api_key=client.api_key,
                                   timeout=client.timeout, max_retries=0) as connection:
                task = asyncio.create_task(connection.chat.completions.create(**kwargs))
                try:
                    while not task.done():
                        if self.stop_requested:
                            task.cancel(); await asyncio.gather(task, return_exceptions=True)
                            raise providers.ProcessingStopped()
                        if self.skip_requested:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            raise RuntimeError('Image skipped; local model request cancelled')
                        await asyncio.wait({task}, timeout=.05)
                    return await task
                finally:
                    if not task.done():
                        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        return asyncio.run(request())

    def _model_list(self, client):
        if not isinstance(client, providers.REAL_OPENAI_CLASS):
            return list(dict.fromkeys(item.id.strip() for item in client.models.list().data if isinstance(item.id, str) and item.id.strip()))
        response = providers.get_http_session().get(str(client.base_url).rstrip('/') + '/models',
            headers={'Authorization': 'Bearer ' + client.api_key}, timeout=10)
        response.raise_for_status()
        return list(dict.fromkeys(item['id'].strip() for item in response.json().get('data', [])
                                 if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'].strip()))

    def _http_request(self, session, prepared, **options):
        """Cancellable transport for prepared provider requests; no detached workers."""
        async def request():
            verify = options.get('verify', True)
            if verify is not False:
                verify = ssl.create_default_context(cafile=verify if isinstance(verify, str) else providers.requests.certs.where())
                if options.get('cert'):
                    cert = options['cert']
                    verify.load_cert_chain(*(cert if isinstance(cert, tuple) else (cert,)))
            timeout = options.get('timeout')
            if isinstance(timeout, tuple):
                timeout = httpx.Timeout(timeout[1], connect=timeout[0])
            proxy = providers.requests.utils.select_proxy(prepared.url, options.get('proxies', {}))
            async with httpx.AsyncClient(verify=verify, proxy=proxy, trust_env=session.trust_env,
                                         follow_redirects=options.get('allow_redirects', True), timeout=timeout) as connection:
                body = prepared.body
                if hasattr(body, 'read'): body = body.read()
                task = asyncio.create_task(connection.request(prepared.method, prepared.url, headers=dict(prepared.headers), content=body))
                try:
                    while not task.done():
                        if self.skip_requested or self.stop_requested:
                            task.cancel(); await asyncio.gather(task, return_exceptions=True)
                            if prepared.url.split('?')[0].endswith('/tag'):
                                self._tagger_manager.interrupt()
                            if self.stop_requested: raise providers.ProcessingStopped()
                            raise providers.ImageSkipped()
                        await asyncio.wait({task}, timeout=.05)
                    raw = await task
                    if self.stop_requested: raise providers.ProcessingStopped()
                    if self.skip_requested: raise providers.ImageSkipped()
                    response = providers.requests.Response()
                    response.status_code = raw.status_code; response._content = raw.content; response._content_consumed = True
                    response.headers = providers.requests.structures.CaseInsensitiveDict(raw.headers)
                    response.url = str(raw.url); response.reason = raw.reason_phrase; response.request = prepared
                    response.encoding = providers.requests.utils.get_encoding_from_headers(response.headers)
                    response.cookies.update(raw.cookies.jar); session.cookies.update(response.cookies)
                    return response
                finally:
                    if not task.done():
                        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        try: return asyncio.run(request())
        except httpx.TimeoutException as exc: raise providers.requests.Timeout(str(exc)) from exc
        except httpx.HTTPError as exc: raise providers.requests.ConnectionError(str(exc)) from exc

    def _read_widget(self, value): return value.get()
    def _entries(self, name): return self.entry_values[name]
    def after(self, delay, callback, *args): self.backend.dispatch.emit(delay, lambda: callback(*args))
    def save_settings(self): return True  # Already validated and saved before the worker started.
    def title(self, value): self.backend.runnerEvent.emit('title', {'text': value})
    def log(self, message, full_only=False): self.backend.log_queue.put((str(message), full_only))
    def update_metrics_display(self): self.backend.runnerEvent.emit('metrics', {})
    def update_progress_display(self, current, total): self.backend.runnerEvent.emit('progress', {'current': current, 'total': total})
    def load_local_thumbnail(self, path): self.backend.runnerEvent.emit('source', {'path': path})
    def update_match_thumbnail(self, url): self.backend.runnerEvent.emit('match', {'url': url})
    def _restore_session(self): self.backend.runnerEvent.emit('finished', {})
    def refresh_history_view(self): pass
    @profile('history')
    def add_history_item(self, original_path, current_path, filename, is_unmatched=False, match_thumb_url='', match_url='', reason=''):
        row = dict(id=uuid.uuid4().hex, original_path=os.path.abspath(original_path), current_path=os.path.abspath(current_path),
            filename=filename, is_unmatched=is_unmatched, match_thumb_url=match_thumb_url, match_url=match_url,
            image_hash=getattr(self, '_history_image_hash', ''), identification_method=getattr(self, '_history_identification_method', ''), from_cache=getattr(self, '_history_from_cache', False),
            reason=reason or ('No reliable image match' if is_unmatched else 'Matched and routed'), session_id=self._current_session_id, recorded_at=time.time())
        with self.store.lock:
            with timed_stage(self, 'history.persist'): changed, deleted = self.store.record(row)
            with timed_stage(self, 'history.notify'): self.backend.historyDelta.emit(changed, deleted)
        self.backend.runnerEvent.emit('source', {'path': current_path})


class LiveBackend(HistoryBackend):
    configurationChanged = Signal()
    liveChanged = Signal()
    liveLogsChanged = Signal()
    timingsChanged = Signal()
    timingRequested = Signal(str)
    dispatch = Signal(int, object)
    runnerEvent = Signal(str, object)
    historyDelta = Signal(object, object)
    modelsReady = Signal(int, object, str)
    quitRequested = Signal()
    pagedView = Signal(int, int, object, object, int, int, int, int)

    def __init__(self, directory, parent=None):
        self.directory = Path(directory).resolve(); self.directory.mkdir(parents=True, exist_ok=True)
        self.instance_lock = QLockFile(str(self.directory / 'tagai-qt.lock'))
        if not self.instance_lock.tryLock(0): raise RuntimeError('Tag-AI Qt is already using this data folder')
        super().__init__(self.directory, parent)
        self.config_path = self.directory / 'config.json'
        self.config = normalized_config(json.loads(self.config_path.read_text(encoding='utf-8')) if self.config_path.exists() else {}, self.directory)
        self.alias_draft = json.dumps(self.config['folder_aliases'], ensure_ascii=False, indent=2)
        self.cache = PersistentResultCache(str(self.directory / 'image_result_cache.sqlite3'))
        self.store = HistoryStore(self.database)
        self.session_store = BatchSessionStore(str(self.directory / 'batch_session.sqlite3'))
        self.pending = self.session_store.load()
        self.current_session_id = self.pending.get('session_id') if self.pending else None
        self.runner = None; self.worker = None
        self.timings = StageTimings(); self._timing_view = {}; self._timing_revision = -1; self._slowest_step = ''
        self._stage_timings = self.timings
        application_directory = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        self.tagger_manager = TaggerManager(application_directory, self.directory)
        self.tagger_models = ModelStore(application_directory / 'tagger-models')
        self._tagger_busy = False
        self._edit_future = None
        self._install_process = None
        self._tagger_message = 'Select a model, then download its weights. PixAI also needs the optional runtime.'
        self._active_image_path = None; self._skip_pending = False
        self.summary_lines = []
        self.log_queue = queue.SimpleQueue(); self._model_ids = []; self._model_generation = 0
        self._reasoning_caps = {}; self._reasoning_error = 'Refresh models to discover reasoning controls'
        self._window_title = 'Tag-AI Booru Image Sorter'
        self._progress = 0.; self._details = 'Ready to sort'; self._closing_requested = False; self._editing = False
        self.history_page = 0; self.history_pages = 1; self.history_page_size = 6
        self.history_order = 'Newest first'; self.history_total = 0
        self.model.incremental = True
        self.pagedView.connect(self._paged_viewed, Qt.ConnectionType.QueuedConnection)
        self.changed.connect(self.liveChanged.emit)
        self.configurationChanged.connect(self._queue_live_settings)
        self.logsChanged.connect(self.liveLogsChanged.emit)
        self.dispatch.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)
        self.runnerEvent.connect(self._runner_event, Qt.ConnectionType.QueuedConnection)
        self.historyDelta.connect(self._history_delta, Qt.ConnectionType.QueuedConnection)
        self.modelsReady.connect(self._models_ready, Qt.ConnectionType.QueuedConnection)
        self.log_timer = QTimer(self); self.log_timer.setInterval(80); self.log_timer.timeout.connect(self._drain_logs); self.log_timer.start()
        self.timing_timer = QTimer(self); self.timing_timer.setInterval(500); self.timing_timer.timeout.connect(self._publish_timings); self.timing_timer.start()
        self._status = 'Session ready to resume' if self.pending else 'Ready to sort'

    def _reasoning_capability(self, stage='routing'):
        selected = self.config.get('vision_model', 'Same as routing') if stage == 'vision' else self.config['selected_model']
        if selected == 'Same as routing': selected = self.config['selected_model']
        if selected == AUTO_MODEL: selected = self._model_ids[0] if self._model_ids else ''
        return self._reasoning_caps.get(selected, {})

    def _publish_timings(self):
        rows, revision, active = self.timings.snapshot()
        if active or revision != self._timing_revision:
            containers = {'setup', 'source', 'database', 'localvision', 'vision', 'routing', 'tagger', 'unmatched', 'history', 'metadata'}
            candidates = [(key, value) for key, value in rows.items() if value['count'] and key not in containers]
            if candidates:
                key, value = max(candidates, key=lambda item: item[1]['average'])
                label = next((step['label'] for step in STEPS if step['key'] == key), key)
                duration = f"{value['average'] * 1000:.1f} ms" if value['average'] < 1 else f"{value['average']:.2f} s"
                self._slowest_step = f"Largest completed average: {label} · {duration} ({value['count']} calls)"
            else: self._slowest_step = 'No completed timings yet'
            self._timing_view = rows; self._timing_revision = revision; self.timingsChanged.emit()

    @Property('QVariantMap', notify=timingsChanged)
    def stageTimings(self): return self._timing_view

    @Property(str, notify=timingsChanged)
    def slowestStep(self): return self._slowest_step or 'No completed timings yet'

    @Property('QVariantList', constant=True)
    def timingSteps(self): return STEPS

    @Slot(str)
    def showTimings(self, key): self.timingRequested.emit(key)

    @Slot()
    def resetTimings(self):
        if not self._running and self.timings.reset(): self._publish_timings()

    @Property('QStringList', notify=configurationChanged)
    def reasoningOptions(self):
        choices = ['Server default'] + [v.title() for v in self._reasoning_capability().get('options', [])]
        # Keep saved selections visible when a new server/model cannot verify them.
        return list(dict.fromkeys(choices + [self.config['vision_reasoning'], self.config['routing_reasoning']]))

    @Property('QStringList', notify=configurationChanged)
    def visionModelOptions(self): return list(dict.fromkeys(['Same as routing'] + self.modelOptions + [self.config['vision_model']]))

    @Property('QStringList', notify=configurationChanged)
    def taggerModelOptions(self): return list(dict.fromkeys(TAGGER_MODELS + ['custom-onnx', self.config['tagger_model']]))

    @Property(bool, notify=liveChanged)
    def taggerBusy(self): return self._tagger_busy

    @Property(str, notify=liveChanged)
    def taggerMessage(self): return self._tagger_message

    @Property(str, notify=configurationChanged)
    def taggerModelStatus(self):
        model = self.config['tagger_model']
        if model not in TAGGER_MODELS: return 'Custom model: manage files in your external service'
        return 'Weights downloaded locally' if self.tagger_models.ready(model) else 'Weights not downloaded · download before searching'

    @Slot(str)
    def manageTagger(self, action):
        if self._tagger_busy: return
        if action not in ('download', 'delete', 'runtime'): return
        if self._running and action in ('delete', 'runtime'):
            self._tagger_status('Stop the batch before deleting models or changing the tagger runtime'); return
        model = self.config['tagger_model']; manager = self.tagger_manager
        if action != 'runtime' and model not in TAGGER_MODELS:
            self._tagger_status('Custom model files are managed by your external service'); return
        self._tagger_busy = True
        self._tagger_status(('Installing PixAI runtime; this can take several minutes. See tagger-runtime-install.log.' if action == 'runtime'
                             else 'Tagger ' + action + ' in progress: ' + model))
        def work():
            try:
                if action == 'download':
                    last_progress = [0.0]
                    def progress(message):
                        if self.stop.is_set(): raise RuntimeError('Application is closing')
                        now = time.monotonic()
                        if now - last_progress[0] >= .5:
                            last_progress[0] = now
                            self.dispatch.emit(0, lambda value=str(message): self._tagger_progress(value))
                    self.tagger_models.download(model, progress=progress); text = 'Tagger model downloaded: ' + model
                elif action == 'delete':
                    manager.close()
                    self.tagger_models.delete(model); text = 'Deleted app-managed model weights: ' + model
                else:
                    manager.close()
                    root = manager.directory; python = runtime_python(root / '.tagger-env')
                    bundle = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).parent
                    with open(self.directory / 'tagger-runtime-install.log', 'ab') as output:
                        if not python.exists():
                            launcher = (shutil.which('py') if os.name == 'nt' else None) or shutil.which('python3') or shutil.which('python')
                            if not launcher: raise ValueError('Install Python first to create the optional PixAI runtime; EVA02 works without it')
                            self._run_tagger_install([launcher, '-m', 'venv', str(root / '.tagger-env')], stdout=output, stderr=output, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        flags = dict(stdout=output, stderr=output, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        base, optional = runtime_requirements(bundle / 'requirements.txt')
                        pytorch = [requirement for requirement in optional if requirement.split('>=')[0] in ('torch', 'torchvision')]
                        extras = [requirement for requirement in optional if requirement not in pytorch]
                        self._run_tagger_install([str(python), '-m', 'pip', 'install', '--index-url', 'https://download.pytorch.org/whl/cpu', *pytorch], **flags)
                        self._run_tagger_install([str(python), '-m', 'pip', 'install', *base, *extras], **flags)
                    text = 'PixAI CPU runtime installed. An externally started helper must be restarted separately.'
            except Exception as exc:
                text = 'Tagger ' + action + ' failed: ' + str(exc)
                if action == 'runtime':
                    path = self.directory / 'tagger-runtime-install.log'
                    if path.exists():
                        tail = path.read_text(encoding='utf-8', errors='replace')[-3000:]
                        errors = [line for line in tail.splitlines() if 'error' in line.lower()]
                        text += '\n' + ('\n'.join(errors[-3:]) or 'See ' + str(path) + ' for installation details.')
            self.dispatch.emit(0, lambda: self._tagger_managed_done(text, action))
        self.pool.submit(work)

    def _run_tagger_install(self, command, **options):
        if self.stop.is_set(): raise RuntimeError('Application is closing')
        process = self._install_process = subprocess.Popen(command, **options)
        try:
            self.tagger_manager.guard_process(process)
            while process.poll() is None:
                if self.stop.wait(.05):
                    TaggerManager.terminate_process(process)
                    raise RuntimeError('Installation cancelled because the app is closing; retry installation to finish setup')
            if process.returncode: raise subprocess.CalledProcessError(process.returncode, command)
        finally: self._install_process = None

    def _tagger_managed_done(self, text, action):
        if self.closed: return
        if action in ('delete', 'runtime'):
            self.tagger_manager = TaggerManager(self.tagger_manager.directory, self.directory)
        self._tagger_busy = False; self.configurationChanged.emit(); self._tagger_status(text)

    def _tagger_progress(self, message):
        if not self.closed and self._tagger_busy:
            self._tagger_message = message; self.changed.emit()

    @Slot()
    def checkTagger(self):
        if self._tagger_busy: return
        config = copy.deepcopy(self.config)
        url = self.config['tagger_url'].rsplit('/', 1)[0] + '/health'
        def check():
            try:
                def check_closing():
                    if self.stop.is_set(): raise RuntimeError('Application is closing')
                if config['tagger_backend'] == 'Managed by app': self.tagger_manager.ensure(config, check_cancel=check_closing)
                response = providers.get_http_session().get(url, timeout=5); response.raise_for_status()
                data = response.json()
                if data.get('service') != 'tag-ai-tagger': raise ValueError('Not a Tag-AI tagger service')
                model = config['tagger_model']
                if config['tagger_backend'] == 'Managed by app' and model in TAGGER_MODELS:
                    text = ('Tagger connected; weights ready. The model loads on the first image.' if self.tagger_models.ready(model)
                            else 'Tagger connected; weights missing. Click Download selected model below, then wait for completion.')
                else: text = 'Tagger connected; loaded model: ' + (data.get('loaded_model') or 'not loaded yet')
            except Exception as exc: text = 'Tagger connection failed: ' + str(exc)
            self.dispatch.emit(0, lambda: self._tagger_status(text))
        self.pool.submit(check)

    def _tagger_status(self, text):
        if not self.closed:
            self._tagger_message = text; self._status = text; self.log(text); self.changed.emit()

    @Property('QStringList', notify=configurationChanged)
    def visionReasoningOptions(self):
        return list(dict.fromkeys(['Server default'] + [v.title() for v in self._reasoning_capability('vision').get('options', [])] + [self.config['vision_reasoning']]))

    @Property(str, notify=configurationChanged)
    def reasoningSupport(self):
        descriptions = []
        for stage in ('vision', 'routing'):
            cap = self._reasoning_capability(stage)
            descriptions.append(stage.title() + ': ' + (', '.join(cap.get('options', [])) + ' (default: ' + str(cap.get('default', 'unknown')) + ')' if cap.get('options') else 'Server default only'))
        text = ' / '.join(descriptions)
        if self._reasoning_error: text += ' / ' + self._reasoning_error
        invalid = [stage for stage in ('vision', 'routing') if self.config[stage + '_reasoning'] != 'Server default' and self.config[stage + '_reasoning'].lower() not in self._reasoning_capability(stage).get('options', [])]
        if invalid: text += ' · Unverified saved selection: ' + ', '.join(invalid) + '. Refresh models or select Server default.'
        return text

    @Property('QVariantMap', notify=configurationChanged)
    def historyColors(self): return dict(self.config['history_colors'])

    @Slot(str, str)
    def setHistoryColor(self, method, color):
        if method not in HISTORY_COLORS or not re.fullmatch(r'#[0-9a-fA-F]{6}', color): return
        self.config['history_colors'][method] = color.lower(); self.configurationChanged.emit()

    @Slot()
    def resetHistoryColors(self):
        self.config['history_colors'] = dict(HISTORY_COLORS); self.configurationChanged.emit()

    @Property(str, notify=liveChanged)
    def windowTitle(self): return self._window_title

    @Property(int, notify=liveChanged)
    def historyPage(self): return self.history_page + 1
    @Property(int, notify=liveChanged)
    def historyPages(self): return self.history_pages
    @Property(int, notify=liveChanged)
    def historyTotal(self): return self.history_total
    @Property(str, notify=liveChanged)
    def historyOrder(self): return self.history_order

    @Slot(str)
    def setHistoryOrder(self, order):
        if order not in ('Newest first', 'Oldest first') or order == self.history_order: return
        self.history_order = order; self.history_page = 0; self._request_view(); self.changed.emit()

    @Slot(int)
    def changeHistoryPage(self, delta):
        page = max(0, min(self.history_pages - 1, self.history_page + delta))
        if page != self.history_page:
            self.history_page = page; self._request_view(); self.changed.emit()

    def setFilters(self, category, scope, attempts):
        if (category, scope, attempts) != (self.category, self.scope, self.attempts): self.history_page = 0
        super().setFilters(category, scope, attempts)

    @Property(bool, constant=True)
    def liveMode(self): return True
    @Property('QVariantMap', notify=configurationChanged)
    def configuration(self): return copy.deepcopy(self.config)
    @Property(str, notify=configurationChanged)
    def aliasesText(self): return self.alias_draft
    @Property('QVariantMap', notify=configurationChanged)
    def flows(self): return {'search': self.config['search_pipeline'], 'IQDB': self.config['database_pipelines']['IQDB'], 'SauceNAO': self.config['database_pipelines']['SauceNAO'], 'context': self.config['context_pipeline']}
    @Property('QVariantMap', constant=True)
    def flowLabels(self): return FLOW_LABELS
    @Property('QStringList', notify=configurationChanged)
    def modelOptions(self): return list(dict.fromkeys([AUTO_MODEL] + self._model_ids + [self.config['selected_model']]))
    @Property(bool, notify=liveChanged)
    def canResume(self): return bool(self.pending) and not self._running
    @Property(str, notify=liveChanged)
    def sessionText(self):
        return f"Stopped session · {self.pending['completed']}/{len(self.pending['files'])} completed" if self.pending else ''
    @Property(str, notify=liveChanged)
    def details(self): return self._details
    @Property(float, notify=liveChanged)
    def runProgress(self): return self._progress
    @Property(bool, notify=liveChanged)
    def editing(self): return self._editing
    @Property(str, notify=liveChanged)
    def actualUsage(self):
        lines = [f'Files moved this app run: {tracker.files_moved}', f'IQDB: {tracker.iqdb_calls} in current rate window', '']
        for label, service, key in (('SerpApi', 'serpapi', 'serpapi_keys'), ('Cloud Vision', 'gvision', 'gvision_keys'), ('SauceNAO', 'saucenao', 'skeys'), ('Danbooru', 'danbooru', 'db_keys'), ('Gelbooru', 'gelbooru', 'gb_keys')):
            lines.append(label)
            keys = list(dict.fromkeys(v.strip() for v in self.config[key] if v.strip()))
            if not keys: lines.append('  No keys configured')
            for index, value in enumerate(keys, 1):
                row = tracker.key_snapshot(service, value)
                lines.append(f"  Key {index}: {row.get('requests', 0)} requests · {row.get('errors', 0)} failed HTTP calls")
                if service == 'serpapi': lines.append(f"  {row.get('uploads', 0)} uploads · {row.get('checks', 0)} balance checks")
                quota = row.get('quota', {})
                if quota and time.time() - quota.get('observed_at', 0) < 86400:
                    if quota.get('total_searches_left') is not None: lines.append(f"  Last server balance: {quota['total_searches_left']} searches left")
                    elif quota.get('long_remaining') is not None: lines.append(f"  Last server quota: {quota.get('short_remaining', '?')} short-window / {quota['long_remaining']} daily left")
                    else: lines.append('  Server quota not reported')
                else: lines.append('  Server quota unknown · local counts are not billing usage')
            lines.append('')
        return '\n'.join(lines)

    @Slot(int, object)
    def _dispatch(self, delay, callback):
        if not self.closed: QTimer.singleShot(delay, lambda: callback() if not self.closed else None)

    def _drain_logs(self):
        messages = []
        for _ in range(100):
            try:
                message, detailed = self.log_queue.get_nowait(); messages.append(message)
                if not detailed: self.summary_lines = (self.summary_lines + [message])[-250:]
            except queue.Empty: break
        if messages:
            if runtime_logger:
                for message in messages: runtime_logger.info('%s', message)
            self._log_lines = (self._log_lines + messages)[-250:]; self.logsChanged.emit()

    @Property(str, notify=liveLogsChanged)
    def summaryLog(self): return '\n'.join(self.summary_lines)

    @Slot(str, 'QVariant')
    def setSetting(self, key, value):
        if key not in self.config or self.config[key] == value: return
        self.config[key] = value
        if key == 'lm_url':
            self._model_generation += 1; self._model_ids = []; self._reasoning_caps = {}
            self._reasoning_error = 'Refresh models to discover reasoning controls for this server'
        self.configurationChanged.emit()

    @Slot(str, QUrl)
    def setPath(self, key, url):
        if key in ('source_folder', 'dest_folder', 'unmatched_folder'):
            self.setSetting(key, url.toLocalFile())

    @Slot(str)
    def setAliases(self, text):
        self.alias_draft = text
        try:
            self.config['folder_aliases'] = validate_folder_aliases(json.loads(text))
            self._queue_live_settings()
        except (ValueError, TypeError): pass

    def _queue_live_settings(self):
        if not self.runner or not self._running: return
        try: self.runner.queue_settings(normalized_config(self.config, self.directory))
        except (ValueError, TypeError): return

    @Slot(str, int, str)
    def editKey(self, key, index, value):
        if key in SortRunner.keys.values() and 0 <= index < len(self.config[key]):
            self.config[key][index] = value; self._queue_live_settings()

    @Slot(str)
    def addKey(self, key):
        if key in SortRunner.keys.values(): self.config[key].append(''); self.configurationChanged.emit()

    @Slot(str, int)
    def removeKey(self, key, index):
        if key in SortRunner.keys.values() and 0 <= index < len(self.config[key]): self.config[key].pop(index); self.configurationChanged.emit()

    def _flow(self, key):
        if key == 'search': return self.config['search_pipeline']
        if key == 'context': return self.config['context_pipeline']
        return self.config['database_pipelines'][key]

    @Slot(str, str, bool)
    def toggleFlow(self, key, method, enabled):
        try:
            for node in self._flow(key):
                if node['method'] == method: node['enabled'] = enabled
            self.config = normalized_config(self.config, self.directory)
            self._queue_live_settings()
            QTimer.singleShot(0, self, self.configurationChanged.emit)
        except (KeyError, ValueError) as exc: self.log(str(exc))

    @Slot(str, str, int)
    def moveFlow(self, key, method, target):
        try:
            flow = self._flow(key); index = next(i for i, n in enumerate(flow) if n['method'] == method)
            flow.insert(max(0, min(target, len(flow) - 1)), flow.pop(index))
            self.config = normalized_config(self.config, self.directory)
            self._queue_live_settings()
            QTimer.singleShot(0, self, self.configurationChanged.emit)
        except (KeyError, ValueError, StopIteration) as exc: self.log(str(exc))

    @Slot(result=bool)
    def saveSettings(self):
        try:
            value = copy.deepcopy(self.config); value['folder_aliases'] = validate_folder_aliases(json.loads(self.alias_draft))
            value = normalized_config(value, self.directory); atomic_config(self.config_path, value); self.config = value
            self._status = 'Settings saved · processing changes apply between steps'; self.configurationChanged.emit(); self.changed.emit(); return True
        except Exception as exc:
            self._status = f'Settings not saved: {exc}'; self.log(self._status); self.changed.emit(); return False

    @Slot(QUrl)
    def importSettings(self, url):
        try:
            value = normalized_config(json.loads(Path(url.toLocalFile()).read_text(encoding='utf-8')), self.directory)
            self.config = value; self.alias_draft = json.dumps(value['folder_aliases'], indent=2, ensure_ascii=False)
            self.configurationChanged.emit(); self._status = 'Settings imported · Save settings to keep them'; self.changed.emit()
        except Exception as exc: self.log(f'Could not import settings: {exc}')

    @Slot(QUrl)
    def exportSettings(self, url):
        try:
            if self.saveSettings(): atomic_config(Path(url.toLocalFile()), self.config)
        except Exception as exc: self.log(f'Could not export settings: {exc}')

    @Slot()
    def refreshModels(self):
        self._model_generation += 1; generation = self._model_generation; address = self.config['lm_url']
        self._status = 'Querying local server models…'; self.changed.emit()
        def query():
            try:
                with providers.local_model_client(address, timeout=10, max_retries=0) as client: ids = available_model_ids(client)
                error = ''
            except Exception as exc: ids, error = [], str(exc)
            try: caps, capability_error = providers.local_reasoning_capabilities(address), ''
            except Exception as exc: caps, capability_error = {}, f'Reasoning controls unavailable: {exc}'
            if not self.closed: self.modelsReady.emit(generation, {'ids': ids, 'caps': caps, 'capability_error': capability_error}, error)
        self.pool.submit(query)

    @Slot(int, object, str)
    def _models_ready(self, generation, ids, error):
        if self.closed or generation != self._model_generation: return
        if isinstance(ids, dict):
            self._reasoning_caps = ids.get('caps', {}); self._reasoning_error = ids.get('capability_error', '')
            ids = ids['ids']
        self._model_ids = ids
        self._status = f'Could not query models: {error}' if error else f'{len(ids)} model(s) available'
        if not error and self.config['selected_model'] != AUTO_MODEL and self.config['selected_model'] not in ids:
            self._status += ' · selected model is unavailable'
        self.configurationChanged.emit(); self.changed.emit()

    @Slot()
    def startSorting(self): self._start(False, False)
    @Slot()
    def resumeSorting(self): self._start(True, False)
    @Slot()
    def restartSorting(self): self._start(False, True)

    def _start(self, resume, restart):
        if self._running or self._editing or self._loading or self._tagger_busy: return
        if self.pending and not (resume or restart): return
        if (resume or restart) and self.pending:
            self.config.update(source_folder=self.pending['source'], dest_folder=self.pending['destination'], exec_mode=self.pending['mode'])
            self.configurationChanged.emit()
        if not self.config['source_folder'] or not self.config['dest_folder']:
            self._status = 'Choose a source and destination first'; self.changed.emit(); return
        if not self.saveSettings(): return
        session = self.pending if resume else None
        identity = session.get('session_id') if session else None
        identity = identity or uuid.uuid4().hex
        if session: self.session_store.set_session_id(identity)
        self.current_session_id = identity
        self._request_view()
        self.runner = SortRunner(self, self.config, session, identity)
        self._running = True; self._progress = 0; self._status = 'Sorting in the background'; self.changed.emit()
        self.worker = threading.Thread(target=self.runner.run_workflow, daemon=True, name='qt-sort-workflow'); self.worker.start()

    @Slot()
    def stopSorting(self):
        if self.runner and self.runner.is_processing:
            self.runner.stop_requested = True; self._status = 'Stopping · cancelling active requests'; self.changed.emit()

    @Slot()
    def skipSorting(self):
        if self.runner and self.runner.request_skip(self._active_image_path):
            self._skip_pending = True
            self._status = 'Skipping current image · cancelling active requests'
            self.changed.emit()

    @Property(bool, notify=liveChanged)
    def canSkip(self): return bool(self._running and self._active_image_path and not self._skip_pending)

    @Property(bool, notify=liveChanged)
    def skipPending(self): return self._skip_pending

    @Slot(str, object)
    def _runner_event(self, kind, value):
        if self.closed: return
        if kind == 'status':
            if not self._skip_pending: self._status = value.get('text', self._status)
        elif kind == 'title': self._window_title = value['text']
        elif kind == 'details': self._details = value.get('text', self._details)
        elif kind == 'clear_match': self._latest_match = ''
        elif kind == 'source': self._latest_local = image_token('local', value['path'], self.generation)
        elif kind == 'match': self._latest_match = image_token('remote', value['url'], self.generation)
        elif kind == 'progress':
            self._window_title = f"Tag-AI {value['current']}/{value['total']}"
            self._progress = value['current'] / value['total']; self._details = f"Processing {value['current']}/{value['total']} images"
        elif kind == 'image_active':
            self._active_image_path = value['path']; self._skip_pending = False
            self._status = 'Sorting in the background'
        elif kind == 'image_inactive':
            if self._active_image_path == value['path']:
                self._active_image_path = None; self._skip_pending = False
        elif kind == 'finished':
            self._active_image_path = None; self._skip_pending = False
            self._running = False; self.pending = self.session_store.load()
            if self.runner and self.runner._workflow_error: self._status = 'Batch could not finish: ' + self.runner._workflow_error
            elif self.pending: self._status = 'Session stopped · Resume or Start over'
            self._drain_logs()
            if self._closing_requested: self.quitRequested.emit()
        elif kind == 'edit_done':
            self._editing = False; self._status = value['text']; self.log(self._status)
        self.changed.emit()

    @Slot(object, object)
    @profile('history.ui')
    def _history_delta(self, changed, deleted):
        if self.closed: return
        by_id = {row['id']: row for row in self.raw}
        for identity in deleted: by_id.pop(identity, None)
        by_id.update((row['id'], row) for row in changed)
        self.raw = list(by_id.values()); self.lookup = by_id; self._request_view(); self.changed.emit()

    # Inherited Qt slots deliberately retain their base metaobject registration.
    def _loaded(self, generation, rows, error):
        if generation != self.generation or self.closed: return
        # A batch may have added rows since the SQLite snapshot started.
        with self.store.lock: self.raw = copy.deepcopy(list(self.store.rows.values()))
        self.lookup = {row['id']: row for row in self.raw}; self._loading = False
        self._status = error or ('Sorting in the background' if self._running else ('Session ready to resume' if self.pending else 'Ready to sort'))
        self.log(self._status); self._request_view(); self.changed.emit()

    def _request_view(self):
        self.view_generation += 1
        generation, view = self.generation, self.view_generation
        rows = tuple(self.raw); category, scope, attempts = self.category, self.scope, self.attempts
        session = self.current_session_id; requested_page = self.history_page
        page_size, descending = self.history_page_size, self.history_order == 'Newest first'
        def build():
            selected, counts, recorded = history_view(rows, category, scope, attempts, session_id=session)
            selected = ordered_history(selected, descending); total = len(selected)
            pages = max(1, (total + page_size - 1) // page_size); page = min(requested_page, pages - 1)
            visible = [dated_model_row(row, generation) for row in selected[page * page_size:(page + 1) * page_size]]
            if not self.closed: self.pagedView.emit(generation, view, visible, counts, recorded, total, page, pages)
        self.pool.submit(build)

    @Slot(int, int, object, object, int, int, int, int)
    def _paged_viewed(self, generation, view, rows, counts, recorded, total, page, pages):
        if generation != self.generation or view != self.view_generation or self.closed: return
        self.history_page, self.history_pages, self.history_total = page, pages, total
        self.model.replace(rows); self._counts = counts
        self._summary = f'{total} results · {recorded} recorded attempts · {len(rows)} shown'
        self.changed.emit()

    def loadSaved(self):
        running = self._running; self.history_page = 0
        super().loadSaved(); self._running = running

    def openHistory(self, url):
        self._status = 'Live History uses the current data folder. Use the separate preview to browse other databases.'; self.changed.emit()

    @Slot(str, str)
    def historyAction(self, identity, action):
        if self._editing: return
        if runtime_logger: runtime_logger.info('History action requested: %s record=%s', action, identity)
        destination = self.config['unmatched_folder'] or os.path.join(self.config['dest_folder'], 'Unmatched')
        self._editing = True; self.changed.emit()
        def edit():
            try:
                with self.store.lock:
                    if action == 'cache':
                        row = self.store.rows.get(identity)
                        if not row: raise ValueError('This History entry is no longer available')
                        current = row['current_path']
                        if path_key(current) in self.store.active_paths:
                            raise ValueError('This image is currently being processed; retry after it finishes')
                        current_hash = stream_file_md5(current)
                        hashes = {current_hash}
                        for record_id in self.store.path_index.get(path_key(current), ()):
                            hashes.add(self.store.rows[record_id].get('image_hash', ''))
                        self.cache.forget_image(hashes, current_hash)
                    else:
                        changed, deleted = self.store.action(identity, action, destination)
                        self.historyDelta.emit(changed, deleted)
                message = 'Cached searches cleared for this image; next attempt will search again (including bypassing embedded tags).' if action == 'cache' else 'History action complete' 
            except Exception as exc: message = f'History action failed: {exc}'
            self.runnerEvent.emit('edit_done', {'text': message})
        self._edit_future = self.pool.submit(edit)

    @Slot()
    def clearImageCache(self):
        if not self._running and not self._editing: self.cache.clear(); self.log('Image search/context cache cleared')

    @Slot()
    def clearHistory(self):
        if self._running or self._editing: return
        with self.store.lock:
            deleted = list(self.store.rows); self.store._write([], deleted); self.store.rows.clear(); self.store.path_index.clear()
        self._history_delta([], deleted); self.log('History cleared; images were not moved or modified')

    @Slot()
    def requestClose(self):
        self._closing_requested = True
        self.stop.set()
        if self._running: self.stopSorting()
        else: self.quitRequested.emit()

    def close(self):
        if self.closed: return
        self.stop.set()
        if self.runner: self.runner.stop_requested = True
        if self.worker and self.worker.is_alive(): self.worker.join()
        # History edits can contain file writes. Finish them before releasing data.
        if self._edit_future:
            self._edit_future.result()
        self.tagger_manager.close()
        self.timing_timer.stop()
        self.log_timer.stop(); super().close(wait=False); self.cache.close(); self.instance_lock.unlock()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    parser.add_argument('--data-dir', type=Path, default=base)
    parser.add_argument('--software', action='store_true')
    options = parser.parse_args(argv)
    install_diagnostics(options.data_dir)
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1]); QQuickStyle.setStyle('Fusion')
    if options.software:
        from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Software)
    backend = LiveBackend(options.data_dir)
    engine, provider = create_engine(backend, 'App.qml')
    if not engine.rootObjects():
        backend.close(); provider.close(); delete_qt_object(engine); return 1
    backend.quitRequested.connect(app.quit)
    QTimer.singleShot(0, backend.loadSaved)
    QTimer.singleShot(0, backend.refreshModels)
    try: return app.exec()
    finally:
        backend.close()
        for window in engine.rootObjects(): window.setVisible(False)
        provider.close(); delete_qt_object(engine)


def entrypoint():
    if '--tagger-server' in sys.argv:
        sys.argv.remove('--tagger-server')
        try:
            from tagger import main as serve
            return serve()
        except Exception:
            import traceback
            directory = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
            with open(directory / 'tagger-startup-error.log', 'a', encoding='utf-8') as stream:
                stream.write(traceback.format_exc())
            return 1
    result = main()
    # All mutations, databases and owned helpers are closed by main's finally.
    # Read-only executor jobs (e.g. a stalled thumbnail socket) must not keep the
    # windowless process alive in Python's executor atexit joins.
    watchdog = threading.Timer(2, lambda: os._exit(result or 0))
    watchdog.daemon = True; watchdog.start()
    return result


if __name__ == '__main__': sys.exit(entrypoint())

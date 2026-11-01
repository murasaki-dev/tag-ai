"""Optional classifier service and its small, runtime-independent client contract.

Run with a Python environment containing the tagger dependencies in requirements.txt. Heavy inference
libraries are imported only in the service, never in the desktop UI process.
"""
import argparse
import base64
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import math
from pathlib import Path
import threading
import subprocess
import sys
import secrets
import hashlib
import shutil
import os
import time
import weakref
from urllib.parse import urlsplit

from PIL import Image, ImageOps

EVA_MODEL = 'SmilingWolf/wd-eva02-large-tagger-v3'
PIXAI_MODEL = 'pixai-labs/pixai-tagger-v1.0'
TAGGER_MODELS = [EVA_MODEL, PIXAI_MODEL]


def runtime_python(environment, platform=None):
    return Path(environment) / ('Scripts/python.exe' if (platform or os.name) == 'nt' else 'bin/python')


def runtime_requirements(path):
    base, optional = [], []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line.startswith('# pixai: '): optional.append(line.removeprefix('# pixai: ').strip())
        elif line.rstrip().endswith('# tagger'): base.append(line.split('#', 1)[0].strip())
    if not base or not optional: raise ValueError('Tagger runtime requirements are missing from requirements.txt')
    return base, optional


class ModelStore:
    """Explicitly managed preset weights; never remove a shared Hugging Face cache."""
    def __init__(self, root): self.root = Path(root).resolve()

    def path(self, model):
        if model not in TAGGER_MODELS: raise ValueError('Model management supports the EVA02 and PixAI presets; custom models remain externally managed')
        return self.root / hashlib.sha256(model.encode()).hexdigest()[:20]

    def ready(self, model): return (self.path(model) / '.ready').is_file()

    def download(self, model, progress=None):
        from huggingface_hub import hf_hub_download, snapshot_download
        from huggingface_hub.utils import tqdm
        class DownloadProgress(tqdm):
            def display(self, *args, **kwargs):
                if progress:
                    # Hugging Face also uses this for aggregate byte progress.
                    progress(str(self))
        folder = self.path(model); folder.mkdir(parents=True, exist_ok=True)
        if model == EVA_MODEL:
            for name in ('model.onnx', 'selected_tags.csv'):
                if progress: progress('Downloading ' + name)
                hf_hub_download(model, name, local_dir=str(folder))
        else:
            if progress: progress('Fetching PixAI file list and weights…')
            snapshot_download(model, local_dir=str(folder), tqdm_class=DownloadProgress)
        (folder / '.ready').write_text(model, encoding='utf-8')

    def delete(self, model):
        folder = self.path(model).resolve()
        if folder.parent != self.root or folder == self.root: raise ValueError('Invalid managed model directory')
        if folder.exists(): shutil.rmtree(folder)


class WindowsProcessJob:
    """The kernel terminates owned children even if the desktop app crashes."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                        ('max_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', IoCounters), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]
        api = self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]; api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]; api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]; api.CloseHandle.restype = wintypes.BOOL
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
        self.finalizer = weakref.finalize(self, api.CloseHandle, self.handle)
        limits = ExtendedLimits(); limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error()); self.close(); raise error

    def add(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self): self.finalizer()


class TaggerManager:
    """Own only the service process started by this app; reuse external services."""
    def __init__(self, directory, log_directory=None):
        self.directory = Path(directory)
        self.log_directory = Path(log_directory or directory)
        self.lock = threading.Lock(); self.process = None; self.token = None
        self.health_url = None; self.closed = False
        self._job_lock = threading.Lock(); self._process_job = None

    def guard_process(self, process):
        if os.name != 'nt': return
        try:
            with self._job_lock:
                if self._process_job is None: self._process_job = WindowsProcessJob()
                self._process_job.add(process)
        except Exception:
            self.terminate_process(process)
            raise

    def ensure(self, options, check_cancel=None):
        import requests
        parts = urlsplit(options['tagger_url'])
        if parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost') or parts.path != '/tag':
            raise ValueError('Managed tagging needs http://127.0.0.1:PORT/tag; choose External service for other endpoints')
        health = options['tagger_url'].rsplit('/', 1)[0] + '/health'
        with self.lock:
            if check_cancel: check_cancel()
            if self.closed: raise RuntimeError('Tagger manager is closing')
            try:
                reply = requests.get(health, timeout=1); reply.raise_for_status(); data = reply.json()
            except requests.RequestException: data = None
            if data is not None:
                if data.get('service') != 'tag-ai-tagger': raise ValueError('Tagger port is occupied by another service')
                return self._validate_runtime(data, options['tagger_model'])
            if options['tagger_model'] not in TAGGER_MODELS:
                raise ValueError('For a custom model, configure its helper and select External service')
            if self.process and self.process.poll() is None:
                if self.health_url != health:
                    raise ValueError('The managed tagger is running at a different endpoint; restart the app to change its port')
                return self._wait_ready(health, options, check_cancel)
            self.token = secrets.token_urlsafe(24)
            port = parts.port or 80
            # PixAI's larger runtime can remain in an optional environment.
            optional_python = runtime_python(self.directory / '.tagger-env')
            helper = self.directory / 'tagger.py'
            if not helper.exists() and getattr(sys, 'frozen', False): helper = Path(sys._MEIPASS) / 'tagger.py'
            if options['tagger_model'] == PIXAI_MODEL and optional_python.exists() and helper.exists():
                command = [str(optional_python), '-u', str(helper)]
            elif getattr(sys, 'frozen', False):
                command = [sys.executable, '--tagger-server']
            else:
                command = [sys.executable, str(helper)]
            command += ['--port', str(port), '--owner-token', self.token, '--log-dir', str(self.log_directory)]
            command += ['--model-dir', str(self.directory / 'tagger-models')]
            self.log_directory.mkdir(parents=True, exist_ok=True)
            with open(self.log_directory / 'tagger-helper.log', 'ab') as out, open(self.log_directory / 'tagger-helper-errors.log', 'ab') as err:
                self.process = subprocess.Popen(command, cwd=self.directory, stdout=out, stderr=err,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.guard_process(self.process)
            self.health_url = health
            return self._wait_ready(health, options, check_cancel)

    def _wait_ready(self, health, options, check_cancel):
        import requests
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.closed: raise RuntimeError('Tagger is closing')
            if check_cancel:
                try: check_cancel()
                except BaseException:
                    self.terminate_process(self.process)
                    self.process = None
                    raise
            if self.process.poll() is not None: raise RuntimeError('Tagger startup failed; see tagger-helper-errors.log')
            try:
                reply = requests.get(health, timeout=.5); reply.raise_for_status(); data = reply.json()
                if data.get('service') == 'tag-ai-tagger': return self._validate_runtime(data, options['tagger_model'])
            except requests.RequestException: pass
            time.sleep(.1)
        raise RuntimeError('Tagger is still starting; retry or see tagger-helper-errors.log')

    @staticmethod
    def _validate_runtime(data, model):
        runtime = data.get('runtimes')
        if model in TAGGER_MODELS and not data.get('explicit_downloads'):
            raise ValueError('An older tagger helper is running at this endpoint. Restart that helper with the updated app, or choose a different local port. Searches require explicitly downloaded weights.')
        if model == PIXAI_MODEL and (not isinstance(runtime, dict) or not runtime.get('transformers')):
            raise ValueError('PixAI needs the optional PyTorch/Transformers runtime. Use Install PixAI runtime in Settings. An older/external helper must be restarted with that runtime or use a different endpoint; EVA02 uses the built-in ONNX runtime.')
        if runtime is not None:
            required = 'transformers' if model == PIXAI_MODEL else 'onnx'
            if not runtime.get(required):
                raise ValueError('PixAI needs its optional runtime; use Install PixAI runtime in Settings' if required == 'transformers' else 'ONNX classifier runtime is unavailable')
        return data

    @staticmethod
    def terminate_process(process):
        if not process or process.poll() is not None: return
        # Only processes created and retained by this app reach this function.
        # Windows venv launchers have a child interpreter: terminate the tree.
        if sys.platform == 'win32':
            try:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=2)
            except subprocess.TimeoutExpired: process.kill()
        else: process.terminate()
        try: process.wait(timeout=.5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=.5)

    def interrupt(self):
        with self.lock:
            self.terminate_process(self.process)
            self.process = None

    def close(self):
        self.closed = True
        with self.lock: self.terminate_process(self.process)
        with self._job_lock:
            if self._process_job: self._process_job.close(); self._process_job = None


def normalize_predictions(value):
    """Validate scores and bound evidence, without treating scores as certainty."""
    if not isinstance(value, dict): raise ValueError('Tagger response must contain category maps')
    output = {}
    for category, limit in [('copyright', 3), ('character', 5), ('general', 16)]:
        scores = value.get(category, {})
        if not isinstance(scores, dict): raise ValueError('Tagger categories must map names to scores')
        valid = {}
        for name, score in scores.items():
            if not isinstance(name, str) or not name.strip() or len(name) > 160: continue
            if isinstance(score, bool) or not isinstance(score, (int, float)): continue
            if math.isfinite(score) and 0 <= score <= 1: valid[name] = float(score)
        output[category] = dict(sorted(valid.items(), key=lambda item: item[1], reverse=True)[:limit])
    return output


def threshold_predictions(value, identity_threshold=.85, general_threshold=.5):
    return {category: {name: score for name, score in scores.items()
                       if score >= (general_threshold if category == 'general' else identity_threshold)}
            for category, scores in normalize_predictions(value).items()}


def guided_query(value):
    # Never use appearance tags to seed identity searches.
    return ' '.join(list(value.get('character', {}))[:1] + list(value.get('copyright', {}))[:1]).replace('_', ' ')[:180]


class Predictor:
    def __init__(self, custom_model=None, custom_labels=None, trust_custom_code=False, model_dir=None):
        self.custom_model, self.custom_labels = custom_model, custom_labels
        self.trust_custom_code = trust_custom_code
        self.model = None; self.model_id = None; self.lock = threading.Lock()
        self.torch_threads_configured = False
        self.store = ModelStore(model_dir or Path.cwd() / 'tagger-models')

    def predict(self, image, model_id, timings=None):
        timings = {} if timings is None else timings
        queued = time.perf_counter()
        with self.lock:
            timings["queue"] = time.perf_counter() - queued
            if model_id in TAGGER_MODELS and not self.store.ready(model_id):
                raise ValueError('Model weights are not downloaded. Use Download selected model in Settings → Local model config before searching.')
            loading = time.perf_counter()
            if model_id == EVA_MODEL or model_id == 'custom-onnx':
                import numpy as np
                import onnxruntime as ort
                if self.model_id != model_id:
                    if model_id == EVA_MODEL:
                        model_path = self.store.path(EVA_MODEL) / 'model.onnx'
                        labels_path = self.store.path(EVA_MODEL) / 'selected_tags.csv'
                    else:
                        if not self.custom_model or not self.custom_labels:
                            raise ValueError('Start service with --custom-model and --custom-labels for custom-onnx')
                        model_path, labels_path = self.custom_model, self.custom_labels
                    self.model = None
                    self.model_id = None
                    options = ort.SessionOptions(); options.intra_op_num_threads = 2
                    self.model = ort.InferenceSession(str(model_path), sess_options=options, providers=['CPUExecutionProvider'])
                    with open(labels_path, encoding='utf-8-sig', newline='') as stream: self.labels = list(csv.DictReader(stream))
                    self.model_id = model_id
                    timings["model_load"] = time.perf_counter() - loading
                preprocessing = time.perf_counter()
                size = self.model.get_inputs()[0].shape[1]
                if not isinstance(size, int): raise ValueError('Custom ONNX requires a fixed NHWC image size')
                image = ImageOps.exif_transpose(image).convert('RGBA')
                background = Image.new('RGBA', image.size, 'white'); background.alpha_composite(image)
                image = background.convert('RGB'); side = max(image.size)
                square = Image.new('RGB', (side, side), 'white')
                square.paste(image, ((side-image.width)//2, (side-image.height)//2))
                square = square.resize((size, size), Image.Resampling.BICUBIC)
                tensor = np.asarray(square, dtype=np.float32)[:, :, ::-1][None, ...]
                timings["preprocess"] = time.perf_counter() - preprocessing
                inference = time.perf_counter()
                scores = self.model.run(None, {self.model.get_inputs()[0].name: tensor})[0][0]
                timings["inference"] = time.perf_counter() - inference
                postprocessing = time.perf_counter()
                if len(scores) != len(self.labels): raise ValueError('Label count does not match model output')
                result = {'general': {}, 'character': {}, 'copyright': {}}
                categories = {'0': 'general', '4': 'character', '3': 'copyright'}
                for label, score in zip(self.labels, scores):
                    category = categories.get(label.get('category'))
                    if category: result[category][label['name']] = float(score)
                result = normalize_predictions(result)
                timings["postprocess"] = time.perf_counter() - postprocessing
                return result
            if model_id != PIXAI_MODEL and model_id != self.custom_model:
                raise ValueError('Unknown model; select a preset or configure --custom-model at service startup')
            if self.model_id != model_id:
                import torch
                from transformers import pipeline
                if not self.torch_threads_configured:
                    torch.set_num_threads(2); torch.set_num_interop_threads(1)
                    self.torch_threads_configured = True
                self.model = None
                self.model_id = None
                local_model = str(self.store.path(model_id)) if model_id in TAGGER_MODELS else model_id
                self.model = pipeline(model=local_model, image_processor=local_model,
                                      trust_remote_code=model_id == PIXAI_MODEL or self.trust_custom_code,
                                      device=-1)
                self.model_id = model_id
                timings["model_load"] = time.perf_counter() - loading
            # PixAI emits categorized maps; compatible custom pipelines can do so too.
            preprocessing = time.perf_counter()
            image = ImageOps.exif_transpose(image).convert('RGBA')
            background = Image.new('RGBA', image.size, 'white'); background.alpha_composite(image)
            image = background.convert('RGB')
            timings["preprocess"] = time.perf_counter() - preprocessing
            inference = time.perf_counter()
            output = self.model(image, threshold=0.0) if model_id == PIXAI_MODEL else self.model(image)
            timings["inference"] = time.perf_counter() - inference
            postprocessing = time.perf_counter()
            if not isinstance(output, dict) or 'results' not in output:
                raise ValueError('Custom pipeline must return {results: {copyright: {}, character: {}, general: {}}}')
            result = normalize_predictions(output['results'])
            timings['postprocess'] = time.perf_counter() - postprocessing
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=7861)
    parser.add_argument('--custom-model'); parser.add_argument('--custom-labels', type=Path)
    parser.add_argument('--trust-custom-code', action='store_true')
    parser.add_argument('--owner-token', default='')
    parser.add_argument('--log-dir', type=Path, default=Path.cwd())
    parser.add_argument('--model-dir', type=Path, default=Path.cwd() / 'tagger-models')
    args = parser.parse_args()
    os.environ['HF_HUB_OFFLINE'] = '1'
    # A windowed frozen executable has no Python stdout/stderr, even though
    # HTTPServer and model-download progress expect writable streams.
    args.log_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None: sys.stdout = open(args.log_dir / 'tagger-helper.log', 'a', encoding='utf-8', buffering=1)
    if sys.stderr is None: sys.stderr = open(args.log_dir / 'tagger-helper-errors.log', 'a', encoding='utf-8', buffering=1)
    predictor = Predictor(args.custom_model, args.custom_labels, args.trust_custom_code, args.model_dir)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            import importlib.util
            encoded = json.dumps({'service': 'tag-ai-tagger', 'loaded_model': predictor.model_id,
                                  'explicit_downloads': True,
                                  'presets': TAGGER_MODELS,
                                  'runtimes': {'onnx': importlib.util.find_spec('onnxruntime') is not None,
                                               'transformers': all(importlib.util.find_spec(name) is not None for name in ('torch', 'transformers', 'timm'))}}).encode()
            self.send_response(200 if self.path == '/health' else 404)
            self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(encoded)))
            self.end_headers(); self.wfile.write(encoded)

        def do_POST(self):
            try:
                if self.path == '/shutdown':
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length < 1024: raise ValueError('Invalid shutdown request')
                    payload = json.loads(self.rfile.read(length))
                    if not args.owner_token or not secrets.compare_digest(str(payload.get('token', '')), args.owner_token):
                        raise ValueError('Not the service owner')
                    self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if self.path != '/tag': raise ValueError('Use POST /tag')
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 16 * 1024 * 1024: raise ValueError('Image request is too large or empty')
                payload = json.loads(self.rfile.read(length))
                raw = base64.b64decode(payload['image'], validate=True)
                with Image.open(BytesIO(raw)) as image:
                    timings = {}
                    result = predictor.predict(image, payload['model'], timings)
                data = {'results': result, 'model': payload['model'], 'timings': timings}; status = 200
            except Exception as exc:
                data = {'error': str(exc)}; status = 400
            encoded = json.dumps(data, allow_nan=False).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

    print(f'Tag-AI tagger listening on http://127.0.0.1:{args.port}/tag', flush=True)
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server: server.serve_forever()


if __name__ == '__main__': main()

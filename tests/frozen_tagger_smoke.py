"""Exercise the frozen classifier with a tiny real ONNX model, no downloads.

Developer-only dependency: onnx + Pillow (in the optional tagger environment).
Usage: python tests/frozen_tagger_smoke.py TagAI-Qt.exe
"""
import base64
from io import BytesIO
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import onnx
from onnx import helper, TensorProto
from PIL import Image


def main(executable):
    with tempfile.TemporaryDirectory(prefix='tagai-frozen-classifier-') as directory:
        root = Path(directory)
        model = helper.make_model(helper.make_graph([
            helper.make_node('Constant', [], ['scores'], value=helper.make_tensor('value', TensorProto.FLOAT, [1, 3], [.91, .96, .7]))
        ], 'test', [helper.make_tensor_value_info('image', TensorProto.FLOAT, [1, 8, 8, 3])],
            [helper.make_tensor_value_info('scores', TensorProto.FLOAT, [1, 3])]), opset_imports=[helper.make_opsetid('', 17)])
        model.ir_version = 8; onnx.checker.check_model(model); onnx.save(model, root / 'model.onnx')
        (root / 'labels.csv').write_text('name,category\nblue_hair,0\nprobe_character,4\nprobe_series,3\n', encoding='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        process = subprocess.Popen([str(Path(executable).resolve()), '--tagger-server', '--port', str(port),
            '--owner-token', 'test-owner', '--custom-model', str(root / 'model.onnx'), '--custom-labels', str(root / 'labels.csv'), '--model-dir', str(root / 'managed-models'), '--log-dir', str(root)])
        def post(path, payload):
            request = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
            return urllib.request.urlopen(request, timeout=10)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(base + '/health', timeout=.5) as response: data = json.load(response)
                    break
                except OSError:
                    if process.poll() is not None: break
                    time.sleep(.1)
            else: raise AssertionError('Frozen classifier did not start before timeout')
            if process.poll() is not None:
                error = Path(executable).resolve().parent / 'tagger-startup-error.log'
                raise AssertionError('Frozen classifier exited: ' + str(process.returncode) + '; ' + (error.read_text(encoding='utf-8')[-3000:] if error.exists() else 'no startup log'))
            assert data['runtimes']['onnx'], data
            image = BytesIO(); Image.new('RGB', (8, 8), 'red').save(image, format='PNG')
            try:
                with post('/tag', {'model':'SmilingWolf/wd-eva02-large-tagger-v3','image':base64.b64encode(image.getvalue()).decode()}):
                    raise AssertionError('Missing preset unexpectedly ran inference')
            except urllib.error.HTTPError as exc:
                assert 'Download selected model' in json.load(exc)['error']
            with post('/tag', {'model': 'custom-onnx', 'image': base64.b64encode(image.getvalue()).decode()}) as response: result = json.load(response)
            assert result['results']['character']['probe_character'] > .95, result
            assert result['results']['copyright']['probe_series'] > .69, result
            assert all(result['timings'][key] >= 0 for key in ('queue', 'model_load', 'preprocess', 'inference', 'postprocess')), result
            with post('/shutdown', {'token': 'test-owner'}): pass
            process.wait(timeout=10)
            print(json.dumps({'ok': True, 'real_onnx_inference': True, 'owned_shutdown': True, 'weights_downloaded': False}))
        finally:
            try:
                with post('/shutdown', {'token': 'test-owner'}): pass
            except OSError: pass
            if process.poll() is None:
                if sys.platform == 'win32':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                else: process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__': main(sys.argv[1])

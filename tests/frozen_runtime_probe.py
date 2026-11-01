"""Offline frozen-runtime probe. Build this with the app's packaging-hooks.

Creates its own loopback server and images; no user folders or external APIs.
Usage: frozen-runtime-probe.exe REPORT.json
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import traceback


def main(report_path):
    report = {'ok': False, 'requests': [], 'logs': []}
    server = None
    try:
        import providers
        from PIL import Image
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, payload):
                body = json.dumps(payload).encode(); self.send_response(200)
                self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                report['requests'].append(self.path)
                self.reply({'object': 'list', 'data': [{'id': 'probe-model', 'object': 'model', 'created': 0, 'owned_by': 'test'}]})
            def do_POST(self):
                report['requests'].append(self.path)
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert payload['model'] == 'probe-model'
                self.reply({'id': 'probe', 'object': 'chat.completion', 'created': 0, 'model': 'probe-model',
                    'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps({'copyright': 'probe_series', 'character': '', 'artist': '', 'general': 'red', 'meta': ''})}}]})
        class Harness(providers.ProviderMixin):
            _run_context_pipeline = []
            stop_requested = skip_requested = False
            def log(self, message, **kwargs): report['logs'].append(message)
        ssl.create_default_context()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        assert providers.get_http_session().get(url + '/http-probe', timeout=5).status_code == 200
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'probe.png'; Image.new('RGB', (24, 24), 'red').save(image)
            with providers.local_model_client(url + '/v1', timeout=5, max_retries=0) as client:
                assert client.models.list().data[0].id == 'probe-model'
                results = Harness().search_local_vision_engine(str(image), client, 'probe-model')
                assert results and results[0]['tag_dict_override']['copyright'] == 'probe_series', report['logs']
        report['ok'] = True
    except Exception: report['traceback'] = traceback.format_exc()
    finally:
        if server: server.shutdown(); server.server_close()
    Path(report_path).write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__ == '__main__': sys.exit(main(sys.argv[1]))

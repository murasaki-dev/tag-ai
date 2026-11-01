"""Offline Windows test of the real GUI executable's search/routing/move path.
Run with the project dependencies: python tests/frozen_app_smoke.py TagAI-Qt.exe
Only temporary image/data folders and an embedded loopback server are used.
"""
import ctypes
from contextlib import closing
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qt_app as q
from PIL import Image
from PySide6.QtCore import QPointF
from PySide6.QtQuick import QQuickItem


def main(executable, truncate_first=False, reasoning_test=False, evidence_test=False, skip_first=False, close_first=False, undo_test=False, crash_helper=False):
    executable = Path(executable).resolve(); requests = []; release_first = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, payload):
            body=json.dumps(payload).encode();self.send_response(200)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
            try:self.end_headers();self.wfile.write(body)
            except OSError:pass
        def do_GET(self):
            if self.path=='/api/v1/models':
                self.reply({'models':[{'key':'probe-model','loaded_instances':[{'id':'probe-model'}],'capabilities':{'reasoning':{'allowed_options':['off','on'],'default':'on'}}}]});return
            requests.append(self.path);self.reply({'object':'list','data':[{'id':'probe-model','object':'model','created':0,'owned_by':'test'}]})
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/tag':
                requests.append('tagger'); assert payload.get('image')
                self.reply({'model':payload['model'],'results':{'copyright':{'probe_series':.95},'character':{},'general':{'red':.9}}});return
            routing='response_format' in payload
            if evidence_test and not routing:
                assert 'UNVERIFIED LOCAL TAGGER' in payload['messages'][1]['content'][0]['text']
            if reasoning_test:assert payload.get('reasoning_effort')==('medium' if routing else 'none'),payload.get('reasoning_effort')
            requests.append('routing' if routing else 'vision')
            if (skip_first or close_first or crash_helper) and not routing and requests.count('vision')==1:release_first.wait(20)
            truncated=truncate_first and not routing and requests.count('vision')==1
            content={'candidate_id':1} if routing else {'copyright':'probe_series','character':'','artist':'','general':'red','meta':''}
            self.reply({'id':'test','object':'chat.completion','created':0,'model':'probe-model','choices':[{'index':0,'finish_reason':'length' if truncated else 'stop','message':{'role':'assistant','content':None if truncated else json.dumps(content),'reasoning_content':'private test reasoning' if truncated else None}}]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    app=q.QGuiApplication.instance() or q.QGuiApplication([]);app.setQuitOnLastWindowClosed(False);q.QQuickStyle.setStyle('Fusion')
    user=ctypes.WinDLL('user32');kernel=ctypes.WinDLL('kernel32')
    user.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user.GetClientRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    user.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
    user.SendMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    with tempfile.TemporaryDirectory(prefix='tagai-full-exe-') as directory:
        root=Path(directory);source=root/'source';source.mkdir();dest=root/'library';(dest/'Probe Series').mkdir(parents=True)
        image=source/'probe.png';Image.new('RGB',(80,60),'red').save(image)
        if skip_first:Image.new('RGB',(80,60),'blue').save(source/'probe2.png')
        config=q.normalized_config({'source_folder':str(source),'dest_folder':str(dest),'lm_url':f'http://127.0.0.1:{server.server_port}/v1','selected_model':'probe-model',
            'search_pipeline':[{'method':'IQDB','enabled':False},{'method':'SauceNAO','enabled':False},{'method':'Local Vision LLM','enabled':True}]},root)
        if reasoning_test:config.update(vision_reasoning='Off',routing_reasoning='On')
        if evidence_test:config.update(tagger_enabled=True,tagger_backend='External service',tagger_url=f'http://127.0.0.1:{server.server_port}/tag')
        if crash_helper:
            import socket
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));tagger_port=listener.getsockname()[1]
            config.update(tagger_enabled=True,tagger_backend='Managed by app',tagger_model='SmilingWolf/wd-eva02-large-tagger-v3',
                          tagger_url=f'http://127.0.0.1:{tagger_port}/tag',skip_on_web_fail=0)
        q.atomic_config(root/'config.json',config)
        # Derive the button position from the actual QML, at the default size.
        backend=q.LiveBackend(root);engine,provider=q.create_engine(backend,'App.qml');window=engine.rootObjects()[0]
        for _ in range(20):app.processEvents();time.sleep(.01)
        button=window.findChild(QQuickItem,'sortStartButton')
        point=button.mapToScene(QPointF(button.width()/2,button.height()/2));width,height=window.width(),window.height()
        button=window.findChild(QQuickItem,'sortSkipButton')
        skip_point=button.mapToScene(QPointF(button.width()/2,button.height()/2))
        backend.close();window.setVisible(False);provider.close();q.delete_qt_object(engine)
        process=subprocess.Popen([str(executable),'--data-dir',str(root)]);windows=[]
        @callback_type
        def find(hwnd,param):
            title=ctypes.create_unicode_buffer(512);user.GetWindowTextW(hwnd,title,512)
            if not title.value.startswith('Tag-AI'):return True
            pid=wintypes.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            handle=kernel.OpenProcess(0x1000,False,pid.value)
            if handle:
                path=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(path))
                if kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(size)) and Path(path.value)==executable:windows.append(hwnd)
                kernel.CloseHandle(handle)
            return True
        try:
            deadline=time.monotonic()+40
            while time.monotonic()<deadline and not windows and process.poll() is None:
                user.EnumWindows(find,0);time.sleep(.2)
            assert windows,'Packaged main window did not appear'
            time.sleep(1)
            rect=wintypes.RECT();user.GetClientRect(windows[0],ctypes.byref(rect))
            x=round(point.x()*rect.right/width);y=round(point.y()*rect.bottom/height);position=(y<<16)|x
            user.SendMessageW(windows[0],0x0200,0,position)
            user.SendMessageW(windows[0],0x0201,1,position);user.SendMessageW(windows[0],0x0202,0,position)
            if crash_helper:
                import urllib.request, urllib.error
                deadline=time.monotonic()+15
                while time.monotonic()<deadline and 'vision' not in requests:time.sleep(.05)
                assert 'vision' in requests,requests
                with urllib.request.urlopen(f'http://127.0.0.1:{tagger_port}/health',timeout=2) as reply:
                    assert json.load(reply)['service']=='tag-ai-tagger'
                pid=wintypes.DWORD();user.GetWindowThreadProcessId(windows[0],ctypes.byref(pid))
                kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
                handle=kernel.OpenProcess(1,False,pid.value);assert handle
                try:assert kernel.TerminateProcess(handle,57005)
                finally:kernel.CloseHandle(handle)
                process.wait(timeout=6)  # Bootloader must finish temp cleanup without a warning dialog.
                try:urllib.request.urlopen(f'http://127.0.0.1:{tagger_port}/health',timeout=1)
                except (OSError,urllib.error.URLError):pass
                else:raise AssertionError('App-owned helper survived abrupt parent termination')
                assert image.exists()
                print(json.dumps({'ok':True,'abrupt_exit':True,'owned_helper_stopped':True,'bootloader_exited':True}))
                return
            if close_first:
                deadline=time.monotonic()+10
                while time.monotonic()<deadline and 'vision' not in requests:time.sleep(.05)
                assert 'vision' in requests,requests
                started=time.monotonic();user.PostMessageW(windows[0],0x0010,0,0)
                process.wait(timeout=6)  # Keep the server blocked until the client exits.
                assert image.exists()
                assert q.BatchSessionStore(root/'batch_session.sqlite3').load()['completed']==0
                print(json.dumps({'ok':True,'closed_during_request':True,'close_seconds':round(time.monotonic()-started,3),'image_untouched':True}))
                return
            if skip_first:
                deadline=time.monotonic()+10
                while time.monotonic()<deadline and 'vision' not in requests:time.sleep(.05)
                assert 'vision' in requests,requests
                time.sleep(.2)
                sx=round(skip_point.x()*rect.right/width);sy=round(skip_point.y()*rect.bottom/height);skip_position=(sy<<16)|sx
                user.SendMessageW(windows[0],0x0200,0,skip_position)
                user.SendMessageW(windows[0],0x0201,1,skip_position);user.SendMessageW(windows[0],0x0202,0,skip_position)
            target=dest/'Probe Series'/('probe2.png' if skip_first else 'probe.png');deadline=time.monotonic()+(8 if skip_first else 25)
            while time.monotonic()<deadline and not target.exists():time.sleep(.1)
            assert target.exists(),f'Executable did not sort the image; requests={requests}'
            assert image.exists() if skip_first else not image.exists()
            rows=[];deadline=time.monotonic()+5;expected_rows=2 if skip_first else 1
            while time.monotonic()<deadline and len(rows)<expected_rows:
                with closing(sqlite3.connect(root/'operation_history.sqlite3')) as connection:
                    rows=[json.loads(row[0]) for row in connection.execute('SELECT payload FROM history')]
                if len(rows)<expected_rows:time.sleep(.05)
            if skip_first:
                assert len(rows)==2 and any(row['reason']=='Skipped by user; left in source' for row in rows),rows
            else:assert len(rows)==1 and not rows[0]['is_unmatched'],rows
            assert '/v1/models' in requests and 'vision' in requests and 'routing' in requests,requests
            if truncate_first:assert requests.count('vision')==2,requests
            if evidence_test:assert requests.count('tagger')==(2 if skip_first else 1),requests
            if undo_test:
                mirror=q.LiveBackend(root/'mirror');mirror_engine,mirror_provider=q.create_engine(mirror,'App.qml');mirror_window=mirror_engine.rootObjects()[0]
                mirror_window.setProperty('page',1);mirror._history_delta(rows,[])
                def walk(item):
                    yield item
                    for child in item.childItems():yield from walk(child)
                deadline=time.monotonic()+5
                while mirror.model.rowCount()!=1 and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
                for _ in range(20):app.processEvents();time.sleep(.01)
                history=next(i for i in walk(mirror_window.contentItem()) if i.property('text')=='History' and i.mapToScene(QPointF(0,0)).y()<72)
                undo=next(i for i in walk(mirror_window.contentItem()) if i.property('text')=='Undo move' and i.isVisible())
                positions=[item.mapToScene(QPointF(item.width()/2,item.height()/2)) for item in (history,undo)]
                mirror.close();mirror_window.setVisible(False);mirror_provider.close();q.delete_qt_object(mirror_engine)
                for point in positions:
                    position=(round(point.y()*rect.bottom/height)<<16)|round(point.x()*rect.right/width)
                    user.SendMessageW(windows[0],0x0200,0,position)
                    user.SendMessageW(windows[0],0x0201,1,position);user.SendMessageW(windows[0],0x0202,0,position);time.sleep(.3)
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    with closing(sqlite3.connect(root/'operation_history.sqlite3')) as connection:
                        after=[json.loads(row[0]) for row in connection.execute('SELECT payload FROM history')]
                    if len(after)==1 and after[0].get('move_undone'):break
                    time.sleep(.05)
                assert len(after)==1 and after[0].get('move_undone'),after
                assert image.exists() and not target.exists()
                assert after[0]['id']==rows[0]['id'] and after[0]['is_unmatched']
            print(json.dumps({'ok':True,'requests':requests,'history_rows':len(rows),'moved':True,'undo_retained':undo_test}))
        finally:
            release_first.set()
            for hwnd in windows:user.PostMessageW(hwnd,0x0010,0,0)
            try:
                try:process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.terminate();process.wait(timeout=10)
                    raise AssertionError('Executable remained running after its window was closed')
            finally:server.shutdown();server.server_close()


if __name__=='__main__':main(sys.argv[1], '--truncate-first' in sys.argv[2:], '--reasoning-test' in sys.argv[2:], '--evidence-test' in sys.argv[2:], '--skip-first' in sys.argv[2:], '--close-first' in sys.argv[2:], '--undo-test' in sys.argv[2:], '--crash-helper' in sys.argv[2:])

"""The app starts its own helper and never shuts down an adopted service."""
import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from tagger import TaggerManager, EVA_MODEL


class TaggerManagerTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows kernel process ownership')
    def test_owned_child_dies_even_when_owner_exits_without_cleanup(self):
        import ctypes, subprocess
        from ctypes import wintypes
        optional=Path(__file__).resolve().parents[1]/'.tagger-env'/'Scripts'/'python.exe'
        launcher=str(optional) if optional.is_file() else sys.executable
        code=(f'import sys;sys.path[:0]={sys.path!r};import os,subprocess;from tagger import TaggerManager;'
              'manager=TaggerManager(".");'
              f'child=subprocess.Popen([{launcher!r},"-c","import os,time;print(os.getpid(),flush=True);time.sleep(30)"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True);'
              'manager.guard_process(child);print(child.stdout.readline().strip(),flush=True);os._exit(0)')
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=True,timeout=10)
        pid=int(result.stdout.strip())
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];api.OpenProcess.restype=wintypes.HANDLE
        api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];api.WaitForSingleObject.restype=wintypes.DWORD
        api.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT];api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.OpenProcess(0x100001,False,pid)
        if handle:
            try:self.assertEqual(api.WaitForSingleObject(handle,2000),0,'Owned child survived its owner')
            finally:
                if api.WaitForSingleObject(handle,0)!=0:api.TerminateProcess(handle,1)
                api.CloseHandle(handle)

    @unittest.skipUnless(importlib.util.find_spec('onnxruntime'), 'Optional source classifier runtime not installed')
    def test_owned_service_starts_and_stops_but_adopted_service_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Path(__file__).resolve().parents[1] / 'tagger.py'
            # Reproduce windowed executables: HTTP logging must work even when
            # Python starts without either console stream.
            (root / 'tagger.py').write_text('import sys, runpy\nsys.stdout = sys.stderr = None\nrunpy.run_path(' + repr(str(original)) + ', run_name="__main__")\n', encoding='utf-8')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
            options = {'tagger_url': f'http://127.0.0.1:{port}/tag', 'tagger_model': EVA_MODEL}
            owner, adopted = TaggerManager(root), TaggerManager(root)
            paths = os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)
            try:
                with patch.dict(os.environ, {'PYTHONPATH': paths}): data = owner.ensure(options)
                self.assertTrue(data['runtimes']['onnx'])
                self.assertIsNotNone(owner.process); self.assertIsNone(data['loaded_model'])
                adopted.ensure(options); self.assertIsNone(adopted.process)
                adopted.close()
                self.assertEqual(requests.get(options['tagger_url'].replace('/tag', '/health'), timeout=2).status_code, 200)
            finally:
                owner.close(); adopted.close()
            self.assertIsNotNone(owner.process.poll())

    def test_unknown_service_is_not_replaced(self):
        response = Mock(); response.json.return_value = {'service': 'somebody-else'}
        with tempfile.TemporaryDirectory() as directory, patch('requests.get', return_value=response), patch('tagger.subprocess.Popen') as launch:
            manager = TaggerManager(directory)
            with self.assertRaisesRegex(ValueError, 'another service'):
                manager.ensure({'tagger_url': 'http://127.0.0.1:7861/tag', 'tagger_model': EVA_MODEL})
            manager.close(); launch.assert_not_called()

    def test_managed_mode_rejects_remote_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TaggerManager(directory)
            with self.assertRaisesRegex(ValueError, 'External service'):
                manager.ensure({'tagger_url': 'https://example.com/tag', 'tagger_model': EVA_MODEL})
            manager.close()


if __name__ == '__main__': unittest.main()

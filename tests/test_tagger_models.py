import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tagger import ModelStore, Predictor, TaggerManager, EVA_MODEL, PIXAI_MODEL, runtime_python, runtime_requirements


class TaggerModelTests(unittest.TestCase):
    def test_pixai_local_loader_does_not_duplicate_transformers_hub_options(self):
        with tempfile.TemporaryDirectory() as folder:
            predictor=Predictor(model_dir=folder)
            path=predictor.store.path(PIXAI_MODEL);path.mkdir();(path/'.ready').write_text(PIXAI_MODEL)
            model=Mock(return_value={'results':{'copyright':{'series':.95}}})
            loader=Mock(return_value=model)
            with patch.dict(sys.modules,{'torch':Mock(),'transformers':SimpleNamespace(pipeline=loader)}):
                result=predictor.predict(Image.new('RGB',(8,8)),PIXAI_MODEL)
            self.assertEqual(result['copyright'],{'series':.95})
            kwargs=loader.call_args.kwargs
            self.assertEqual(kwargs['model'],str(path))
            self.assertNotIn('local_files_only',kwargs.get('model_kwargs',{}))
            self.assertTrue(kwargs['trust_remote_code'])

    def test_single_requirements_file_keeps_pixai_optional(self):
        base, optional = runtime_requirements(Path(__file__).resolve().parents[1] / 'requirements.txt')
        self.assertTrue(any(item.startswith('onnxruntime') for item in base))
        self.assertFalse(any(item.startswith('torch') for item in base))
        self.assertTrue(any(item.startswith('torch>=') for item in optional))
        self.assertTrue(any(item.startswith('transformers>=') for item in optional))

    def test_runtime_interpreter_paths_follow_target_platform(self):
        self.assertEqual(runtime_python('environment', 'nt').parts[-2:], ('Scripts', 'python.exe'))
        self.assertEqual(runtime_python('environment', 'posix').parts[-2:], ('bin', 'python'))

    def test_search_never_downloads_missing_preset_weights(self):
        with tempfile.TemporaryDirectory() as folder:
            predictor = Predictor(model_dir=folder)
            with patch('huggingface_hub.hf_hub_download') as download:
                for model in (EVA_MODEL, PIXAI_MODEL):
                    with self.assertRaisesRegex(ValueError, 'Download selected model'):
                        predictor.predict(None, model)
                download.assert_not_called()

    def test_explicit_download_marks_ready_and_delete_is_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store = ModelStore(root / 'managed')
            unrelated = root / 'other'; unrelated.write_text('keep')
            with patch('huggingface_hub.hf_hub_download') as download:
                store.download(EVA_MODEL)
                self.assertEqual(download.call_count, 2)
                self.assertTrue(store.ready(EVA_MODEL))
            store.delete(EVA_MODEL); self.assertFalse(store.ready(EVA_MODEL));self.assertTrue(unrelated.exists())
            with self.assertRaises(ValueError):store.delete('../other')

    def test_old_helper_and_missing_pixai_runtime_have_actionable_errors(self):
        with self.assertRaisesRegex(ValueError, 'older tagger helper'):
            TaggerManager._validate_runtime({'service':'tag-ai-tagger'}, EVA_MODEL)
        with self.assertRaisesRegex(ValueError, 'Install PixAI runtime'):
            TaggerManager._validate_runtime({'explicit_downloads':True,'runtimes':{'onnx':True,'transformers':False}}, PIXAI_MODEL)

    def test_pixai_download_reports_progress_and_failed_download_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ModelStore(folder); messages = []
            def snapshot(*args, **kwargs):
                with kwargs['tqdm_class'](total=2, disable=False) as bar:
                    bar.update(1);bar.refresh()
                raise RuntimeError('incomplete transfer')
            with patch('huggingface_hub.snapshot_download', side_effect=snapshot):
                with self.assertRaisesRegex(RuntimeError, 'incomplete transfer'):
                    store.download(PIXAI_MODEL, progress=messages.append)
            self.assertFalse(store.ready(PIXAI_MODEL))
            self.assertTrue(any('50%' in message for message in messages))

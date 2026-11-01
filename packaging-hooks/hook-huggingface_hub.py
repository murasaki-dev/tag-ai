"""Keep lazy model-download exports available in the integrated classifier."""
from PyInstaller.utils.hooks import copy_metadata

hiddenimports = ['huggingface_hub.file_download', 'huggingface_hub._snapshot_download', 'hf_xet']
datas = copy_metadata('huggingface_hub')

# -*- mode: python ; coding: utf-8 -*-

datas = [('qml', 'qml'), ('tagger.py', '.'), ('requirements.txt', '.')]

a = Analysis(
    ['qt_app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=['packaging-hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
              'torch', 'torchvision', 'transformers', 'timm', 'customtkinter', 'tkinter'],
    noarchive=False,
    optimize=0,
)
# Avoid incompatible ICU libraries from unrelated tools on the build PATH.
a.binaries = [entry for entry in a.binaries if entry[0].lower() != 'icuuc.dll']
# Optional model weights, including example networks, are never packaged.
a.datas = [entry for entry in a.datas if not entry[0].lower().endswith(('.onnx', '.safetensors', '.pt', '.pth'))]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='TagAI-Qt', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, upx_exclude=[], runtime_tmpdir=None,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)

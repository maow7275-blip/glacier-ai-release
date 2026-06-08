# -*- mode: python ; coding: utf-8 -*-

import os
import sys

_spec_dir = os.path.dirname(os.path.abspath(SPEC))
_leaked_files = [
    fname for fname in ("api_key.dat",)
    if os.path.exists(os.path.join(_spec_dir, fname))
]
if _leaked_files:
    sys.stderr.write(
        "\n[GlacierAI 打包检查] 发现敏感文件，已中止打包以防止泄露：\n"
    )
    for f in _leaked_files:
        sys.stderr.write(f"  - {f}\n")
    sys.stderr.write("请删除上述文件后重试。\n\n")
    raise SystemExit(1)


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('logo.png', '.')],
    hiddenimports=['cv2', 'ffpyplayer', 'ffpyplayer.player', 'ffpyplayer.pic'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GlacierAI_V3.0',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['logo.ico'],
)

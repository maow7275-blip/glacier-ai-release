# -*- mode: python ; coding: utf-8 -*-

import os
import sys

_spec_dir = os.path.dirname(os.path.abspath(SPEC))
# 项目根目录和 dist/ 都要查：分发时常整个打包 dist 文件夹，密钥会跟着发出去
_scan_dirs = (_spec_dir, os.path.join(_spec_dir, 'dist'))
_leaked_files = [
    os.path.relpath(os.path.join(d, fname), _spec_dir)
    for d in _scan_dirs
    for fname in ('api_key.dat',)
    if os.path.exists(os.path.join(d, fname))
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
    [],
    exclude_binaries=True,
    name='GlacierAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='GlacierAI',
)

app = BUNDLE(
    coll,
    name='GlacierAI.app',
    icon='logo.icns',
    bundle_identifier='com.glacier.ai.tool',
    version='3.6.0',
    info_plist={
        'NSHighResolutionCapable': 'True',
        'CFBundleShortVersionString': '3.6.0',
        'CFBundleVersion': '3.6.0',
        'NSPrincipalClass': 'NSApplication',
    },
)

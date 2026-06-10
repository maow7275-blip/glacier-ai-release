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
    hiddenimports=[],
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
    version='3.0.1',
    info_plist={
        'NSHighResolutionCapable': 'True',
        'CFBundleShortVersionString': '3.0.1',
        'CFBundleVersion': '3.0.1',
        'NSPrincipalClass': 'NSApplication',
    },
)

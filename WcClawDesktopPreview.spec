# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src\\desktop_preview.py'],
    pathex=[],
    binaries=[],
    datas=[('assets\\wcclaw_logo.png', 'assets'), ('assets\\wcclaw_claw.png', 'assets'), ('assets\\wcclaw_claw.ico', 'assets')],
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
    a.binaries,
    a.datas,
    [],
    name='WcClawDesktopPreview',
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
    icon=['assets\\wcclaw_claw.ico'],
)

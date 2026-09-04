# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


datas = collect_data_files("customtkinter")
datas += copy_metadata("atproto")
datas += [
    ("assets/icon.ico", "assets"),
    ("assets/icon.png", "assets"),
]

hiddenimports = collect_submodules("atproto")
hiddenimports += collect_submodules("atproto_client")
hiddenimports += ["PIL.ImageTk", "pystray._win32", "socksio"]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AgniaBlueskySuite",
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
    icon=["assets/icon.ico"],
    version="packaging/version_info.txt",
)


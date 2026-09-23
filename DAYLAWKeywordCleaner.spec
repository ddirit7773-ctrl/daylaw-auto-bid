# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files


datas = [
    ("config", "config"),
    (".env.example", "."),
]
datas += collect_data_files("customtkinter")

hiddenimports = [
    "run_v2_scan",
    "build_v2_delete_plan",
    "execute_v2_delete",
    "query_account_stats",
    "restore_deleted_keyword",
    "desktop_app",
    "desktop_app_v2",
    "desktop_app_v3",
    "desktop_app_v4",
]

a = Analysis(
    ["daylaw_app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    name="DAYLAW Keyword Cleaner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name="DAYLAW Keyword Cleaner",
)

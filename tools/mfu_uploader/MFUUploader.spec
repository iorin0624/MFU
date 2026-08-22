# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path


version_vars = {}
exec((Path(SPECPATH) / "version.py").read_text(encoding="utf-8"), version_vars)
app_version = version_vars["__version__"]
version_parts = tuple(int(part) for part in app_version.split("."))
version_quad = (*version_parts, *([0] * (4 - len(version_parts))))[:4]

version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VSVersionInfo,
        VarFileInfo,
        VarStruct,
    )

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=version_quad, prodvers=version_quad),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "041104B0",
                        [
                            StringStruct("CompanyName", "MFU"),
                            StringStruct("FileDescription", "MFU Uploader"),
                            StringStruct("FileVersion", app_version),
                            StringStruct("InternalName", "MFUUploader"),
                            StringStruct("OriginalFilename", "MFUUploader.exe"),
                            StringStruct("ProductName", "MFU Uploader"),
                            StringStruct("ProductVersion", app_version),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1041, 1200])]),
        ],
    )

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"],
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
    name="MFUUploader",
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
    version=version_info,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MFUUploader",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MFUUploader.app",
        icon=None,
        bundle_identifier="jp.iori0624.mfuuploader",
        info_plist={
            "CFBundleDisplayName": "MFU Uploader",
            "CFBundleShortVersionString": app_version,
            "CFBundleVersion": app_version,
            "NSHighResolutionCapable": True,
        },
    )

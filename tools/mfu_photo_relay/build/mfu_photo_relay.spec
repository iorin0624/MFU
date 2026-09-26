from pathlib import Path

import PySide6

root = Path(SPEC).resolve().parents[1]
qt_root = Path(PySide6.__file__).resolve().parent
runtime_names = (
    "concrt140.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_codecvt_ids.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)
runtime_binaries = [
    (str(qt_root / name), ".") for name in runtime_names if (qt_root / name).is_file()
]

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    # PythonとQtで同じVisual C++ランタイムを使用する。
    binaries=runtime_binaries,
    datas=[],
    hiddenimports=["engineio.async_drivers.threading"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MFUPhotoRelay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MFUPhotoRelay",
)

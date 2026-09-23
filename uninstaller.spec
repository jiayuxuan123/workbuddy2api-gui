# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ``uninstall.exe``.

A deliberately minimal windowed build: it uses ``MessageBoxW`` for its
questions instead of tkinter, so the bundle carries no Tcl/Tk at all and
stays a couple of megabytes. It is compiled from the same ``installer.py``
so install and uninstall can never drift apart.

``--onefile`` matters here: the uninstaller is copied into the install
directory, and Add/Remove Programs has to be able to delete that whole
directory afterwards. A onefile build leaves nothing behind but itself.
"""

import os

block_cipher = None

_here = os.path.dirname(os.path.abspath(SPEC))          # noqa: F821
_icon = os.path.join(_here, "app.ico")

datas = []
if os.path.exists(_icon):
    datas.append((_icon, "."))

hiddenimports = ["winreg"]

# Same rule as elsewhere: keep email/http/html/xml importable.
excludes = [
    "unittest", "pydoc", "doctest", "test", "distutils", "setuptools",
    "pip", "pdb", "lib2to3",
    "numpy", "pandas", "PIL", "matplotlib",
    "PySide6", "PyQt5", "PyQt6",
    "sqlite3", "curses",
    "tkinter",                    # MessageBoxW needs no toolkit
    "_tkinter",
    "multiprocessing",
]

a = Analysis(
    ["installer.py"],
    pathex=[_here],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="uninstall",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon if os.path.exists(_icon) else None,
)

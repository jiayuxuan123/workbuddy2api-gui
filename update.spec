# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone updater.

The updater ships as its own small console program: it has to run while the
main application is stopped (or about to be stopped), so bundling it into the
GUI would be circular. Console mode on purpose - the user needs to read what
it is doing and confirm before their installation is touched.

Build:
    pyinstaller update.spec --noconfirm
Output:
    dist-updater/更新程序.exe
"""

import os

block_cipher = None

a = Analysis(
    ["update.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["shutil", "subprocess", "tempfile"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # No GUI toolkit and no data-processing packages: this program only moves
    # files around, so the bundle stays a few megabytes.
    excludes=[
        "tkinter", "PySide6", "unittest", "pydoc", "doctest", "test",
        "distutils", "setuptools", "pip", "pdb", "lib2to3",
        "numpy", "pandas", "PIL", "matplotlib",
    ],
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
    name="更新程序",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # console=True: the operator should see the plan and confirm it. This is
    # the one program in the suite where a window would hide the important part.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

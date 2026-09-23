# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the WorkBuddy2API installer (``setup.exe``).

Produces a **single-file windowed executable** that carries three things
inside itself:

* ``payload.zip``   - the program directory to unpack (EXE + ``_internal/``)
* ``uninstall.exe`` - the uninstaller, dropped next to the installed files
* ``app.ico``       - the mark shown on shortcuts and in the taskbar

Why one file: an installer that arrives as a folder is not an installer,
it is an archive. Users double-click one thing.

tkinter is the wizard toolkit here. It is part of the standard library, so
the build needs no extra package, and PyInstaller's Tcl/Tk hook collects
everything it needs. ``--onefile`` puts Tcl/Tk on the tmpdir, which is
exactly what PyInstaller expects it to do.

The universal rule from ``workbuddy2api.spec`` applies here too: do **not**
exclude ``email``. Anything touching ``urllib``/``http`` pulls it in at
import time, and the EXE then dies with ModuleNotFoundError before a single
window appears.
"""

import os

ONEFILE = True
block_cipher = None

#: Files that must travel inside the setup executable. The build script
#: creates these before invoking PyInstaller; missing ones would produce a
#: setup that cannot install anything, so they are asserted here rather than
#: discovered at runtime by the user.
_REQUIRED = ("payload.zip", "uninstall.exe", "app.ico")
_here = os.path.dirname(os.path.abspath(SPEC))          # noqa: F821
#: The build script writes payload.zip into dist/ while the spec directory
#: holds the smaller inputs; accept either location for each name.
_SEARCH = (_here, os.path.join(_here, "dist"), os.path.join(_here, "dist",
                                                             "uninstaller"))


def _locate(name):
    for folder in _SEARCH:
        candidate = os.path.join(folder, name)
        if os.path.exists(candidate):
            return candidate
    return ""


_resolved = {}
for _name in _REQUIRED:
    _path = _locate(_name)
    if not _path:
        raise SystemExit(
            "installer.spec: %s not found.\n"
            "Looked in: %s\n"
            "Run _build_installer.py instead of calling PyInstaller directly."
            % (_name, ", ".join(_SEARCH)))
    _resolved[_name] = _path

datas = [(_resolved[name], ".") for name in _REQUIRED]
_icon_path = _resolved["app.ico"]

hiddenimports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.constants",
    "winreg",
]

# Only genuinely unreachable packages. NOTE: "email", "http", "html" and
# "xml" must stay importable - see the module docstring.
excludes = [
    "unittest", "pydoc", "doctest", "test", "distutils", "setuptools",
    "pip", "pdb", "lib2to3",
    "numpy", "pandas", "PIL", "matplotlib",
    "PySide6", "PyQt5", "PyQt6",
    "sqlite3", "curses", "multiprocessing",
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
    name="WorkBuddy2API-%s-setup" % os.environ.get("WB_SETUP_VERSION", "1.0.0"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # The wizard itself is the console. A console window behind it would
    # look broken to anyone who is not debugging the build.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon_path,
)

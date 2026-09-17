# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for WorkBuddy2API (PySide6 / Qt6 GUI build).

Produces ``WorkBuddy2API.exe``: a windowed application with the full interface
and no console. Everything the old .bat files did - start/stop, add accounts,
autostart - lives inside the window.

Four things this spec has to get right:

1. **dashboard.html is bundled as data.** ``wb_runtime.resource_path()`` looks
   for it inside ``sys._MEIPASS``, so a build that omitted it would serve
   "dashboard.html unavailable" on the panel page.

2. **accounts/ and usage/ are NOT bundled.** They are created next to the EXE
   by ``wb_runtime.data_dir()``. Shipping empty copies inside the bundle
   would only hide the real files from the user.

3. **The Qt platform plugin must ship.** Without ``platforms/qwindows.dll``
   the EXE aborts at startup with "could not find or load the Qt platform
   plugin windows" - and with ``console=False`` there is no console to show
   it, so it looks like nothing happens at all. PyInstaller's PySide6 hook
   collects these, and the build is smoke-tested afterwards to prove it.

4. **Only the Qt modules actually used are collected.** A default PySide6
   build pulls in every module (WebEngine, 3D, Charts, Multimedia...) and
   lands well over 200 MB. The list below covers the widgets, core, gui and
   svg pieces this app touches.
"""

import os

# ONEFILE=1 builds a single self-extracting .exe. onedir starts faster and is
# the better default for an app that autostarts at login.
ONEFILE = os.environ.get("ONEFILE", "").strip() not in ("", "0", "no", "false")

block_cipher = None

datas = [
    # Read-only web panel, read from inside the bundle by resource_path().
    ("dashboard.html", "."),
]

#: Qt modules the application imports. Anything outside this list is excluded
#: further down; adding a widget from a new module means adding it here too.
QT_MODULES = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

hiddenimports = [
    # Project modules. Several are imported inside functions to keep import
    # time down, which static analysis can miss.
    "wb_runtime",
    "wb_usagelog",
    "wb_accounts",
    "wb_catalog",
    "wb_settings",
    "wb_scheduler",
    "wb_tasks",
    "wb_fingerprint",
    "wb_gateway",
    "wb_autostart",
    "wb_gui",
    "wb_ui_theme",
] + QT_MODULES + [
    # Standard-library pieces the HTTPS and tray paths need.
    "webbrowser",
    "winreg",
    "ssl",
    "http.client",
    "urllib.request",
    "urllib.error",
    "html.parser",
]

# Only genuinely unreachable packages belong here.
#
# Do NOT add "email", "http", "html" or "xml": stdlib urllib.request imports
# ``email`` at module load and http.client imports ``email.parser``, so
# excluding them makes the EXE die on startup with ModuleNotFoundError. This
# was hit once already - the build is smoke-tested afterwards to prove it runs.
#
# The Qt exclusions keep the bundle near 60 MB instead of several hundred:
# none of these are imported by QtCore/QtGui/QtWidgets.
excludes = [
    "unittest", "pydoc", "doctest", "test", "distutils", "setuptools",
    "pip", "pdb", "lib2to3",
    "numpy", "pandas", "PIL", "matplotlib",
    # Qt modules the app does not use.
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQml", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtWebSockets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
]

a = Analysis(
    ["wb_gui.py"],
    pathex=[],
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

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="WorkBuddy2API",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,          # UPX corrupts some Qt DLLs; the size is acceptable
        upx_exclude=[],
        runtime_tmpdir=None,
        # console=False: this is a GUI application. wb_runtime turns every
        # stream write into a no-op when there is no console, so logging still
        # reaches the in-app Logs tab.
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="WorkBuddy2API",
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
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="WorkBuddy2API",
    )

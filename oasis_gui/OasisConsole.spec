# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Oasis console, Windows single-file build.

    pyinstaller --noconfirm --clean OasisConsole.spec

Notes that matter:

  * onefile is expressed by handing a.binaries / a.datas to EXE. The app then
    resolves its data directory from sys.executable (see app.app_dir()), because
    __file__ points into the temporary extraction folder under onefile.
  * qfluentwidgets carries Qt style sheets, SVG icons and a compiled Qt resource
    module; curl_cffi carries its own libcurl. Both need collect_all or the exe
    starts with a missing-resource or missing-DLL error.
  * socks is imported lazily inside mailbox.open_tunnel(), so PyInstaller's
    static analysis cannot see it.
  * Qt 6.6.x is the target. Newer Qt builds (6.11) require Windows APIs that the
    Wine-based cross-build host does not implement, and the resulting exe would
    not have been analysable.
"""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# qfluentwidgets: Qt style sheets, SVG icons, compiled Qt resource module.
# curl_cffi: its own libcurl.
# playwright: the node driver + cli.js, so browser mode works from the exe
#            (the browser binary itself is fetched on first use, see
#            browser_registrar.resolve_channel).
for package in ("qfluentwidgets", "curl_cffi", "playwright"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "socks",                    # lazy import in core.mailbox
    "pyqt6_frameless_window",   # required by qfluentwidgets
    "darkdetect",
    "imaplib",
    "email",
    "email.utils",
    "email.message",
    "email.header",
]

# qfluentwidgets imports QtCore, QtGui, QtWidgets, QtSvg, QtXml, QtMultimedia
# and QtMultimediaWidgets - none of those may be excluded. Everything below is
# genuinely unused by this app and is what keeps the exe near 42 MB.
excludes = [
    "tkinter", "unittest", "pydoc_data", "lib2to3", "test",
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebChannel",
    "PyQt6.QtQuick", "PyQt6.QtQuick3D", "PyQt6.QtQml", "PyQt6.QtQuickWidgets",
    "PyQt6.Qt3DCore", "PyQt6.Qt3DRender", "PyQt6.Qt3DAnimation",
    "PyQt6.QtBluetooth", "PyQt6.QtNfc", "PyQt6.QtPositioning",
    "PyQt6.QtRemoteObjects", "PyQt6.QtSensors", "PyQt6.QtSerialPort",
    "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtDesigner", "PyQt6.QtHelp",
    "PyQt6.QtCharts", "PyQt6.QtDataVisualization", "PyQt6.QtPdf",
    "PyQt6.QtPdfWidgets", "PyQt6.QtTextToSpeech", "PyQt6.QtWebSockets",
    "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets",
]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name="OasisConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # desktop app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

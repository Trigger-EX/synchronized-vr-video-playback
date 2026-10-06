# PyInstaller spec: onedir, windowed. Build from the repo root:  pyinstaller packaging/syncvr.spec
# Needs: pip install ./server[gui] pyinstaller   (PySide6-Essentials only, no Addons)
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
icon = ROOT / "packaging" / "icon.ico"

# Qt modules the control window does not use; excluding them keeps their libraries, plugins and
# QML files out of the bundle.
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtMultimediaWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtRemoteObjects",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtNetworkAuth", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtHttpServer", "PySide6.QtUiTools",
    "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtPrintSupport",
    "tkinter", "unittest", "pytest",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "server")],
    hiddenimports=["syncvr.gui", "aiohttp"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="SyncVR",
    console=False,  # windowed: no console on Windows, no terminal on macOS
    icon=str(icon) if sys.platform.startswith("win") and icon.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SyncVR")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="SyncVR.app", bundle_identifier="com.syncvr.desktop",
                 info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0"})

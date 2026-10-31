# -*- mode: python ; coding: utf-8 -*-
# Build from repo root:
#   pyinstaller --noconfirm build/pyinstaller/avidbackup.spec
# Produces dist/avidbackup.exe (windowed UI launcher that opens the browser).

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parents[1]
SRC = ROOT / "src"
TEMPLATES = SRC / "avidbackup" / "webui" / "templates"
ENTRY = SRC / "avidbackup" / "__main_ui__.py"

hiddenimports = collect_submodules("avidbackup")
hiddenimports += [
    "flask",
    "jinja2",
    "paramiko",
    "pymysql",
    "dotenv",
    "webbrowser",
    "werkzeug",
    "pystray",
    "PIL",
]

STATIC = SRC / "avidbackup" / "webui" / "static"

datas = [(str(TEMPLATES), os.path.join("avidbackup", "webui", "templates"))]
if STATIC.is_dir():
    datas.append((str(STATIC), os.path.join("avidbackup", "webui", "static")))

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    a.binaries,
    a.datas,
    [],
    name="avidbackup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

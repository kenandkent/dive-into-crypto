# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the short-lab desktop backend (`short-lab`).

Build (from desktop/backend):

    cd desktop/backend && uv run --with pyinstaller pyinstaller short-lab.spec --noconfirm

Output: dist/short-lab/ — a ONEDIR bundle on purpose, NOT onefile. A onefile
self-extracting executable is a classic antivirus false-positive trigger and
pays a temp-dir extraction cost on every launch. Afterwards run
dist/short-lab/short-lab.exe; it serves 127.0.0.1:8780 and opens the UI.

The `short-lab` console script and the legacy `dive-desktop` alias both point
at `diveintocrypto_desktop.__main__:main`, so the frozen executable exposes
the same entry regardless of which CLI name launched the source run.

How the frozen app finds the UI bundle
--------------------------------------
api/app.py resolves the prebuilt UI as:

    _UI_DIST = Path(__file__).resolve().parents[4] / "ui" / "dist"

In the checkout (app.py at desktop/backend/src/diveintocrypto_desktop/api/app.py)
parents[4] == desktop/, i.e. desktop/ui/dist. In a frozen onedir build the
module lives at <app>/_internal/diveintocrypto_desktop/api/app.pyc, so
parents[4] is one level ABOVE the app folder — the untouched path math can
never see inside the bundle. Two complementary measures close that gap:

1. This spec copies desktop/ui/dist to sys._MEIPASS/ui/dist (datas dest
   "ui/dist"), which is exactly where the sys._MEIPASS fallback in api/app.py
   looks for it.
2. The packaging job in .github/workflows/release.yml (`package-desktop`)
   mirrors the bundle to <dist-root>/ui/dist — the location parents[4]
   resolves to in the frozen tree — so the legacy resolution ALSO finds it
   when the whole dist/ tree is extracted together. See docs/packaging.md.

How the frozen app finds its writable data
------------------------------------------
The DuckDB file is NEVER written next to the frozen resources. At runtime
shortlab/paths.py resolves the writable directory to the per-user data
location (%LOCALAPPDATA%/short-lab on Windows) and creates/migrates
shortlab.duckdb there, so launching from a read-only extraction directory
works and data survives restarts. See docs/packaging.md.

Bundled resources beyond the UI
-------------------------------
- DuckDB native libraries (collect_dynamic_libs("duckdb")): the query engine
  cannot run from pure Python alone.
- shortlab/default.yaml: the packaged default Short-Lab configuration, read
  when SHORTLAB_CONFIG_PATH is unset.

pandas / numpy / aiohttp / fastapi are picked up automatically by Analysis;
the hiddenimports below cover uvicorn's dynamically-selected standard extras,
which static analysis cannot see through uvicorn's import-time configuration.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# desktop/backend — the directory containing this spec (SPECPATH is injected
# by PyInstaller when it evaluates the spec).
_ROOT = os.path.abspath(SPECPATH)
# The committed prebuilt UI bundle that api/app.py serves at "/".
_UI_DIST = os.path.join(_ROOT, os.pardir, "ui", "dist")
# The packaged Short-Lab default configuration (overridable at runtime via
# SHORTLAB_CONFIG_PATH).
_SHORTLAB_DEFAULT_YAML = os.path.join(
    _ROOT, "src", "diveintocrypto_desktop", "shortlab", "default.yaml"
)

a = Analysis(
    # Analyze the console-script target directly. __main__.py imports the rest
    # of the package absolutely (`from diveintocrypto_desktop.api.app import
    # app`), which resolves thanks to pathex below — no extra launcher script
    # needed for the `short-lab` entry point (the legacy `dive-desktop` alias
    # maps to the same `__main__:main`).
    [os.path.join(_ROOT, "src", "diveintocrypto_desktop", "__main__.py")],
    # src-layout: the package is importable from src/.
    pathex=[os.path.join(_ROOT, "src")],
    binaries=[
        # DuckDB query engine native libraries.
        *collect_dynamic_libs("duckdb"),
    ],
    datas=[
        # Bundled under sys._MEIPASS as "ui/dist" (see header): the
        # frozen-build fallback location in api/app.py.
        (_UI_DIST, "ui/dist"),
        # Packaged Short-Lab default configuration (see header).
        (_SHORTLAB_DEFAULT_YAML, "diveintocrypto_desktop/shortlab"),
        # DuckDB package data, if any ships alongside the extension.
        *collect_data_files("duckdb", include_py_files=False),
    ],
    hiddenimports=[
        # uvicorn[standard] — dynamically imported, invisible to Analysis.
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        # Short-Lab runtime: DuckDB engine + YAML config (both imported
        # through several layers, listed explicitly for frozen builds).
        "duckdb",
        "yaml",
        # Imported directly by the app / root e2e suite surface.
        "websockets",
        "aiohttp",
    ],
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
    exclude_binaries=True,  # onedir: binaries go to COLLECT, not the exe
    name="short-lab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX packing triples AV false-positive rates; leave off
    console=True,  # terminal-launched service; logs to stdout (api/app.py)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="short-lab",
)

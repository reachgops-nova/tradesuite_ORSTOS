# -*- mode: python ; coding: utf-8 -*-
"""
==========================================================
TradeSuite
installer/TradeSuite.spec
==========================================================

PyInstaller onefile build, same approach as SureFramePro's
SureFramePro_Windows.spec (Analysis -> PYZ -> EXE, no COLLECT --
that's what makes it one file instead of a folder build).

OpenAlgo itself is NOT bundled here -- it's fetched fresh per-install by
engine/openalgo_manager.py at first run (see installer/
openalgo_bootstrap.py and the plan's licensing rationale). Only
TradeSuite's own proprietary code (engine/, gui/, licensing/, installer/)
and its direct Python dependencies go into this EXE.

Build:
    cd installer
    pyinstaller TradeSuite.spec
Output: dist/TradeSuite.exe
"""

import sys
from pathlib import Path

block_cipher = None
PROJECT_ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "engine", "engine.config_store", "engine.openalgo_client", "engine.orb_strategy",
        "engine.tamil_strategy", "engine.process_manager", "engine.openalgo_manager", "engine.shadow_compare",
        "gui", "gui.main_window", "gui.license_screen", "gui.setup_wizard",
        "gui.status_panel", "gui.strategy_picker", "gui.trade_log_panel", "gui.history_panel", "gui.table_utils",
        "gui.activity_log_panel", "gui.labels", "gui.trail_study_panel", "engine.trail_tracker",
        "licensing", "licensing.license_core", "licensing.device_fingerprint", "licensing.qr_payment",
        "licensing.server_config", "licensing._server_blob", "licensing.mail_relay", "licensing.renewal_report",
        "installer", "installer.openalgo_bootstrap",
    ],
    hookspath=[],
    runtime_hooks=[],
    # TradeSuite doesn't use any of these -- they were pulled in transitively
    # (mostly via pandas' optional-feature hooks) from this shared dev
    # machine's global site-packages, which has every other project's deps
    # mixed in. Excluding them: (a) cuts the onefile payload dramatically
    # (self-extraction of the full payload runs on every launch, so this
    # also directly affects startup time), and (b) removes their
    # PyInstaller runtime hooks (pywintypes/pythoncom COM init, tkinter's
    # Tcl/Tk lookup) from executing unconditionally before main.py's own
    # code even runs -- the leading suspect for a silent post-import hang
    # observed in testing, with zero traceback since nothing crashed.
    excludes=[
        "tkinter", "_tkinter",
        "win32com", "pythoncom", "pywintypes",
        "IPython", "jedi", "parso", "traitlets", "nbformat",
        "numba", "llvmlite",
        "sqlalchemy",
        "pyarrow", "fastparquet",
        "jsonschema", "jsonschema_specifications",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TradeSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI app -- no console window. Confirmed working with console=True first
                     # (see session notes: startup hang was caused by unrelated bundled packages'
                     # PyInstaller runtime hooks -- win32com/pythoncom/pywintypes/tkinter -- pulled
                     # in transitively from this dev machine's shared global site-packages; fixed
                     # via the excludes list above, verified before switching back to windowed mode).
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # TODO: add an .ico before shipping
)

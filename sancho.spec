# -*- mode: python ; coding: utf-8 -*-

# This is a PyInstaller spec file. For more information, see:
# https://pyinstaller.org/en/stable/spec-files.html

import os
import sys
from PyInstaller.utils.hooks import collect_data_files

# Determine the project root directory
project_root = os.path.dirname(os.path.abspath(__file__))

# --- Collect Cogs ---
# Dynamically find all Python files in the 'cogs' directory to be included as hidden imports.
# PyInstaller cannot automatically detect these because they are loaded dynamically.
hidden_imports = []
cogs_dir = os.path.join(project_root, 'cogs')
if os.path.isdir(cogs_dir):
    for filename in os.listdir(cogs_dir):
        if filename.endswith('.py') and not filename.startswith('__'):
            cog_module = f'cogs.{filename[:-3]}'
            hidden_imports.append(cog_module)

# --- Collect Data Files ---
# Data files required by the application at runtime.
# The 'assets' directory and 'info.env' are NOT bundled, as they are intended
# to be user-configurable and reside next to the executable.
datas = []

# Collect data files for libraries that need them (e.g., dateparser, pytz).
# PyInstaller hooks usually handle this, but we add them explicitly for robustness.
datas.extend(collect_data_files('dateparser'))
datas.extend(collect_data_files('pytz'))

# --- Analysis ---
# The Analysis object collects the script's dependencies.
a = Analysis(
    ['main.py'],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# --- PYZ (Python Archive) ---
# Create a PYZ archive containing all the Python modules.
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# --- EXE ---
# Create the executable file.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='sancho',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None # You can specify an icon file here, e.g., 'assets/icon.ico'
)

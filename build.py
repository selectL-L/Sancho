import os
import sys
import PyInstaller.__main__  # type: ignore[import-not-found]

import config

# Platform behavior:
#   - Windows: Console defaults to cp1252/cp437; must force UTF-8 for emoji support
#   - Linux: System default is usually UTF-8; no reconfiguration needed
if sys.platform == "win32":
    # Pylance doesn't know sys.stdout is a TextIOWrapper here
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]

# Get the absolute path to the workspace root
HERE = os.path.dirname(os.path.abspath(__file__))

# ──────────────────────────────────────────────────────────────────
# Package data & hidden-import declarations
#
# Many dependencies ship non-.py files (dictionaries, locale data,
# JS solvers, SSL certs) or use dynamic imports that PyInstaller
# cannot trace.  We declare them here so the build picks them up.
#
# --collect-data <pkg>   → bundles every non-.py file in the package
# --collect-submodules   → recursively includes all submodules
# --hidden-import        → adds a single module PyInstaller missed
# ──────────────────────────────────────────────────────────────────

# Packages whose non-Python data files must be bundled.
# fmt: off
COLLECT_DATA_PACKAGES: list[str] = [
    'pykakasi',           # Japanese romanization dictionaries (.db files)
    'pypinyin',           # Chinese pinyin dictionaries (.json files)
    'dateparser',         # Timezone cache (.pkl file)
    'certifi',            # Root CA bundle (cacert.pem) for SSL/TLS
    'ytmusicapi',         # Locale .mo files for YouTube Music API
    'yt_dlp',             # YouTube JS solver scripts
    'yt_dlp_ejs',         # External JS solver for yt-dlp
]
# fmt: on

# Packages with submodules loaded dynamically at runtime.
# yt_dlp_plugins is a *namespace package* — bgutil-ytdlp-pot-provider
# and yt-dlp-ejs install extractors into it.  PyInstaller cannot
# discover namespace packages on its own.
# fmt: off
COLLECT_SUBMODULES_PACKAGES: list[str] = [
    'yt_dlp_plugins',     # Namespace pkg: bgutil PO-token extractor, etc.
    'uvicorn',            # Dynamically loads loops, protocols, lifespan
]
# fmt: on

# Individual modules that are imported dynamically and missed by
# analysis.  Add entries here when a frozen build crashes with
# "ModuleNotFoundError" for a specific module.
EXTRA_HIDDEN_IMPORTS: list[str] = [
    # uvicorn dynamically selects event-loop and protocol implementations
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    'uvicorn.loops.auto',
    'uvicorn.loops.asyncio',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets.auto',
]


def get_hidden_imports() -> list[str]:
    """Scans the 'cogs' directory and returns a list of module names
    to be passed as hidden imports to PyInstaller.
    """
    cogs_path = os.path.join(HERE, 'cogs')
    cogs: list[str] = []

    if os.path.exists(cogs_path):
        for filename in os.listdir(cogs_path):
            if filename.endswith('.py') and not filename.startswith('__'):
                # Convert filename to module path: "fun.py" -> "cogs.fun"
                cogs.append(f'cogs.{filename[:-3]}')

    return cogs


def _check_package_available(pkg: str) -> bool:
    """Returns True if *pkg* is importable in the current environment."""
    try:
        __import__(pkg)
        return True
    except ImportError:
        return False


def build() -> None:
    print("🚀 Starting Bot Build Process...")

    # 1. Gather Hidden Imports
    # PyInstaller cannot see imports that are loaded dynamically (like your cogs).
    # We find them manually and tell PyInstaller to include them.
    cogs = get_hidden_imports()
    print(f"📦 Found {len(cogs)} cogs to bundle: {', '.join(cogs)}")

    # Create a manifest file for the frozen app to read
    import json
    manifest_path = os.path.join(HERE, 'cogs_manifest.json')
    with open(manifest_path, 'w') as f:
        json.dump(cogs, f)

    hidden_import_args: list[str] = []
    for cog in cogs:
        hidden_import_args.append(f'--hidden-import={cog}')

    # 2. Collect package data & submodules
    # Skip packages that aren't installed — the bot may run without
    # optional features (e.g. music cog disabled → no pykakasi).
    collect_args: list[str] = []

    for pkg in COLLECT_DATA_PACKAGES:
        if _check_package_available(pkg):
            collect_args.append(f'--collect-data={pkg}')
            print(f"📂 Collecting data files for {pkg}")
        else:
            print(f"⏭️  Skipping {pkg} (not installed)")

    for pkg in COLLECT_SUBMODULES_PACKAGES:
        if _check_package_available(pkg):
            collect_args.append(f'--collect-submodules={pkg}')
            print(f"📂 Collecting submodules for {pkg}")
        else:
            print(f"⏭️  Skipping {pkg} (not installed)")

    for mod in EXTRA_HIDDEN_IMPORTS:
        hidden_import_args.append(f'--hidden-import={mod}')

    # 3. Check for FFmpeg to bundle (optional but recommended for music playback)
    # Platform behavior:
    #   - Windows: Expects ffmpeg.exe in project root
    #   - Linux: Expects ffmpeg (no extension) in project root
    # Note: get_ffmpeg_path() in utils/music_helpers.py must match this logic
    ffmpeg_args: list[str] = []
    ffmpeg_path = None

    if sys.platform == "win32":
        candidate = os.path.join(HERE, 'ffmpeg.exe')
        if os.path.exists(candidate):
            ffmpeg_path = candidate
    else:
        candidate = os.path.join(HERE, 'ffmpeg')
        if os.path.exists(candidate):
            ffmpeg_path = candidate

    if ffmpeg_path:
        print(f"🎵 Found FFmpeg at {ffmpeg_path} - will bundle for music playback")
        ffmpeg_args.append(f'--add-binary={ffmpeg_path}{os.pathsep}.')
    else:
        print("⚠️  FFmpeg not found in project root - music playback will require FFmpeg in PATH")
        print("   To bundle FFmpeg: download the ffmpeg binary and place it in the project root.")

    # 4. Construct PyInstaller Arguments
    # Note: We are NOT bundling the 'assets' folder. The bot expects 'assets' to be
    # in the same directory as the executable (see config.py). This allows for
    # easy customization and database persistence without rebuilding.
    args = [
        'main.py',                      # Entry point
        f'--name={config.BOT_NAME}',    # Output executable name
        '--onefile',                    # Bundle everything into a single .exe
        '--clean',                      # Clean PyInstaller cache
        '--noconfirm',                  # Overwrite output directory without asking
        '--console',                    # Keep the console window (essential for bot logs)
        # Include the manifest file in the root of the bundle
        f'--add-data=cogs_manifest.json{os.pathsep}.',
        # '--debug=all',                # Uncomment if you need to debug the bootloader
    ] + hidden_import_args + collect_args + ffmpeg_args

    # 5. Run PyInstaller
    print("🔨 Running PyInstaller...")
    try:
        PyInstaller.__main__.run(args)
        exe_name = f"{config.BOT_NAME}.exe" if sys.platform == "win32" else config.BOT_NAME
        print("\n✅ Build Complete!")
        print(f"   Executable is located in: {os.path.join(HERE, 'dist', exe_name)}")
        print("   IMPORTANT: You must copy your 'info.env' file AND the 'assets' folder")
        print("               to the same directory as the executable for it to run!")
    except Exception as e:
        print(f"\n❌ Build Failed: {e}")
        sys.exit(1)
    finally:
        # Cleanup the temporary manifest file
        if os.path.exists(manifest_path):
            os.remove(manifest_path)


if __name__ == '__main__':
    build()

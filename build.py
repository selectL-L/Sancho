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


def get_hidden_imports():
    """
    Scans the 'cogs' directory and returns a list of module names
    to be passed as hidden imports to PyInstaller.
    """
    cogs_path = os.path.join(HERE, 'cogs')
    cogs = []

    if os.path.exists(cogs_path):
        for filename in os.listdir(cogs_path):
            if filename.endswith('.py') and not filename.startswith('__'):
                # Convert filename to module path: "fun.py" -> "cogs.fun"
                cogs.append(f'cogs.{filename[:-3]}')

    return cogs


def build():
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

    hidden_import_args = []
    for cog in cogs:
        hidden_import_args.append(f'--hidden-import={cog}')

    # 2. Check for FFmpeg to bundle (optional but recommended for music playback)
    # Platform behavior:
    #   - Windows: Expects ffmpeg.exe in project root
    #   - Linux: Expects ffmpeg (no extension) in project root
    # Note: get_ffmpeg_path() in utils/music_helpers.py must match this logic
    ffmpeg_args = []
    ffmpeg_path = None

    if os.name == 'nt':
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

    # 3. Construct PyInstaller Arguments
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
    ] + hidden_import_args + ffmpeg_args

    # 4. Run PyInstaller
    print("🔨 Running PyInstaller...")
    try:
        PyInstaller.__main__.run(args)
        print("\n✅ Build Complete!")
        print(f"   Executable is located in: {os.path.join(HERE, 'dist', f'{config.BOT_NAME}.exe')}")
        print("   IMPORTANT: You must copy your 'info.env' file AND the 'assets' folder")
        print("               to the same directory as the executable for it to run!")
    except Exception as e:
        print(f"\n❌ Build Failed: {e}")
    finally:
        # Cleanup the temporary manifest file
        if os.path.exists(manifest_path):
            os.remove(manifest_path)


if __name__ == '__main__':
    build()

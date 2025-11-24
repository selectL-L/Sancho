import os
import sys
import PyInstaller.__main__

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
    print("🚀 Starting Sancho Build Process...")

    # 1. Gather Hidden Imports
    # PyInstaller cannot see imports that are loaded dynamically (like your cogs).
    # We find them manually and tell PyInstaller to include them.
    cogs = get_hidden_imports()
    print(f"📦 Found {len(cogs)} cogs to bundle: {', '.join(cogs)}")
    
    hidden_import_args = []
    for cog in cogs:
        hidden_import_args.append(f'--hidden-import={cog}')

    # 2. Construct PyInstaller Arguments
    # Note: We are NOT bundling the 'assets' folder. The bot expects 'assets' to be
    # in the same directory as the executable (see config.py). This allows for
    # easy customization and database persistence without rebuilding.
    args = [
        'main.py',                      # Entry point
        '--name=Sancho',                # Output executable name
        '--onefile',                    # Bundle everything into a single .exe
        '--clean',                      # Clean PyInstaller cache
        '--noconfirm',                  # Overwrite output directory without asking
        '--console',                    # Keep the console window (essential for bot logs)
        # '--debug=all',                # Uncomment if you need to debug the bootloader
    ] + hidden_import_args

    # 3. Run PyInstaller
    print("🔨 Running PyInstaller...")
    try:
        PyInstaller.__main__.run(args)
        print("\n✅ Build Complete!")
        print(f"   Executable is located in: {os.path.join(HERE, 'dist', 'Sancho.exe')}")
        print("   IMPORTANT: You must copy your 'info.env' file AND the 'assets' folder")
        print("               to the same directory as the executable for it to run!")
    except Exception as e:
        print(f"\n❌ Build Failed: {e}")

if __name__ == '__main__':
    build()

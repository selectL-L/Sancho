import os
import subprocess
import sys
import argparse
from config import COGS_TO_LOAD

# This script will automate the Pyinstaller build process for us.
# It's job is to find all the cogs in the cogs directory, and then
# add them as --hidden-imports to the Pyinstaller command.
# The only reason this exists is because Pyinstaller wants a static list to build from

def main():
    """Runs pyinstaller with the appropriate hidden imports."""
    
    # --- Argument Parsing ---
    parser = argparse.ArgumentParser(description="Sancho Pyinstaller Build Script")
    
    # Determine the default platform
    current_platform = 'windows'
    if sys.platform == 'linux':
        current_platform = 'linux'
    elif sys.platform == 'darwin':
        current_platform = 'macos'

    parser.add_argument(
        '--target', 
        choices=['windows', 'linux', 'macos'], 
        default=current_platform,
        help="The target operating system. Defaults to the current OS."
    )
    args = parser.parse_args()

    # Determine the correct path separator for the target OS
    path_separator = ';' if args.target == 'windows' else ':'
    
    project_root = os.path.dirname(os.path.abspath(__file__))

    print(f"--- Sancho Pyinstaller Build Script (Target: {args.target}) ---")

    # The list of cogs is now imported directly from config.py
    if not COGS_TO_LOAD:
        print("Could not discover any cogs from config.py. Exiting.")
        sys.exit(1)
    
    # Then build the pyinstaller command
    command = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',  # Create a one-file bundled executable
        '--name', 'sancho',  # Name of the output executable
        '--clean',  # Clean up previous builds before building (This REALLY hurt last time I forgot it)
        '--noconfirm',  # Overwrite output directory without asking
    ]

    # Add data files (such as the ENV file)
    command.extend(['--add-data', f'info.env{path_separator}.' ])

    # This section is a little sensitive to your environment so if you're not building it exactly like I am uhm, sorry I guess?
    # It looks for the dateparser library and handles the neccesary hidden imports and data files (which is WHY it's specific to your environment)
    try:
        import dateparser as dp
        dateparser_path = os.path.dirname(dp.__file__)
        command.extend(['--add-data', f'{dateparser_path}{path_separator}dateparser'])
        print(f"found dateparser at: {dateparser_path}") # I know wher emy dateparser is, but most won't so this is just to help with debugging
    except ImportError:
        print("dateparser library not found. Please ensure it is installed in your environment.") # This is a critical error, the bot won't run without it (so we immediatly exit after)
        sys.exit(1)

    # Do the same for pytz, which is also critical for timezone handling
    try:
        import pytz
        pytz_path = os.path.dirname(pytz.__file__)
        command.extend(['--add-data', f'{pytz_path}{path_separator}pytz'])
        print(f"found pytz at: {pytz_path}")
    except ImportError:
        print("pytz library not found. Please ensure it is installed in your environment.")
        sys.exit(1)
    
    # Now we add the discovered cogs as hidden imports
    for cog in COGS_TO_LOAD:
        command.extend(['--hidden-import', cog])
    
    # Add the main script to be bundled
    command.append('main.py') # For you, if you've renamed main.py, you have more problems than this failing, so we don't check for it

    # Finally, run the command
    print("Running Pyinstaller with the following command:")
    print(' '.join(f'"{c}"' if ' ' in c else c for c in command)) # This is just to make it easier to read in the console, so you can copy/paste it if you want
    print("\n Please wait, this will take a few moments...") # Just so you know something is happening (some people are impatient)

    try:
        subprocess.run(command, check=True, text=True, capture_output=False) # We don't capture it because we want to see the output in real time (in case of errors)
        print("\n --- Build completed successfully! ---") # Maybe a little too cheerful, but it's a good sign I guess.
        print(f"You can find the built executable in the '{os.path.join(project_root, 'dist')}'.")
    except subprocess.CalledProcessError as e:
        print("\n --- Build failed! ---")
        print(f"An error occurred during the build process. The error code was: {e.returncode}. Please check above for details.") # We do NOT sys.exit here because we want to see the error message above (sys.exit would hide it)
    except FileNotFoundError:
        print("\n --- Build failed! ---")
        print("Pyinstaller is not installed or not found in your PATH. Please install it and try again.") # This is a critical error, the build cannot proceed without Pyinstaller again we do not sys.exit here because we want to see this error message
        print("You can install it via pip: pip install pyinstaller") # Helpful for those who are stuck
    # Any other exceptions we don't catch, because we want to see them in the console (they're going to be caught by pyinstaller anyway so they're not our fault, hopefully)

if __name__ == '__main__':
    main()